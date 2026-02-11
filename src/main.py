import asyncio
import logging
import logging.handlers
import os
import random
import sys
import time

import yaml

from .auth import Authenticator
from .browser import BrowserManager
from .captcha import CaptchaSolver
from .form_filler import FormFiller
from .notifier import TelegramNotifier
from .slot_checker import SlotChecker

logger = logging.getLogger("bls_monitor")


# ------------------------------------------------------------------
# Config loading
# ------------------------------------------------------------------

def load_config(path: str = "config/settings.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # Environment variable overrides
    env_map = {
        ("bls", "email"): "BLS_EMAIL",
        ("bls", "password"): "BLS_PASSWORD",
        ("captcha", "api_key"): "CAPTCHA_API_KEY",
        ("telegram", "bot_token"): "TELEGRAM_BOT_TOKEN",
        ("telegram", "chat_id"): "TELEGRAM_CHAT_ID",
        ("browser", "proxy"): "BLS_PROXY",
    }
    for (section, key), env_var in env_map.items():
        val = os.environ.get(env_var)
        if val:
            cfg[section][key] = val

    return cfg


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

    async def run(self) -> None:
        logger.info("BLS Monitor starting")
        self.notifier.send_status("BLS Monitor started")

        while True:
            browser: BrowserManager | None = None
            try:
                browser = BrowserManager(self.config)
                await browser.launch()

                captcha = CaptchaSolver(self.config)
                auth = Authenticator(self.config, captcha)
                form = FormFiller(self.config, captcha)
                checker = SlotChecker(self.config, browser, auth, form, self.notifier)

                available, screenshot = await checker.check_once()
                self.total_checks += 1
                self.consecutive_errors = 0

                if available:
                    logger.info("SLOTS AVAILABLE!")
                    self.notifier.send_slot_alert(screenshot)
                    # Wait before rechecking to avoid alert spam
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

                if self.consecutive_errors >= self.config["monitoring"]["max_retries"]:
                    self.notifier.send_alert(
                        f"Monitor failing: {self.consecutive_errors} consecutive errors.\n"
                        f"Last: {str(e)[:300]}"
                    )
                    # Extended backoff
                    await asyncio.sleep(300)
                    self.consecutive_errors = 0

            finally:
                if browser:
                    try:
                        await browser.close()
                    except Exception:
                        pass

            # Health report every 20 checks
            if self.total_checks > 0 and self.total_checks % 20 == 0:
                uptime_hrs = (time.time() - self.start_time) / 3600
                self.notifier.send_health(
                    self.total_checks, uptime_hrs, self.consecutive_errors
                )

            # Randomized wait
            wait = random.uniform(
                self.config["monitoring"]["check_interval_min"],
                self.config["monitoring"]["check_interval_max"],
            )
            logger.info("Next check in %.0f seconds", wait)
            await asyncio.sleep(wait)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main() -> None:
    setup_logging()
    config = load_config()

    # Validate required fields
    missing = []
    if not config["bls"]["email"]:
        missing.append("BLS_EMAIL")
    if not config["bls"]["password"]:
        missing.append("BLS_PASSWORD")
    if not config["captcha"]["api_key"]:
        missing.append("CAPTCHA_API_KEY")
    if not config["telegram"]["bot_token"]:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not config["telegram"]["chat_id"]:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:
        logger.error(
            "Missing required config/env vars: %s", ", ".join(missing)
        )
        sys.exit(1)

    monitor = Monitor(config)
    asyncio.run(monitor.run())


if __name__ == "__main__":
    main()
