import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, config: dict):
        self.bot_token = config["telegram"]["bot_token"]
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}"
        self._form_config = config.get("form", {})

        # Support both single chat_id and list of chat_ids
        tg = config["telegram"]
        if "chat_ids" in tg and tg["chat_ids"]:
            self.chat_ids = [str(cid) for cid in tg["chat_ids"]]
        else:
            self.chat_ids = [str(tg["chat_id"])]

    def send_status(self, message: str) -> None:
        self._send_all(message)

    def send_alert(self, message: str) -> None:
        self._send_all(f"\U0001f6a8 {message}")

    def send_slot_alert(self, screenshot_path: str | None = None) -> None:
        location = self._form_config.get("location", "N/A")
        visa_type = self._form_config.get("visa_type", "N/A")
        visa_sub = self._form_config.get("visa_sub_type", "")
        appt_for = self._form_config.get("appointment_for", "N/A")
        members = self._form_config.get("number_of_members", "N/A")

        message = (
            f"\U0001f525 SLOTS DETECTED!\n\n"
            f"BLS Portugal Russia \u2014 {location}\n"
            f"{visa_type}"
            + (f" \u2014 {visa_sub}\n" if visa_sub else "\n")
            + f"{appt_for} \u2014 {members}\n\n"
            f"\u27a1 https://russia.blsportugal.com/Global/bls/VisaTypeVerification\n\n"
            f"Time: {self._now()}"
        )
        for chat_id in self.chat_ids:
            self._send(chat_id, message)
            if screenshot_path:
                self._send_photo(chat_id, screenshot_path, "Slot availability screenshot")

    def send_health(self, total_checks: int, uptime_hours: float, errors: int) -> None:
        message = (
            f"\u2705 Health check\n"
            f"Checks: {total_checks}\n"
            f"Uptime: {uptime_hours:.1f}h\n"
            f"Recent errors: {errors}"
        )
        self._send_all(message)

    def send_low_balance(self, balance: float) -> None:
        self._send_all(
            f"\u26a0\ufe0f 2captcha balance low: ${balance:.2f}\n"
            f"Top up to avoid captcha solve failures."
        )

    def _send_all(self, text: str) -> None:
        for chat_id in self.chat_ids:
            self._send(chat_id, text)

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
