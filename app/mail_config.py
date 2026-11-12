"""Settings of outgoing email. Read by the worker (which sends) and by the API (which
only needs to know whether mail is available at all, and the public URL of the web)."""

from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MailSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FICHAITOR_", env_file=".env", extra="ignore")

    smtp_host: str = ""
    # 465: TLS from the first byte. Any other port: STARTTLS, which is mandatory.
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_timeout_seconds: float = Field(default=20.0, gt=0)
    # In production: avisos@fichaitor.es, a mailbox nobody reads. The From header is
    # "Fichaitor <avisos@fichaitor.es>".
    mail_from: str = ""
    mail_from_name: str = "Fichaitor"
    # Where replies go. Mandatory in the brand rules (brand/correo/LEEME.md): the
    # sender is not read. Empty: no Reply-To header.
    mail_reply_to: str = "soporte@fichaitor.es"
    # Where the links of the emails point: "<base>/app#restaurar=<token>". Absolute,
    # from here only: never from the Host header or anything of a request.
    public_base_url: str = "http://localhost:8080"
    # true: nothing is sent; the code and the restore link are written to the log.
    fake_mail: bool = False

    @field_validator("public_base_url")
    @classmethod
    def _absolute(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("FICHAITOR_PUBLIC_BASE_URL debe ser una URL absoluta http(s)")
        if parts.query or parts.fragment:
            raise ValueError("FICHAITOR_PUBLIC_BASE_URL no puede llevar consulta ni fragmento")
        return value

    @property
    def available(self) -> bool:
        return self.fake_mail or bool(self.smtp_host and self.mail_from)
