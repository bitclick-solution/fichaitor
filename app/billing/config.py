"""Configuration of the billing process (`python -m app.billing`): FICHAITOR_ variables.

Separate from app/config.py: the Stripe key and the Odoo key are read by this process
only, never by the API, the worker, the conversation nor the backoffice
(docs/admin-facturacion.md, 4.1 and 9.0.1). Odoo's variables (FICHAITOR_ODOO_*) are read
by `odoo_settings`, with the checks of app/adapters/odoo_invoicing/real.py.
"""

import os
import socket
from collections.abc import Mapping

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.adapters.odoo_invoicing.real import OdooSettings


class BillingConfigError(Exception):
    """Raised at start-up. The message names variables, never their values."""


def _default_worker_id() -> str:
    return f"billing-{socket.gethostname()}-{os.getpid()}"


class BillingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FICHAITOR_", env_file=".env", extra="ignore")

    billing_database_url: str = ""
    # The restricted key (rk_test_... until the launch). Never in api.env.
    stripe_secret_key: SecretStr = SecretStr("")
    stripe_livemode: bool = False
    # The fake Stripe (app/adapters/stripe/fake.py): local development and tests only.
    # deploy.sh refuses any FICHAITOR_FAKE_* in production.
    fake_stripe: bool = False
    # Where Checkout comes back to: <base>/app?pago=ok&ficha=<location_id>.
    public_base_url: str = "https://fichaitor.es"
    # The fake Odoo (app/adapters/odoo_invoicing/fake.py): local development only.
    fake_odoo: bool = False
    # The offer of the email beta_ending (decision 9.0, 10): not decided, so empty.
    billing_beta_offer: str = Field(default="", max_length=300)
    # Hour (UTC) of the daily jobs: reconciliation, renewal notices, end of the beta, purge.
    billing_daily_hour_utc: int = Field(default=5, ge=0, le=23)

    worker_poll_interval_seconds: float = Field(default=2.0, gt=0)
    # The daily reconciliation reads every seat in Stripe: more than a minute with many.
    worker_job_timeout_seconds: float = Field(default=240.0, gt=0)
    worker_lock_timeout_seconds: float = Field(default=300.0, gt=0)
    worker_id: str = Field(default_factory=_default_worker_id)

    def check(self) -> None:
        problems: list[str] = []
        if not self.billing_database_url:
            problems.append("falta FICHAITOR_BILLING_DATABASE_URL")
        if self.fake_stripe:
            if self.stripe_livemode:
                problems.append(
                    "FICHAITOR_FAKE_STRIPE no se puede usar con FICHAITOR_STRIPE_LIVEMODE"
                )
        elif not self.stripe_secret_key.get_secret_value():
            problems.append("falta FICHAITOR_STRIPE_SECRET_KEY (o FICHAITOR_FAKE_STRIPE=true)")
        if self.fake_odoo and self.stripe_livemode:
            problems.append("FICHAITOR_FAKE_ODOO no se puede usar con FICHAITOR_STRIPE_LIVEMODE")
        if not self.fake_odoo:
            try:
                odoo_settings(os.environ)
            except ValueError as exc:
                problems.append(str(exc))
        if not self.public_base_url.startswith(
            ("https://", "http://localhost", "http://127.0.0.1")
        ):
            problems.append("FICHAITOR_PUBLIC_BASE_URL debe ser https")
        if self.worker_job_timeout_seconds >= self.worker_lock_timeout_seconds:
            problems.append(
                "FICHAITOR_WORKER_JOB_TIMEOUT_SECONDS debe ser menor que "
                "FICHAITOR_WORKER_LOCK_TIMEOUT_SECONDS"
            )
        if problems:
            raise BillingConfigError("; ".join(problems))


def odoo_settings(env: Mapping[str, str]) -> OdooSettings | None:
    """Odoo's settings, or None when FICHAITOR_ODOO_URL is not set: then no invoice is
    issued (they wait, pending, and the reconciliation logs them as overdue). ValueError
    naming the variable when one is wrong."""
    if not env.get("FICHAITOR_ODOO_URL"):
        return None
    return OdooSettings.from_env(env)
