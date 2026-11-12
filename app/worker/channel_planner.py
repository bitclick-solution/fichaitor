"""The planner of the conversation process (docs/canales.md, step 6).

Today a profile is only synced when its owner opens the panel: without a planner there
would be no new reviews to tell anybody about. Every minute it enqueues, with an
idempotency key per slot, so that any number of processes enqueue each job once:

- `conversation_tick`, every 5 minutes: key `conversation_tick:<slot>`;
- `sync_profile` of every ACTIVE profile of an account with a linked address that sees
  it, every 30 minutes from 8:30 to 21:00 (local) and every 3 hours at night: key
  `sync:<google_sub>:<location>:<slot>`. Each one runs at its own moment of the slot
  (`spread_run_at`), so that many profiles do not all call Google in the same minute
  (docs/multinegocio.md, 7).

- `plan_key_dates`, when given its kind, once a local day, to run at 9:05: key
  `plan_key_dates:<date>`.

A profile with a scheduled leave is not synced (the worker would skip it anyway).
"""

import asyncio
import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo

from app.domain import channel_jobs
from app.domain.conversation import DEFAULT_TIMEZONE, timezone_for
from app.ports.conversation_store import LinkedProfiles
from app.ports.job_queue import JobEnqueuer
from app.worker import log

SYNC_KIND = "sync_profile"
SYNC_MAX_ATTEMPTS = 3
PLAN_EVERY_SECONDS = 60.0


def spread_run_at(google_sub: str, location: str, slot: str) -> datetime:
    """When the sync of that profile runs within `slot` (`channel_jobs.sync_slot`): the
    start of the slot plus a fixed offset of that profile, below the length of the slot.
    The same profile always gets the same offset; different ones spread over the slot."""
    every = channel_jobs.SYNC_DAY_EVERY if slot.startswith("d") else channel_jobs.SYNC_NIGHT_EVERY
    seconds = int(every.total_seconds())
    start = datetime.fromtimestamp(int(slot[1:]) * seconds, UTC)
    digest = hashlib.sha256(f"{google_sub}\n{location}".encode()).digest()
    return start + timedelta(seconds=int.from_bytes(digest[:8], "big") % seconds)


class ChannelPlanner:
    def __init__(
        self,
        jobs: JobEnqueuer,
        profiles: LinkedProfiles,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        zone: tzinfo | None = None,
        sync_kind: str = SYNC_KIND,
        tick_kind: str = channel_jobs.CONVERSATION_TICK,
        plan_kind: str | None = None,
    ) -> None:
        self._jobs = jobs
        self._profiles = profiles
        self._clock = clock
        # The zone of the accounts does not exist yet: the peninsula for every one.
        self._zone = zone or timezone_for(DEFAULT_TIMEZONE)
        self._sync_kind = sync_kind
        self._tick_kind = tick_kind
        # The kind of the daily planning of key dates; None: not enqueued.
        self._plan_kind = plan_kind

    async def plan_once(self) -> dict[str, int]:
        """Enqueue what is due now. Returns how many jobs were created of each kind."""
        now = self._clock()
        created = {"conversation_tick": 0, "sync_profile": 0}
        if await self._jobs.enqueue(
            self._tick_kind,
            {},
            idempotency_key=channel_jobs.tick_key(now).replace(
                channel_jobs.CONVERSATION_TICK, self._tick_kind, 1
            ),
            max_attempts=channel_jobs.TICK_MAX_ATTEMPTS,
        ):
            created["conversation_tick"] += 1
        slot = channel_jobs.sync_slot(now, self._zone)
        for google_sub, location in await self._profiles.linked_profiles():
            key = channel_jobs.sync_key(google_sub, location, slot)
            if self._sync_kind != SYNC_KIND:
                key = f"{self._sync_kind}:{key}"
            if await self._jobs.enqueue(
                self._sync_kind,
                {"google_sub": google_sub, "location": location},
                idempotency_key=key[:200],
                run_at=spread_run_at(google_sub, location, slot),
                max_attempts=SYNC_MAX_ATTEMPTS,
            ):
                created["sync_profile"] += 1
        if self._plan_kind is not None:
            today = now.astimezone(self._zone).date()
            created["plan_key_dates"] = int(
                await self._jobs.enqueue(
                    self._plan_kind,
                    {},
                    idempotency_key=channel_jobs.plan_key_dates_key(today).replace(
                        channel_jobs.PLAN_KEY_DATES, self._plan_kind, 1
                    ),
                    run_at=channel_jobs.plan_key_dates_run_at(now, self._zone),
                    max_attempts=channel_jobs.PLAN_MAX_ATTEMPTS,
                )
            )
        return created

    async def run(self, stop: asyncio.Event, every: float = PLAN_EVERY_SECONDS) -> None:
        """Plan until stop is set. A failure (the database is down) is logged and tried
        again on the next round; it never stops the process."""
        while not stop.is_set():
            try:
                created = await self.plan_once()
                if any(created.values()):
                    log.event("planner_enqueued", **created)
            except Exception as exc:
                log.event("planner_error", logging.ERROR, error=type(exc).__name__)
            try:
                async with asyncio.timeout(every):
                    await stop.wait()
            except TimeoutError:
                pass
