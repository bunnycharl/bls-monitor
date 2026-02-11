import logging
import os
import time

from playwright.async_api import Page

from .auth import Authenticator
from .browser import BrowserManager
from .form_filler import FormFiller
from .notifier import TelegramNotifier

logger = logging.getLogger(__name__)


class SlotChecker:
    def __init__(
        self,
        config: dict,
        browser: BrowserManager,
        auth: Authenticator,
        form: FormFiller,
        notifier: TelegramNotifier,
    ):
        self.config = config
        self.browser = browser
        self.auth = auth
        self.form = form
        self.notifier = notifier

    async def check_once(self) -> tuple[bool, str | None]:
        """Run one full check cycle.

        Returns (slots_available, screenshot_path).
        """
        page: Page = self.browser.page

        # 1. Ensure logged in
        await self.auth.ensure_authenticated(page)

        # 2. Navigate through VisaTypeVerification + captcha
        await self.form.navigate_to_form(page)

        # 3. Fill all form fields
        await self.form.fill_form(page)

        # 4. Submit and check
        available = await self.form.submit_and_check(page)

        # 5. Take screenshot
        screenshot_path = self._screenshot_path()
        os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)
        await page.screenshot(path=screenshot_path, full_page=True)
        logger.info("Screenshot saved: %s", screenshot_path)

        return available, screenshot_path

    @staticmethod
    def _screenshot_path() -> str:
        ts = int(time.time())
        return f"screenshots/check_{ts}.png"
