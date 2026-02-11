import asyncio
import logging
import re

from playwright.async_api import Page
from twocaptcha import TwoCaptcha

logger = logging.getLogger(__name__)


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
    # Remote solving via 2captcha
    # ------------------------------------------------------------------

    async def _solve_remote(self, captcha_type: str, sitekey: str, url: str) -> str:
        loop = asyncio.get_event_loop()

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
