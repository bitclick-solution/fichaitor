"""Settings of outgoing email. Read by the worker (which sends) and by the API (which
only needs to know whether mail is available at all, and the public URL of the web)."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MailSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FICHAITOR_", env_file=".env", extra="ignore")

    smtp_host: str = ""
    # 465: TLS from the first byte. Any other port: STARTTLS, which is mandatory.
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_timeout_seconds: float = Field(default=20.0, gt=0)
    mail_from: str = ""
    mail_from_name: str = "Fichaitor"
    # Where replies go. Empty: no Reply-To header.
    mail_reply_to: str = ""
    # Where the links of the emails point: "<base>/app?restaurar=<token>".
    public_base_url: str = "http://localhost:8080"
    # true: nothing is sent; the code and the restore link are written to the log.
    fake_mail: bool = False

    @property
    def available(self) -> bool:
        return self.fake_mail or bool(self.smtp_host and self.mail_from)
