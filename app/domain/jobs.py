"""Job rules: states, retry policy and what may be stored as an error. No I/O."""

from collections.abc import Iterable
from dataclasses import dataclass, field

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

DEFAULT_MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 3600
ERROR_LIMIT = 500


class JobError(Exception):
    """A failure whose message is safe to store in job.last_error and to log.

    `retry_in`: seconds until the next attempt, when the handler knows better than the
    default backoff (a slow outage of Odoo, retried for days)."""

    def __init__(self, message: str = "", *, retry_in: int | None = None) -> None:
        super().__init__(message)
        self.retry_in = retry_in


class PermanentJobError(JobError):
    """Retrying cannot help: the job goes straight to 'failed'."""


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    payload: dict = field(default_factory=dict)
    attempts: int = 1  # counts the attempt in progress
    max_attempts: int = DEFAULT_MAX_ATTEMPTS

    @property
    def is_last_attempt(self) -> bool:
        return self.attempts >= self.max_attempts


def backoff_seconds(
    attempts: int, base: int = BACKOFF_BASE_SECONDS, cap: int = BACKOFF_CAP_SECONDS
) -> int:
    """Wait before the next try: base, 2x, 4x... up to the cap."""
    exponent = min(max(attempts, 1) - 1, 30)
    return min(base * 2**exponent, cap)


def retry_delay(job: Job, error: BaseException) -> int | None:
    """Seconds until the next attempt, or None when the job must end as failed."""
    if isinstance(error, PermanentJobError) or job.is_last_attempt:
        return None
    retry_in = getattr(error, "retry_in", None)
    if isinstance(retry_in, int) and retry_in > 0:
        return retry_in
    return backoff_seconds(job.attempts)


def safe_error(message: str, secrets: Iterable[str | None] = (), limit: int = ERROR_LIMIT) -> str:
    """Remove known secrets from a message and cap its length."""
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[oculto]")
    return message[:limit]
