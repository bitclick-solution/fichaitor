"""Worker configuration: environment variables with the FICHAITOR_ prefix.

Separate from app/config.py on purpose: the token private key and the worker's
database user are read by the worker only, never by the API.
"""

import base64
import binascii
import os
import socket

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(Exception):
    """Raised at start-up. The message names variables, never their values."""


def _default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FICHAITOR_", env_file=".env", extra="ignore")

    worker_database_url: str = ""
    token_private_key: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    fake_google: bool = False

    worker_poll_interval_seconds: float = Field(default=2.0, gt=0)
    # A job running for longer than the lock timeout is taken for dead and retried
    # by another worker, so every job is cut off well before that.
    worker_job_timeout_seconds: float = Field(default=120.0, gt=0)
    worker_lock_timeout_seconds: float = Field(default=300.0, gt=0)
    worker_id: str = Field(default_factory=_default_worker_id)

    def check(self) -> None:
        """Validate what pydantic cannot. Called once when the worker starts."""
        problems: list[str] = []
        if not self.worker_database_url:
            problems.append("falta FICHAITOR_WORKER_DATABASE_URL")
        if not self.token_private_key:
            problems.append("falta FICHAITOR_TOKEN_PRIVATE_KEY")
        elif not _is_key(self.token_private_key):
            problems.append(
                "FICHAITOR_TOKEN_PRIVATE_KEY no es una clave válida (32 bytes en base64)"
            )
        if not self.fake_google:
            if not self.google_client_id:
                problems.append("falta FICHAITOR_GOOGLE_CLIENT_ID")
            if not self.google_client_secret:
                problems.append("falta FICHAITOR_GOOGLE_CLIENT_SECRET")
        if self.worker_job_timeout_seconds >= self.worker_lock_timeout_seconds:
            problems.append(
                "FICHAITOR_WORKER_JOB_TIMEOUT_SECONDS debe ser menor que "
                "FICHAITOR_WORKER_LOCK_TIMEOUT_SECONDS"
            )
        if problems:
            raise ConfigError("; ".join(problems))


def _is_key(value: str) -> bool:
    try:
        return len(base64.b64decode(value, validate=True)) == 32
    except (binascii.Error, ValueError):
        return False
