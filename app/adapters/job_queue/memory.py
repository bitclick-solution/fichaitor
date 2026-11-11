"""In-memory job queue for tests. Same rules as the PostgreSQL one, single process."""

import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.domain.jobs import DEFAULT_MAX_ATTEMPTS, DONE, ERROR_LIMIT, FAILED, PENDING, RUNNING, Job

LOST_WORKER_ERROR = "el worker que lo ejecutaba dejó de responder"


@dataclass
class StoredJob:
    id: str
    kind: str
    payload: dict
    run_at: datetime
    max_attempts: int
    idempotency_key: str | None = None
    state: str = PENDING
    attempts: int = 0
    locked_by: str | None = None
    locked_at: datetime | None = None
    last_error: str | None = None
    finished_at: datetime | None = None
    seq: int = field(default=0)


class MemoryJobQueue:
    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._clock = clock
        self._seq = itertools.count(1)
        self.jobs: dict[str, StoredJob] = {}

    async def enqueue(
        self,
        kind: str,
        payload: dict,
        *,
        idempotency_key: str | None = None,
        run_at: datetime | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> bool:
        if idempotency_key is not None and any(
            j.idempotency_key == idempotency_key for j in self.jobs.values()
        ):
            return False
        seq = next(self._seq)
        self.jobs[str(seq)] = StoredJob(
            id=str(seq),
            kind=kind,
            payload=dict(payload),
            run_at=run_at or self._clock(),
            max_attempts=max_attempts,
            idempotency_key=idempotency_key,
            seq=seq,
        )
        return True

    async def claim(self, worker_id: str, kinds: Sequence[str], lock_timeout: float) -> Job | None:
        now = self._clock()
        stale_before = now - timedelta(seconds=lock_timeout)
        due: list[StoredJob] = []
        for job in self.jobs.values():
            if job.kind not in kinds:
                continue
            stale = job.state == RUNNING and job.locked_at is not None
            stale = stale and job.locked_at < stale_before
            if stale and job.attempts >= job.max_attempts:
                job.state, job.finished_at, job.last_error = FAILED, now, LOST_WORKER_ERROR
                job.locked_by = job.locked_at = None
            elif stale or (job.state == PENDING and job.run_at <= now):
                due.append(job)
        if not due:
            return None
        job = min(due, key=lambda j: (j.run_at, j.seq))
        job.state, job.locked_by, job.locked_at = RUNNING, worker_id, now
        job.attempts += 1
        return Job(job.id, job.kind, dict(job.payload), job.attempts, job.max_attempts)

    def _held(self, job: Job, worker_id: str) -> StoredJob | None:
        stored = self.jobs.get(job.id)
        if (
            stored
            and stored.state == RUNNING
            and stored.locked_by == worker_id
            and stored.attempts == job.attempts
        ):
            return stored
        return None

    async def complete(self, job: Job, worker_id: str) -> bool:
        stored = self._held(job, worker_id)
        if stored is None:
            return False
        stored.state, stored.finished_at, stored.last_error = DONE, self._clock(), None
        stored.locked_by = stored.locked_at = None
        return True

    async def fail(self, job: Job, worker_id: str, error: str, retry_in: float | None) -> bool:
        stored = self._held(job, worker_id)
        if stored is None:
            return False
        now = self._clock()
        stored.last_error = error[:ERROR_LIMIT]
        stored.locked_by = stored.locked_at = None
        if retry_in is None:
            stored.state, stored.finished_at = FAILED, now
        else:
            stored.state, stored.run_at = PENDING, now + timedelta(seconds=retry_in)
        return True
