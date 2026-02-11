import asyncio
import logging
import logging.handlers
import os
import random
import signal
import sys
import time

from dotenv import load_dotenv

from .auth import Authenticator
from .browser import BrowserManager
from .captcha import CaptchaSolver
from .config import AppConfig, load_config
from .form_filler import FormFiller
from .notifier import TelegramNotifier
from .slot_checker import SlotChecker

logger = logging.getLogger("bls_monitor")

BALANCE_CHECK_INTERVAL = 20  # check balance every N checks
LOW_BALANCE_THRESHOLD = 0.5  # USD


# ------------------------------------------------------------------
# Logging setup
# ------------------------------------------------------------------

def setup_logging() -> None:
    os.makedirs("logs", exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Console
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    # Rotating file
    fh = logging.handlers.RotatingFileHandler(
        "logs/monitor.log",
        maxBytes=10_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)


# ------------------------------------------------------------------
# Monitor
# ------------------------------------------------------------------

class Monitor:
    def __init__(self, config: dict):
        self.config = config
        self.notifier = TelegramNotifier(config)
        self.consecutive_errors = 0
        self.total_checks = 0
        self.start_time = time.time()
        self._shutdown = False
        self._captcha: CaptchaSolver | None = None

    async def _create_components(self) -> tuple[BrowserManager, SlotChecker]:
        """Create browser and all dependent components."""
        browser = BrowserManager(self.config)
        await browser.launch()

        self._captcha = CaptchaSolver(self.config)
        auth = Authenticator(self.config, self._captcha)
        form = FormFiller(self.config, self._captcha)
        checker = SlotChecker(self.config, browser, auth, form, self.notifier)
        return browser, checker

    async def _close_browser(self, browser: BrowserManager | None) -> None:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass

    def _check_captcha_balance(self) -> None:
        """Check 2captcha balance and warn if low."""
        if self._captcha is None:
            return
        balance = self._captcha.check_balance()
        if balance < 0:
            return  # failed to check
        logger.info("2captcha balance: $%.2f", balance)
        if balance < LOW_BALANCE_THRESHOLD:
            self.notifier.send_low_balance(balance)

    async def run(self) -> None:
        logger.info("BLS Monitor starting")
        self.notifier.send_status("BLS Monitor started")

        # Register signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._handle_signal)

        # Create browser once, reuse across checks
        browser: BrowserManager | None = None
        checker: SlotChecker | None = None

        while not self._shutdown:
            try:
                # Create browser & components if not yet created
                if browser is None:
                    browser, checker = await self._create_components()
                    # Check balance on first launch
                    self._check_captcha_balance()

                available, screenshot = await checker.check_once()
                self.total_checks += 1
                self.consecutive_errors = 0

                if available:
                    logger.info("SLOTS AVAILABLE!")
                    self.notifier.send_slot_alert(screenshot)
                    await asyncio.sleep(60)
                else:
                    logger.info("No slots available (check #%d)", self.total_checks)

            except Exception as e:
                self.consecutive_errors += 1
                logger.exception(
                    "Check cycle failed (consecutive=%d): %s",
                    self.consecutive_errors,
                    e,
                )

                # Restart browser on error
                await self._close_browser(browser)
                browser = None
                checker = None

                if self.consecutive_errors >= self.config["monitoring"]["max_retries"]:
                    self.notifier.send_alert(
                        f"Monitor failing: {self.consecutive_errors} consecutive errors.\n"
                        f"Last: {str(e)[:300]}"
                    )
                    await asyncio.sleep(300)
                    self.consecutive_errors = 0

            # Health report & balance check every N checks
            if self.total_checks > 0 and self.total_checks % BALANCE_CHECK_INTERVAL == 0:
                uptime_hrs = (time.time() - self.start_time) / 3600
                self.notifier.send_health(
                    self.total_checks, uptime_hrs, self.consecutive_errors
                )
                self._check_captcha_balance()

            if self._shutdown:
                break

            # Randomized wait
            wait = random.uniform(
                self.config["monitoring"]["check_interval_min"],
                self.config["monitoring"]["check_interval_max"],
            )
            logger.info("Next check in %.0f seconds", wait)
            await asyncio.sleep(wait)

        # Graceful shutdown
        logger.info("Shutting down...")
        await self._close_browser(browser)
        self.notifier.send_status("BLS Monitor stopped")
        logger.info("Monitor stopped")

    def _handle_signal(self) -> None:
        logger.info("Received shutdown signal")
        self._shutdown = True


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main() -> None:
    load_dotenv()  # Load .env file automatically
    setup_logging()

    # Load and validate config via Pydantic model
    app_config: AppConfig = load_config()

    # Validate required fields
    missing = []
    if not app_config.bls.email:
        missing.append("BLS_EMAIL")
    if not app_config.bls.password:
        missing.append("BLS_PASSWORD")
    if not app_config.captcha.api_key:
        missing.append("CAPTCHA_API_KEY")
    if not app_config.telegram.bot_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not app_config.telegram.chat_id and not app_config.telegram.chat_ids:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:
        logger.error(
            "Missing required config/env vars: %s", ", ".join(missing)
        )
        sys.exit(1)

    # Convert to dict for existing modules (backwards compatible)
    config = app_config.model_dump()

    monitor = Monitor(config)
    asyncio.run(monitor.run())


if __name__ == "__main__":
    main()
