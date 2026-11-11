"""Durable job queue. The API only enqueues; the worker claims and finishes."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from app.domain.jobs import DEFAULT_MAX_ATTEMPTS, Job


class JobEnqueuer(Protocol):
    async def enqueue(
        self,
        kind: str,
        payload: dict,
        *,
        idempotency_key: str | None = None,
        run_at: datetime | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> bool:
        """True if a job was created; False if the idempotency key already existed."""
        ...


class JobQueue(JobEnqueuer, Protocol):
    async def claim(self, worker_id: str, kinds: Sequence[str], lock_timeout: float) -> Job | None:
        """Take the next due job of the given kinds, or one whose worker died
        (locked for longer than lock_timeout seconds). No two callers get the same job."""
        ...

    async def complete(self, job: Job, worker_id: str) -> bool:
        """Mark as done. False if this worker no longer holds the job."""
        ...

    async def fail(self, job: Job, worker_id: str, error: str, retry_in: float | None) -> bool:
        """Record the error. retry_in seconds schedules another attempt; None ends it
        as failed. False if this worker no longer holds the job."""
        ...
