import asyncio
import logging
import time

from playwright.async_api import Page

from .captcha import CaptchaSolver
from .human import HumanBehavior

logger = logging.getLogger(__name__)

# Cloudflare challenge page indicators
CF_INDICATORS = [
    "Just a moment",
    "Checking your browser",
    "cf-browser-verification",
    "challenge-platform",
    "Attention Required",
]


class Authenticator:
    def __init__(self, config: dict, captcha: CaptchaSolver):
        self.config = config
        self.captcha = captcha
        self.human = HumanBehavior()
        self._last_login_time: float = 0

    @property
    def session_valid(self) -> bool:
        if self._last_login_time == 0:
            return False
        elapsed = time.time() - self._last_login_time
        return elapsed < self.config["monitoring"]["session_refresh_interval"]

    async def ensure_authenticated(self, page: Page) -> None:
        if not self.session_valid:
            await self.login(page)
            return

        # Quick check: go to home page, see if redirected to login
        await page.goto(
            self.config["bls"]["home_url"],
            wait_until="domcontentloaded",
            timeout=30000,
        )
        if "login" in page.url.lower():
            logger.info("Session expired (redirected to login)")
            await self.login(page)

    async def login(self, page: Page) -> None:
        logger.info("Starting login flow")

        await page.goto(
            self.config["bls"]["login_url"],
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await self.human.random_delay(2000, 4000)

        # Handle Cloudflare challenge if present
        if await self._is_cloudflare(page):
            await self._handle_cloudflare(page)

        # Debug: save screenshot and HTML of login page
        try:
            import os
            os.makedirs("screenshots", exist_ok=True)
            await page.screenshot(path="screenshots/debug_login_page.png")
            html = await page.content()
            with open("screenshots/debug_login_page.html", "w", encoding="utf-8") as f:
                f.write(html)
            logger.info("Login page debug saved. URL: %s, Title: %s", page.url, await page.title())
        except Exception as e:
            logger.warning("Could not save debug data: %s", e)

        # BLS uses honeypot fields: many hidden UserId/Password inputs,
        # only one pair is visible. Target visible inputs only.
        email_sel = 'input[name^="UserId"]:visible'
        await self.human.type_like_human(page, email_sel, self.config["bls"]["email"])
        await self.human.random_delay(300, 800)

        pass_sel = 'input[name^="Password"]:visible'
        await self.human.type_like_human(page, pass_sel, self.config["bls"]["password"])
        await self.human.random_delay(1000, 2000)

        # Check privacy consent checkbox if present
        checkbox = page.locator('input[type="checkbox"]').first
        if await checkbox.is_visible():
            await checkbox.check()
            logger.info("Privacy checkbox checked")
            await self.human.random_delay(300, 600)

        # BLS login flow:
        # 1. Click "Verify" → opens captcha popup via JS OpenWindow()
        # 2. Solve captcha → callback OnVarifyCaptcha() shows "Login" button
        # 3. Click "Login" to submit
        #
        # The popup mechanism uses iframes that may not work in headless.
        # Instead, we extract the captcha URL and solve it directly.

        await self._handle_bls_captcha(page)

        # After captcha is solved, the "Login" submit button should be visible
        login_btn = page.locator('#btnSubmit')
        try:
            await login_btn.wait_for(state="visible", timeout=10000)
            logger.info("Login submit button visible, clicking")
            await self.human.click_with_delay(login_btn)
        except Exception:
            logger.warning("Login submit button not visible after captcha")
            try:
                await page.screenshot(path="screenshots/debug_no_login_btn.png")
            except Exception:
                pass
            raise RuntimeError("Login button not visible — captcha verification may have failed")

        # Wait for navigation away from login
        try:
            await page.wait_for_url("**/home/**", timeout=20000)
        except Exception:
            current = page.url
            try:
                await page.screenshot(path="screenshots/debug_login_failed.png")
            except Exception:
                pass
            if "login" in current.lower():
                raise RuntimeError(f"Login failed — still on login page: {current}")
            logger.warning("Unexpected post-login URL: %s", current)

        self._last_login_time = time.time()
        logger.info("Login successful")

    # ------------------------------------------------------------------
    # BLS custom captcha handling
    # ------------------------------------------------------------------

    async def _handle_bls_captcha(self, page: Page) -> None:
        """Handle BLS captcha verification.

        Flow: extract captcha URL → open in new tab → solve grid captcha →
        the captcha page calls parent OnVarifyCaptcha() → Login button appears.
        """
        # Extract captcha URL from the page's JS
        captcha_url = await page.evaluate("""() => {
            const html = document.documentElement.innerHTML;
            const m = html.match(/iframeOpenUrl\\s*=\\s*['"]([^'"]+)['"]/);
            return m ? m[1] : null;
        }""")

        if not captcha_url:
            raise RuntimeError("Could not extract BLS captcha URL from page")

        if captcha_url.startswith("/"):
            captcha_url = self.config["bls"]["base_url"] + captcha_url

        logger.info("BLS captcha URL: %s", captcha_url[:100])

        # Open captcha page in a new tab
        captcha_page = await page.context.new_page()
        try:
            await captcha_page.goto(captcha_url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)

            # Save captcha page screenshot
            try:
                await captcha_page.screenshot(path="screenshots/debug_captcha_page.png")
            except Exception:
                pass

            # Solve the grid captcha
            solved = await self.captcha.solve_bls_grid(captcha_page)
            if not solved:
                raise RuntimeError("Failed to solve BLS grid captcha")

            # After submit, the captcha page should call parent's OnVarifyCaptcha
            # Wait for the page to process
            await asyncio.sleep(2)

            # Check if the captcha page shows a success result
            result_text = await captcha_page.evaluate("""() => {
                return document.body.innerText || '';
            }""")
            logger.info("Captcha page result text: %s", result_text[:200])

        finally:
            await captcha_page.close()

        # The captcha page's submit should have called parent.OnVarifyCaptcha
        # via window.opener or parent. Since we opened in a new tab, we need
        # to manually trigger the callback on the login page.
        # Check if the Login button is already visible (popup communicated success)
        login_visible = await page.evaluate("""() => {
            const btn = document.getElementById('btnSubmit');
            return btn && btn.style.display !== 'none';
        }""")

        if not login_visible:
            # Manually call the callback
            logger.info("Login button not visible, calling OnVarifyCaptcha manually")
            await page.evaluate("""() => {
                if (typeof OnVarifyCaptcha === 'function') {
                    OnVarifyCaptcha({success: true, captcha: 'solved'});
                }
            }""")
            await asyncio.sleep(1)

    # ------------------------------------------------------------------
    # Cloudflare handling
    # ------------------------------------------------------------------

    async def _is_cloudflare(self, page: Page) -> bool:
        title = await page.title()
        content = await page.content()
        return any(ind in title or ind in content for ind in CF_INDICATORS)

    async def _handle_cloudflare(self, page: Page) -> None:
        logger.info("Cloudflare challenge detected, waiting for JS challenge…")

        # Wait up to 15 seconds for automatic JS challenge to resolve
        for _ in range(15):
            if not await self._is_cloudflare(page):
                logger.info("Cloudflare JS challenge passed")
                return
            await asyncio.sleep(1)

        # Try solving Turnstile captcha if present
        turnstile = await page.query_selector(".cf-turnstile")
        if turnstile:
            logger.info("Solving Cloudflare Turnstile captcha")
            await self.captcha.detect_and_solve(page)
            submit = await page.query_selector("input[type='submit'], button[type='submit']")
            if submit:
                await submit.click()
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            if not await self._is_cloudflare(page):
                return

        # Last resort: reload and wait
        logger.warning("Cloudflare still active, reloading page")
        await page.reload()
        await asyncio.sleep(5)
        if await self._is_cloudflare(page):
            raise RuntimeError("Could not pass Cloudflare challenge")
