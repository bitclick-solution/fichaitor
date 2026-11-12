"""Job retention: the daily pass that deletes what is past its term.

Payload: {}. The worker enqueues it for itself once a day (see `DailyJob` in
app/worker/loop.py) with a key per day, so several workers enqueue one job between
them. Running it twice deletes nothing more: everything is decided from the clock and
from what is in the database.

What it does:

1. Makes effective the scheduled leaves whose job was lost, and finishes revocations
   that were left half done (after a day, giving up on Google).
2. Deletes, physically, everything past its term (docs/retencion.md has the table).
3. Records how many rows went from each table: in `retention_run` and in the log.
   Counts only, never an identifier.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.domain.jobs import Job
from app.domain.offboarding import RetentionPolicy
from app.ports.worker_store import WorkerStore
from app.worker import log
from app.worker.apply_offboarding import ApplyOffboarding

KIND = "retention"
MAX_ATTEMPTS = 3
# Without a pass for this long, the worker warns in the log (B10 of
# docs/seguridad/revision-2026-11-12-b.md): a day plus the margin of its retries.
OVERDUE_AFTER = timedelta(hours=36)


class Retention:
    def __init__(
        self,
        store: WorkerStore,
        apply: ApplyOffboarding,
        policy: RetentionPolicy,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._apply = apply
        self._policy = policy
        self._clock = clock

    async def __call__(self, job: Job) -> None:
        now = self._clock()
        applied = 0
        for offboarding_id, give_up in await self._store.due_offboardings(now):
            try:
                if await self._apply.apply(offboarding_id, give_up=give_up) is not None:
                    applied += 1
            except Exception as exc:  # one leave must not stop the rest of the pass
                log.event(
                    "offboarding_not_applied",
                    logging.ERROR,
                    offboarding_id=offboarding_id,
                    error=type(exc).__name__,
                )
        deleted = await self._store.run_retention(now, self._policy)
        log.event("retention_done", job_id=job.id, offboardings_applied=applied, deleted=deleted)


class RetentionWatch:
    """Warns in the log (`retention_overdue`, level warning) while the daily pass has
    not finished for OVERDUE_AFTER: its job may have been blocked (an idempotency key
    taken in advance, B10), or keep failing. Without any pass yet, it counts from the
    start of this process. Run by the worker loop every few minutes; it never raises
    into the loop."""

    def __init__(
        self,
        store: WorkerStore,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        overdue_after: timedelta = OVERDUE_AFTER,
    ) -> None:
        self._store = store
        self._clock = clock
        self._overdue_after = overdue_after
        self._started = clock()

    async def __call__(self) -> bool:
        """True if it warned."""
        now = self._clock()
        last = await self._store.last_retention_at()
        since = last or self._started
        if now - since <= self._overdue_after:
            return False
        log.event(
            "retention_overdue",
            logging.WARNING,
            hours=int((now - since).total_seconds() // 3600),
            never=last is None,
        )
        return True
