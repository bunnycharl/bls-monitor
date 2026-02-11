import logging

from playwright.async_api import Page

from .captcha import CaptchaSolver
from .human import HumanBehavior

logger = logging.getLogger(__name__)

# Mapping: lowercase label substring -> config key and input type.
# "has_popup" means selecting this value triggers a confirmation modal.
FIELD_DEFS = {
    "appointment category": {"config_key": "appointment_category", "type": "dropdown"},
    "appointment for": {"config_key": "appointment_for", "type": "radio", "has_popup": True},
    "number of members": {"config_key": "number_of_members", "type": "dropdown"},
    "location": {"config_key": "location", "type": "dropdown"},
    "visa type": {"config_key": "visa_type", "type": "dropdown"},
    "visa sub type": {"config_key": "visa_sub_type", "type": "dropdown"},
}


class FormFiller:
    def __init__(self, config: dict, captcha: CaptchaSolver):
        self.config = config
        self.captcha = captcha
        self.human = HumanBehavior()
        # Pre-resolve desired values from config
        self._values: dict[str, str] = {}
        for label_key, fdef in FIELD_DEFS.items():
            self._values[label_key] = config["form"][fdef["config_key"]]

    # ------------------------------------------------------------------
    # Step 1: VisaTypeVerification -> captcha -> redirect to visatype form
    # ------------------------------------------------------------------

    async def navigate_to_form(self, page: Page) -> None:
        logger.info("Navigating to VisaTypeVerification")
        await page.goto(
            self.config["bls"]["visa_verification_url"],
            wait_until="networkidle",
            timeout=30000,
        )
        await self.human.random_delay(2000, 4000)

        # Click "Verify Selection"
        verify_btn = page.locator(
            'button:has-text("Verify Selection"), '
            'input[value*="Verify"], '
            'a:has-text("Verify Selection")'
        ).first
        await self.human.click_with_delay(verify_btn)
        await self.human.random_delay(2000, 3000)

        # Solve captcha that appears after Verify Selection
        await self.captcha.detect_and_solve(page)
        await self.human.random_delay(500, 1000)

        # Click Submit to pass the captcha screen
        submit = page.locator(
            'button[type="submit"], input[type="submit"], '
            'button:has-text("Submit")'
        ).first
        await self.human.click_with_delay(submit)

        # Wait for redirect to visatype page
        await page.wait_for_load_state("networkidle", timeout=20000)
        logger.info("On form page: %s", page.url)

    # ------------------------------------------------------------------
    # Step 2: Fill all fields (random order on page)
    # ------------------------------------------------------------------

    async def fill_form(self, page: Page) -> None:
        """Identify all form fields by their label text and fill them."""
        filled: set[str] = set()

        # Collect all labels on the page
        labels = await page.query_selector_all("label")

        for label_el in labels:
            raw_text = await label_el.inner_text()
            label_text = raw_text.strip().lower()

            # Match against known fields
            matched_key: str | None = None
            for field_key in FIELD_DEFS:
                if field_key in label_text:
                    matched_key = field_key
                    break

            if matched_key is None or matched_key in filled:
                continue

            fdef = FIELD_DEFS[matched_key]
            value = self._values[matched_key]
            logger.info("Filling: %s -> %s", matched_key, value)

            # Find associated input via the "for" attribute
            for_attr = await label_el.get_attribute("for")

            await self.human.random_delay(500, 1500)

            if fdef["type"] == "dropdown":
                await self._fill_dropdown(page, label_el, for_attr, value)
            elif fdef["type"] == "radio":
                await self._fill_radio(page, value, fdef.get("has_popup", False))

            filled.add(matched_key)
            await self.human.random_delay(800, 1800)

        missing = set(FIELD_DEFS.keys()) - filled
        if missing:
            logger.error("Could not find fields: %s", missing)
            raise RuntimeError(f"Missing form fields: {missing}")

        logger.info("All form fields filled")

    async def _fill_dropdown(
        self, page: Page, label_el, for_attr: str | None, value: str
    ) -> None:
        """Select a value from a <select> dropdown."""
        if for_attr:
            select_loc = page.locator(f"#{for_attr}")
        else:
            # Fallback: find <select> inside the same form-group container
            parent_handle = await label_el.evaluate_handle(
                "el => el.closest('.form-group') || el.parentElement"
            )
            select_loc = page.locator("select").first

        # Try selecting by visible label text
        try:
            await select_loc.select_option(label=value)
        except Exception:
            # Fallback: partial text match via JS
            await page.evaluate(
                """(val) => {
                    for (const sel of document.querySelectorAll('select')) {
                        for (const opt of sel.options) {
                            if (opt.text.includes(val)) {
                                sel.value = opt.value;
                                sel.dispatchEvent(new Event('change', { bubbles: true }));
                                return;
                            }
                        }
                    }
                }""",
                value,
            )

        # Trigger change event to load dependent fields
        if for_attr:
            await page.evaluate(
                f"""() => {{
                    const el = document.getElementById('{for_attr}');
                    if (el) el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}"""
            )

        # Wait for any dependent fields to render
        await self.human.random_delay(1000, 2000)

    async def _fill_radio(self, page: Page, value: str, has_popup: bool) -> None:
        """Select a radio button by its label text."""
        radio = page.locator(f'label:has-text("{value}")').first
        await self.human.click_with_delay(radio)

        if has_popup:
            await self.human.random_delay(1000, 2000)
            confirm_btn = page.locator(
                'button:has-text("Confirm"), '
                '.modal button:has-text("Confirm"), '
                '.modal .btn-primary, '
                'button:has-text("OK"), '
                '.swal2-confirm'
            ).first
            try:
                await confirm_btn.wait_for(state="visible", timeout=5000)
                await self.human.click_with_delay(confirm_btn)
                logger.info("Popup confirmed for '%s'", value)
            except Exception:
                logger.warning("No popup appeared after selecting '%s'", value)

        await self.human.random_delay(500, 1000)

    # ------------------------------------------------------------------
    # Step 3: Submit and check availability
    # ------------------------------------------------------------------

    async def submit_and_check(self, page: Page) -> bool:
        """Submit the form and return True if slots are available."""
        submit = page.locator(
            'button[type="submit"]:visible, '
            'input[type="submit"]:visible, '
            'button:visible:has-text("Submit")'
        ).first
        await self.human.click_with_delay(submit)

        # Wait for response (popup or page change)
        await self.human.random_delay(3000, 5000)

        return await self._check_availability(page)

    @staticmethod
    async def _check_availability(page: Page) -> bool:
        """Analyze page content for slot availability signals."""
        text = (await page.inner_text("body")).lower()

        no_slot_signals = [
            "no appointments available",
            "no slots available",
            "currently, no slots",
            "no available dates",
            "currently unavailable",
        ]
        for signal in no_slot_signals:
            if signal in text:
                logger.info("No slots (matched: '%s')", signal)
                return False

        # If no negative signal found, might have slots
        slot_signals = [
            "select date",
            "select time",
            "available dates",
            "book appointment",
            "appointment date",
            "calendar",
        ]
        for signal in slot_signals:
            if signal in text:
                logger.info("SLOTS POSSIBLY AVAILABLE (matched: '%s')", signal)
                return True

        # Ambiguous — err on the side of alerting
        logger.warning("Ambiguous page state — treating as potential availability")
        return True
