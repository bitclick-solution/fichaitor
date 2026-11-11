"""Settings of the screenshot diagnosis. Kept apart from `app.config` on purpose."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ScreenshotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FICHAITOR_", env_file=".env", extra="ignore")

    # Key of the Gemini API. SecretStr: it never shows up in a repr or a traceback.
    gemini_api_key: SecretStr = SecretStr("")
    # With or without the "models/" prefix.
    gemini_model: str = "gemini-3.6-flash"
    # No model: canned answers, for development and for laying out the web.
    fake_screenshot: bool = False
    # Gemini takes up to 20 MB per request with inline data, base64 and prompts included.
    screenshot_max_bytes: int = Field(default=6_000_000, gt=0, le=12_000_000)
    # Stricter than the text diagnosis (10 per hour): every capture is up to two paid calls.
    screenshot_rate_limit: int = Field(default=3, ge=0)  # per client IP
    screenshot_rate_window_seconds: int = Field(default=3600, gt=0)
    # All clients together, per 24 hours. Caps the bill if someone rotates addresses.
    screenshot_daily_limit: int = Field(default=300, ge=0)
    # Reading and suggested reply together. If the reading does not fit, the route
    # answers 503; if only the reply does not fit, it is left out.
    screenshot_timeout_seconds: float = Field(default=30.0, gt=0)
    # Below this confidence about which screen the image shows, the capture is rejected.
    screenshot_min_confidence: float = Field(default=0.6, ge=0, le=1)
