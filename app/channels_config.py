"""Settings of the messaging channels. Read by whoever runs the conversation (the
worker) and by the API (webhook secret, link to the bot)."""

from datetime import time

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.adapters.messaging.whatsapp import API_VERSION as WHATSAPP_API_VERSION
from app.adapters.messaging.whatsapp import link_url as whatsapp_link_url
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

    # WhatsApp (Cloud API), one number of Fichaitor in Bitclick's WhatsApp Business
    # Account (docs/canales.md, "WhatsApp"). Split by process:
    # - the conversation sends: the phone number id and the access token (a permanent
    #   token of a system user with whatsapp_business_messaging);
    # - the API receives: the phone number id (to keep only what is for our number), the
    #   app secret (signature of the webhook), the verify token (the GET of Meta) and the
    #   display number (the wa.me link).
    whatsapp_phone_number_id: str = Field(default="", pattern=r"^(\d{1,30})?$")
    whatsapp_access_token: SecretStr = SecretStr("")
    whatsapp_app_secret: SecretStr = SecretStr("")
    whatsapp_verify_token: SecretStr = SecretStr("")
    # The number as people dial it, in international form: "+34 600 000 000".
    whatsapp_display_number: str = Field(default="", pattern=r"^(\+?[0-9 ]{8,20})?$")
    whatsapp_api_version: str = Field(default=WHATSAPP_API_VERSION, pattern=r"^v\d{1,3}\.\d$")

    # Linking. A secret that is not in the database, mixed into the hash of the codes.
    # The API (which creates the codes) and the conversation (which checks them) need
    # the same value.
    link_code_pepper: SecretStr = SecretStr("")
    # Wrong codes of every chat together per hour; past it, typed codes wait (B3).
    link_global_max_failures: int = Field(default=50, ge=1, le=10_000)
    # Codes a web session may ask for, per account and hour.
    link_code_rate_limit: int = Field(default=5, ge=1, le=100)

    # Where the texts send the owner.
    channels_panel_url: str = "https://fichaitor.es/app"
    channels_signup_url: str = "https://fichaitor.es"
    channels_reconnect_url: str = "https://fichaitor.es/app"
    channels_privacy_url: str = "https://fichaitor.es/privacidad/"

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
    def whatsapp_enabled(self) -> bool:
        """The conversation can send by WhatsApp."""
        return bool(self.whatsapp_phone_number_id and self.whatsapp_access_token.get_secret_value())

    @property
    def whatsapp_webhook_enabled(self) -> bool:
        """The API can receive by WhatsApp: without every secret, the route is not mounted."""
        return bool(
            self.whatsapp_phone_number_id
            and self.whatsapp_app_secret.get_secret_value()
            and self.whatsapp_verify_token.get_secret_value()
        )

    def whatsapp_link(self, token: str) -> str | None:
        """`https://wa.me/<number>?text=Vincular%20Fichaitor%20<token>`, or None while the
        channel is not configured (no number to show, or nothing receives)."""
        if not self.whatsapp_display_number or not self.whatsapp_webhook_enabled:
            return None
        return whatsapp_link_url(self.whatsapp_display_number, token)

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
            privacy_url=self.channels_privacy_url,
            link_global_max_failures=self.link_global_max_failures,
        )
