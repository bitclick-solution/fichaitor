"""The worker loop: take a job, run its handler, record the outcome."""

import asyncio
import logging
import time
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import time as day_time

from app.domain.jobs import Job, JobError, PermanentJobError, retry_delay, safe_error
from app.ports.job_queue import JobQueue
from app.worker.log import event
from app.worker.registry import HandlerRegistry

TIMEOUT_ERROR = "tiempo máximo de ejecución superado"


@dataclass(frozen=True)
class DailyJob:
    """A job the worker enqueues for itself once a day. There is no scheduler process:
    every worker tries to enqueue today's job and the idempotency key (`<kind>:<day>`)
    lets only one of them succeed, so any number of workers run it once. A worker that
    starts after the hour still gets today's job, which is then already due."""

    kind: str
    hour_utc: int = 3
    max_attempts: int = 3

    def key(self, day: date) -> str:
        return f"{self.kind}:{day.isoformat()}"

    def run_at(self, day: date) -> datetime:
        return datetime.combine(day, day_time(self.hour_utc), UTC)


class Worker:
    def __init__(
        self,
        queue: JobQueue,
        registry: HandlerRegistry,
        *,
        worker_id: str,
        poll_interval: float = 2.0,
        job_timeout: float = 120.0,
        lock_timeout: float = 300.0,
        recover_interval: float = 60.0,
        daily: Sequence[DailyJob] = (),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._queue = queue
        self._registry = registry
        self._worker_id = worker_id
        self._poll_interval = poll_interval
        self._job_timeout = job_timeout
        self._lock_timeout = lock_timeout
        self._recover_interval = recover_interval
        self._next_recovery = 0.0  # monotonic clock: the first round always looks
        self._daily = tuple(daily)
        self._clock = clock
        self._scheduled: set[str] = set()  # keys this process already tried today
        self._next_schedule = 0.0

    async def run(self, stop: asyncio.Event) -> None:
        """Run jobs until stop is set. The job in progress is always finished first."""
        event("worker_started", worker_id=self._worker_id, kinds=list(self._registry.kinds))
        while not stop.is_set():
            try:
                worked = await self.run_once()
            except Exception as exc:  # the database is down, most likely: keep trying
                event("queue_error", logging.ERROR, error=type(exc).__name__)
                worked = False
            if not worked:
                await _sleep_until_stop(stop, self._poll_interval)
        event("worker_stopped", worker_id=self._worker_id)

    async def run_once(self) -> bool:
        """Take and run one job. False if there was nothing to do."""
        await self._schedule()
        job = await self._queue.claim(self._worker_id, self._registry.kinds, self._lock_timeout)
        # After claim(), which is what closes the jobs abandoned on their last attempt.
        await self._record_lost()
        if job is None:
            return False
        fields = {"job_id": job.id, "kind": job.kind, "attempt": job.attempts}
        event("job_started", **fields)

        try:
            async with asyncio.timeout(self._job_timeout):
                await self._registry.get(job.kind)(job)
        except Exception as exc:
            await self._failed(job, exc, fields)
        else:
            held = await self._queue.complete(job, self._worker_id)
            event("job_done" if held else "job_lost", **fields)
        return True

    async def _failed(self, job: Job, exc: Exception, fields: dict) -> None:
        error = _describe(exc)
        delay = retry_delay(job, exc)
        if delay is None and not isinstance(exc, PermanentJobError):
            await self._notify_exhausted(job, fields)
        held = await self._queue.fail(job, self._worker_id, error, delay)
        if not held:
            event("job_lost", logging.WARNING, **fields)
        elif delay is None:
            event("job_failed", logging.ERROR, error=error, **fields)
        else:
            event("job_retry", logging.WARNING, error=error, retry_in=delay, **fields)

    async def _schedule(self) -> None:
        """Enqueue today's daily jobs, once per process and day. Looked at once per
        recover_interval; a failure here never stops the jobs and is tried again."""
        if not self._daily:
            return
        tick = time.monotonic()
        if tick < self._next_schedule:
            return
        self._next_schedule = tick + self._recover_interval
        today = self._clock().astimezone(UTC).date()
        for daily in self._daily:
            key = daily.key(today)
            if key in self._scheduled:
                continue
            try:
                created = await self._queue.enqueue(
                    daily.kind,
                    {},
                    idempotency_key=key,
                    run_at=daily.run_at(today),
                    max_attempts=daily.max_attempts,
                )
            except Exception as exc:
                event("schedule_error", logging.ERROR, error=type(exc).__name__, kind=daily.kind)
                continue
            self._scheduled = {k for k in self._scheduled if not k.startswith(f"{daily.kind}:")}
            self._scheduled.add(key)
            if created:
                event("daily_job_enqueued", kind=daily.kind, day=today.isoformat())

    async def _record_lost(self) -> None:
        """Let each handler record the jobs of its kind that died with their worker.
        At most once per recover_interval; a failure here never stops the jobs."""
        now = time.monotonic()
        if now < self._next_recovery:
            return
        self._next_recovery = now + self._recover_interval
        for kind in self._registry.kinds:
            hook = getattr(self._registry.get(kind), "on_lost", None)
            if hook is None:
                continue
            try:
                async with asyncio.timeout(self._job_timeout):
                    count = await hook(kind)
            except Exception as exc:
                event("lost_hook_error", logging.ERROR, error=type(exc).__name__, kind=kind)
            else:
                if count:
                    event("lost_jobs_recorded", logging.WARNING, kind=kind, count=count)

    async def _notify_exhausted(self, job: Job, fields: dict) -> None:
        hook = getattr(self._registry.get(job.kind), "on_exhausted", None)
        if hook is None:
            return
        try:
            async with asyncio.timeout(self._job_timeout):
                await hook(job)
        except Exception as exc:
            event("exhausted_hook_error", logging.ERROR, error=type(exc).__name__, **fields)


def _describe(exc: Exception) -> str:
    """What is stored in job.last_error and logged.

    A JobError carries a message written by a handler that knows it is safe.
    Anything else may quote data we do not control (a URL, an SQL value, a
    token), so only its type and the place where it was raised are kept.
    """
    if isinstance(exc, JobError):
        return safe_error(str(exc))
    if isinstance(exc, TimeoutError):
        return TIMEOUT_ERROR
    frames = traceback.extract_tb(exc.__traceback__)
    where = f" en {frames[-1].name}:{frames[-1].lineno}" if frames else ""
    return safe_error(f"error inesperado: {type(exc).__name__}{where}")


async def _sleep_until_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        async with asyncio.timeout(seconds):
            await stop.wait()
    except TimeoutError:
        pass
