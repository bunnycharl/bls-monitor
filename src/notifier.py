import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, config: dict):
        self.bot_token = config["telegram"]["bot_token"]
        self.chat_id = str(config["telegram"]["chat_id"])
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}"

    def send_status(self, message: str) -> None:
        self._send(self.chat_id, message)

    def send_alert(self, message: str) -> None:
        self._send(self.chat_id, f"\U0001f6a8 {message}")

    def send_slot_alert(self, screenshot_path: str | None = None) -> None:
        message = (
            "\U0001f525 SLOTS DETECTED!\n\n"
            "BLS Portugal Russia \u2014 Moscow\n"
            "National Visa \u2014 Residency (Pensioners)\n"
            "Family \u2014 2 Members\n\n"
            f"\u27a1 https://russia.blsportugal.com/Global/bls/VisaTypeVerification\n\n"
            f"Time: {self._now()}"
        )
        self._send(self.chat_id, message)
        if screenshot_path:
            self._send_photo(self.chat_id, screenshot_path, "Slot availability screenshot")

    def send_health(self, total_checks: int, uptime_hours: float, errors: int) -> None:
        message = (
            f"\u2705 Health check\n"
            f"Checks: {total_checks}\n"
            f"Uptime: {uptime_hours:.1f}h\n"
            f"Recent errors: {errors}"
        )
        self._send(self.chat_id, message)

    def _send(self, chat_id: str, text: str) -> None:
        try:
            resp = requests.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if not resp.ok:
                logger.error("Telegram API error: %s %s", resp.status_code, resp.text)
        except requests.RequestException as e:
            logger.error("Telegram send failed: %s", e)

    def _send_photo(self, chat_id: str, photo_path: str, caption: str = "") -> None:
        try:
            with open(photo_path, "rb") as photo:
                requests.post(
                    f"{self.api_base}/sendPhoto",
                    data={"chat_id": chat_id, "caption": caption[:1024]},
                    files={"photo": photo},
                    timeout=30,
                )
        except Exception as e:
            logger.error("Failed to send screenshot: %s", e)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
