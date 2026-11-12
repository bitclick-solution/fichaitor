"""Settings of the messaging channels. Read by whoever runs the conversation (the
worker) and by the API (webhook secret, link to the bot)."""

from datetime import time

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.application.conversation_service import ServiceConfig
from app.domain.conversation import QuietHours


class ChannelsSettings(BaseSettings):
    # hide_input_in_errors: a badly shaped secret is not printed by the error about it.
    model_config = SettingsConfigDict(
        env_prefix="FICHAITOR_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    # Telegram. One bot of Fichaitor for every customer (@FichaitorBot): the owner
    # never creates a bot. SecretStr: never in a repr, a log or a traceback.
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_bot_username: str = Field(default="FichaitorBot", pattern=r"^[A-Za-z0-9_]{5,32}$")
    # Production: Telegram sends it back in X-Telegram-Bot-Api-Secret-Token.
    # 1-256 characters, only A-Z, a-z, 0-9, _ and - (Bot API, setWebhook).
    telegram_webhook_secret: SecretStr = SecretStr("")
    # Local development without a public URL: long polling with getUpdates.
    telegram_polling: bool = False

    # Linking. A secret that is not in the database, mixed into the hash of the codes.
    link_code_pepper: SecretStr = SecretStr("")

    # Where the texts send the owner.
    channels_panel_url: str = "https://fichaitor.es/app"
    channels_signup_url: str = "https://fichaitor.es"
    channels_reconnect_url: str = "https://fichaitor.es/app"

    # Frequency and quiet hours (docs/04-ux-conversacional.md, section 3).
    proactive_weekly_cap: int = Field(default=2, ge=0, le=50)
    quiet_hours_start: time = time(21, 0)
    quiet_hours_end: time = time(9, 0)

    @field_validator("telegram_webhook_secret")
    @classmethod
    def _secret_shape(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if secret and not (
            len(secret) <= 256 and secret.replace("_", "").replace("-", "").isalnum()
        ):
            raise ValueError("solo admite A-Z, a-z, 0-9, _ y -, hasta 256 caracteres")
        if secret and not secret.isascii():
            raise ValueError("solo admite caracteres ASCII")
        return value

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token.get_secret_value())

    def telegram_link(self, code: str) -> str:
        """`https://t.me/<bot>?start=<code>`: opens the bot and sends the code."""
        return f"https://t.me/{self.telegram_bot_username}?start={code}"

    def service_config(self) -> ServiceConfig:
        return ServiceConfig(
            panel_url=self.channels_panel_url,
            signup_url=self.channels_signup_url,
            reconnect_url=self.channels_reconnect_url,
            weekly_cap=self.proactive_weekly_cap,
            quiet=QuietHours(start=self.quiet_hours_start, end=self.quiet_hours_end),
            link_code_pepper=self.link_code_pepper.get_secret_value(),
        )
