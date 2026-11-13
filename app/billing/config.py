"""Configuration of the billing process (`python -m app.billing`): FICHAITOR_ variables.

Separate from app/config.py: the Stripe key is read by this process only, never by the
API, the worker or the conversation (docs/admin-facturacion.md, 4.1).
"""

import os
import socket

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    worker_poll_interval_seconds: float = Field(default=2.0, gt=0)
    worker_job_timeout_seconds: float = Field(default=60.0, gt=0)
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
