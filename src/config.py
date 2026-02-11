"""Typed configuration model with validation."""

import os

import yaml
from pydantic import BaseModel, Field


class BlsConfig(BaseModel):
    base_url: str = "https://russia.blsportugal.com"
    login_url: str = "https://russia.blsportugal.com/Global/account/login"
    home_url: str = "https://russia.blsportugal.com/Global/home/index"
    visa_verification_url: str = "https://russia.blsportugal.com/Global/bls/VisaTypeVerification"
    email: str = ""
    password: str = ""


class FormConfig(BaseModel):
    appointment_category: str = "Normal"
    appointment_for: str = "Family"
    number_of_members: str = "2 Members"
    location: str = "Moscow"
    visa_type: str = "National Visa"
    visa_sub_type: str = ""


class CaptchaConfig(BaseModel):
    provider: str = "2captcha"
    api_key: str = ""
    timeout: int = 120
    poll_interval: int = 5


class TelegramConfig(BaseModel):
    bot_token: str = ""
    chat_id: str = ""
    chat_ids: list[str] = Field(default_factory=list)


class MonitoringConfig(BaseModel):
    check_interval_min: int = 180
    check_interval_max: int = 300
    max_retries: int = 3
    session_refresh_interval: int = 1800


class BrowserConfig(BaseModel):
    headless: bool = True
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
    viewport_width: int = 1920
    viewport_height: int = 1080
    locale: str = "ru-RU"
    timezone: str = "Europe/Moscow"
    proxy: str = ""


class AppConfig(BaseModel):
    bls: BlsConfig = Field(default_factory=BlsConfig)
    form: FormConfig = Field(default_factory=FormConfig)
    captcha: CaptchaConfig = Field(default_factory=CaptchaConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)


def load_config(path: str = "config/settings.yaml") -> AppConfig:
    """Load config from YAML file with environment variable overrides."""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    config = AppConfig(**raw)

    # Environment variable overrides
    env_overrides = {
        "BLS_EMAIL": ("bls", "email"),
        "BLS_PASSWORD": ("bls", "password"),
        "CAPTCHA_API_KEY": ("captcha", "api_key"),
        "TELEGRAM_BOT_TOKEN": ("telegram", "bot_token"),
        "TELEGRAM_CHAT_ID": ("telegram", "chat_id"),
        "BLS_PROXY": ("browser", "proxy"),
    }
    for env_var, (section, field) in env_overrides.items():
        val = os.environ.get(env_var)
        if val:
            sub = getattr(config, section)
            setattr(sub, field, val)

    return config
