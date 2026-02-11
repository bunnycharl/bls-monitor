import asyncio
import base64
import logging
import re

from playwright.async_api import Page
from twocaptcha import TwoCaptcha

logger = logging.getLogger(__name__)

MAX_SOLVE_RETRIES = 3
SOLVE_RETRY_DELAY = 5  # seconds


class CaptchaSolver:
    def __init__(self, config: dict):
        cap_cfg = config["captcha"]
        self._solver = TwoCaptcha(
            cap_cfg["api_key"],
            defaultTimeout=cap_cfg["timeout"],
            pollingInterval=cap_cfg["poll_interval"],
        )

    async def detect_and_solve(self, page: Page) -> str | None:
        """Detect captcha on the page, solve it via 2captcha, inject token.

        Returns the token string or None if no captcha was found.
        """
        captcha_type, sitekey = await self._detect(page)
        if captcha_type is None:
            logger.info("No captcha detected")
            return None

        url = page.url
        logger.info("Detected %s (sitekey=%s...) on %s", captcha_type, sitekey[:12], url)

        token = await self._solve_remote(captcha_type, sitekey, url)
        await self._inject_token(page, captcha_type, token)
        logger.info("Captcha token injected (len=%d)", len(token))
        return token

    def check_balance(self) -> float:
        """Return current 2captcha account balance in USD."""
        try:
            balance = self._solver.balance()
            return float(balance)
        except Exception as e:
            logger.error("Failed to check 2captcha balance: %s", e)
            return -1.0

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    async def _detect(self, page: Page) -> tuple[str | None, str | None]:
        # 1. Check main page DOM
        result = await self._detect_in_frame(page)
        if result[0]:
            return result

        # 2. Check inside iframes (captcha widgets are often in iframes)
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            result = await self._detect_in_frame(frame)
            if result[0]:
                return result

        # 3. Fallback: regex scan of full page HTML
        html = await page.content()
        return self._detect_from_html(html)

    async def _detect_in_frame(self, frame) -> tuple[str | None, str | None]:
        # hCaptcha
        el = await frame.query_selector(".h-captcha[data-sitekey]")
        if el:
            return ("hcaptcha", await el.get_attribute("data-sitekey"))

        # Cloudflare Turnstile
        el = await frame.query_selector(".cf-turnstile[data-sitekey]")
        if el:
            return ("turnstile", await el.get_attribute("data-sitekey"))

        # reCAPTCHA
        el = await frame.query_selector(".g-recaptcha[data-sitekey]")
        if el:
            return ("recaptcha", await el.get_attribute("data-sitekey"))

        # Generic data-sitekey
        el = await frame.query_selector("[data-sitekey]")
        if el:
            sitekey = await el.get_attribute("data-sitekey")
            cls = (await el.get_attribute("class")) or ""
            if "h-captcha" in cls:
                return ("hcaptcha", sitekey)
            if "cf-turnstile" in cls:
                return ("turnstile", sitekey)
            # Default to hcaptcha (most common on BLS)
            return ("hcaptcha", sitekey)

        return (None, None)

    @staticmethod
    def _detect_from_html(html: str) -> tuple[str | None, str | None]:
        m = re.search(r'data-sitekey=["\']([a-f0-9-]{30,})["\']', html)
        if not m:
            return (None, None)
        sitekey = m.group(1)
        if "h-captcha" in html:
            return ("hcaptcha", sitekey)
        if "cf-turnstile" in html:
            return ("turnstile", sitekey)
        if "g-recaptcha" in html:
            return ("recaptcha", sitekey)
        return ("hcaptcha", sitekey)

    # ------------------------------------------------------------------
    # Remote solving via 2captcha (with retry)
    # ------------------------------------------------------------------

    async def _solve_remote(self, captcha_type: str, sitekey: str, url: str) -> str:
        loop = asyncio.get_event_loop()
        last_error = None

        for attempt in range(1, MAX_SOLVE_RETRIES + 1):
            try:
                if captcha_type == "hcaptcha":
                    result = await loop.run_in_executor(
                        None, lambda: self._solver.hcaptcha(sitekey=sitekey, url=url)
                    )
                elif captcha_type == "turnstile":
                    result = await loop.run_in_executor(
                        None, lambda: self._solver.turnstile(sitekey=sitekey, url=url)
                    )
                elif captcha_type == "recaptcha":
                    result = await loop.run_in_executor(
                        None, lambda: self._solver.recaptcha(sitekey=sitekey, url=url)
                    )
                else:
                    raise ValueError(f"Unknown captcha type: {captcha_type}")

                return result["code"]

            except ValueError:
                raise
            except Exception as e:
                last_error = e
                logger.warning(
                    "2captcha attempt %d/%d failed: %s",
                    attempt, MAX_SOLVE_RETRIES, e,
                )
                if attempt < MAX_SOLVE_RETRIES:
                    await asyncio.sleep(SOLVE_RETRY_DELAY)

        raise RuntimeError(
            f"2captcha failed after {MAX_SOLVE_RETRIES} attempts: {last_error}"
        )

    # ------------------------------------------------------------------
    # BLS grid captcha solving
    # ------------------------------------------------------------------

    async def solve_bls_grid(self, captcha_page) -> bool:
        """Solve the BLS custom grid captcha.

        The captcha shows a 3x3 grid of number images and asks to
        select all images matching a target number.

        Returns True if solved successfully.
        """
        # 1. Extract the target number from the visible label
        target_number = await captcha_page.evaluate("""() => {
            // Find the visible box-label
            const labels = document.querySelectorAll('.box-label');
            for (const label of labels) {
                const style = window.getComputedStyle(label);
                if (style.display !== 'none' && style.visibility !== 'hidden'
                    && label.offsetParent !== null) {
                    const m = label.textContent.match(/number\\s+(\\d+)/);
                    if (m) return m[1];
                }
            }
            // Fallback: try all labels
            for (const label of labels) {
                const m = label.textContent.match(/number\\s+(\\d+)/);
                if (m) return m[1];
            }
            return null;
        }""")

        if not target_number:
            logger.error("Could not extract target number from BLS captcha")
            return False

        logger.info("BLS captcha target number: %s", target_number)

        # 2. Get only VISIBLE cell images (BLS has many hidden honeypot cells)
        cells = await captcha_page.evaluate("""() => {
            const result = [];
            const imgs = document.querySelectorAll('.captcha-img');
            for (const img of imgs) {
                // Check if the image is actually visible
                const rect = img.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) continue;
                const style = window.getComputedStyle(img.parentElement);
                if (style.display === 'none' || style.visibility === 'hidden') continue;

                const parent = img.closest('[id]');
                const src = img.getAttribute('src') || '';
                result.push({
                    id: parent ? parent.id : null,
                    src: src,
                });
            }
            return result;
        }""")

        if not cells:
            logger.error("No captcha cell images found")
            return False

        logger.info("Found %d captcha cells, recognizing numbers...", len(cells))

        # 3. Recognize each cell's number using 2captcha
        loop = asyncio.get_event_loop()
        matching_ids = []

        for i, cell in enumerate(cells):
            if not cell.get("src") or not cell.get("id"):
                continue

            # Extract base64 data from src
            src = cell["src"]
            if src.startswith("data:"):
                # Remove "data:image/...;base64," prefix
                b64_data = src.split(",", 1)[1] if "," in src else src
            else:
                continue

            try:
                result = await loop.run_in_executor(
                    None,
                    lambda b64=b64_data: self._solver.normal(
                        b64, numeric=1, minLen=2, maxLen=4
                    ),
                )
                recognized = result.get("code", "").strip()
                logger.info("Cell %d (%s): recognized=%s, target=%s",
                            i, cell["id"][:8], recognized, target_number)

                if recognized == target_number:
                    matching_ids.append(cell["id"])
            except Exception as e:
                logger.warning("Failed to recognize cell %d: %s", i, e)
                continue

        if not matching_ids:
            logger.error("No cells matched target number %s", target_number)
            return False

        logger.info("Clicking %d matching cells: %s", len(matching_ids), matching_ids)

        # 4. Click matching cells
        for cell_id in matching_ids:
            img = captcha_page.locator(f"#{cell_id} img")
            await img.click()
            await asyncio.sleep(0.3)

        # 5. Click "Submit Selection"
        submit = captcha_page.locator('text=Submit Selection').first
        await submit.click()
        await asyncio.sleep(2)

        logger.info("BLS captcha submitted")
        return True

    # ------------------------------------------------------------------
    # Token injection
    # ------------------------------------------------------------------

    @staticmethod
    async def _inject_token(page: Page, captcha_type: str, token: str) -> None:
        # Escape token for safe JS string embedding
        safe_token = token.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")

        if captcha_type == "hcaptcha":
            await page.evaluate(
                f"""() => {{
                    for (const name of ['h-captcha-response', 'g-recaptcha-response']) {{
                        const el = document.querySelector('[name="' + name + '"]');
                        if (el) {{ el.value = '{safe_token}'; }}
                    }}
                }}"""
            )
        elif captcha_type == "turnstile":
            await page.evaluate(
                f"""() => {{
                    const el = document.querySelector('[name="cf-turnstile-response"]');
                    if (el) {{ el.value = '{safe_token}'; }}
                }}"""
            )
        elif captcha_type == "recaptcha":
            await page.evaluate(
                f"""() => {{
                    const el = document.querySelector('[name="g-recaptcha-response"]');
                    if (el) {{ el.value = '{safe_token}'; }}
                }}"""
            )
