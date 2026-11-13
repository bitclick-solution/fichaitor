"""Configuration of the backoffice (`python -m app.admin`): FICHAITOR_ADMIN_* variables.

Separate from app/config.py and read only from the environment of the process (admin.env
in production, through Compose). Not from `.env`: that file carries the keys of the other
processes. The backoffice has no key of Stripe, Odoo, SMTP nor Google
(docs/admin-facturacion.md, 4.1): if the environment carries one, it refuses to start.
"""

import os
import re
from collections.abc import Mapping
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Variables that belong to other processes. Their presence means admin.env was mixed up
# with another file: the backoffice does not start.
_FOREIGN = re.compile(
    r"^FICHAITOR_[A-Z0-9_]*(STRIPE|ODOO|SMTP|GOOGLE|TOKEN_PRIVATE|TELEGRAM|WHATSAPP|VERTEX|"
    r"GEMINI|PLACES)[A-Z0-9_]*$"
)
_HEX_KEY = re.compile(r"^[0-9a-fA-F]{64}$")

# The only Host the backoffice answers to (DNS rebinding, section 6.1).
DEFAULT_ALLOWED_HOSTS = ("127.0.0.1:8090", "localhost:8090")


class AdminConfigError(Exception):
    """Raised at start-up. The message names variables, never their values."""


class AdminSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FICHAITOR_ADMIN_", env_file=None, extra="ignore")

    # fichaitor_admin_prod (written by deploy/init-roles.sh).
    database_url: SecretStr = SecretStr("")
    # AES-256-GCM key of the TOTP secrets: 32 bytes in hexadecimal (openssl rand -hex 32).
    totp_key: SecretStr = SecretStr("")
    # The "pepper" of the passwords and recovery codes: never in the database.
    password_pepper: SecretStr = SecretStr("")
    # authserv-id of our mail server: the leave asked for by email (DMARC, section 6.4).
    # Empty: the check of the sender is refused.
    authserv_id: str = ""

    host: str = "0.0.0.0"  # noqa: S104 - inside its container; Compose publishes 127.0.0.1 only
    port: int = Field(default=8090, ge=1, le=65535)
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS
    # The bundle of web/admin (served on the same origin). Missing: a placeholder page.
    static_dir: Path = Path("web/admin/dist")
    # Base of the links to the invoices in Odoo (section 6.4). Only a URL, no key.
    erp_base_url: str = "https://erp.bitclick.solutions"

    # Communications by email (docs/contrato-backoffice.md, 10): how many emails a
    # confirmed communication enqueues per minute. The database accepts 1 to 600.
    communication_per_minute: int = Field(default=60, ge=1, le=600)

    # Argon2id (section 6.2): 64 MiB, 3 iterations, parallelism 4. Lower only in tests.
    argon2_time_cost: int = Field(default=3, ge=1)
    argon2_memory_kib: int = Field(default=65536, ge=8)
    argon2_parallelism: int = Field(default=4, ge=1)

    def check(self, environ: Mapping[str, str] | None = None) -> None:
        env = os.environ if environ is None else environ
        problems: list[str] = []
        if not self.database_url.get_secret_value():
            problems.append("falta FICHAITOR_ADMIN_DATABASE_URL")
        if not _HEX_KEY.match(self.totp_key.get_secret_value()):
            problems.append("FICHAITOR_ADMIN_TOTP_KEY falta o no son 64 caracteres hexadecimales")
        if len(self.password_pepper.get_secret_value()) < 32:
            problems.append("FICHAITOR_ADMIN_PASSWORD_PEPPER falta o tiene menos de 32 caracteres")
        if self.totp_key.get_secret_value() and (
            self.totp_key.get_secret_value() == self.password_pepper.get_secret_value()
        ):
            problems.append(
                "FICHAITOR_ADMIN_TOTP_KEY y FICHAITOR_ADMIN_PASSWORD_PEPPER son iguales"
            )
        if not self.allowed_hosts:
            problems.append("FICHAITOR_ADMIN_ALLOWED_HOSTS está vacía")
        foreign = sorted(name for name in env if _FOREIGN.match(name))
        if foreign:
            problems.append(
                "el entorno lleva variables de otros procesos (el backoffice no tiene claves de"
                " Stripe, Odoo, SMTP ni Google): " + ", ".join(foreign)
            )
        if problems:
            raise AdminConfigError("; ".join(problems))
