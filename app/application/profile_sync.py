"""Panel summary: read what the worker wrote and ask it for a new sync when needed.

The API never talks to Google here and never writes a summary. Its only lever is to
enqueue a sync_profile job; the worker does the rest.
"""

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.domain.profile_summary import STATUS_OK
from app.ports.job_queue import JobEnqueuer
from app.ports.store import Store, StoredSummary

log = logging.getLogger("fichaitor.profile_sync")

SYNC_PROFILE_KIND = "sync_profile"  # same value as app.worker.sync_profile.KIND


def sync_key(google_sub: str, location: str, now: float, window_seconds: int) -> str:
    """One key per profile and time window: an idempotency key is unique forever, so the
    window is what lets the same profile be synced again later.

    The profile is hashed: the key has a length limit and needs no identifiers in clear.
    """
    profile = hashlib.sha256(f"{google_sub}\n{location}".encode()).hexdigest()[:32]
    return f"{SYNC_PROFILE_KIND}:{profile}:{int(now // window_seconds)}"


@dataclass(frozen=True)
class SummaryView:
    summary: StoredSummary | None  # None: nothing written yet, a sync has been requested
    stale: bool  # the figures are old and a sync has been requested


class ProfileSync:
    def __init__(
        self,
        store: Store,
        jobs: JobEnqueuer,
        *,
        window_seconds: int,
        stale_after_seconds: float,
        clock: Callable[[], float],
    ) -> None:
        self._store = store
        self._jobs = jobs
        self._window = max(1, int(window_seconds))
        self._stale_after = timedelta(seconds=stale_after_seconds)
        self._clock = clock

    async def _enqueue(self, google_sub: str, location: str) -> None:
        """Ask for a sync. Never raises: the route of the user must not fail because of it."""
        try:
            await self._jobs.enqueue(
                SYNC_PROFILE_KIND,
                {"google_sub": google_sub, "location": location},
                idempotency_key=sync_key(google_sub, location, self._clock(), self._window),
            )
        except Exception as exc:  # identifiers and connection strings stay out of the logs
            log.warning("sincronización de la ficha no encolada: %s", type(exc).__name__)

    async def request_for_account(self, account_id: str) -> None:
        """Sync the selected profile of that account. Never raises."""
        try:
            profile = await self._store.selected_profile(account_id)
        except Exception as exc:
            log.warning("sincronización de la ficha no encolada: %s", type(exc).__name__)
            return
        if profile is not None:
            await self._enqueue(profile.google_sub, profile.location)

    async def view(self, account_id: str) -> SummaryView | None:
        """The summary of the selected profile of that account. None if none is selected."""
        profile = await self._store.selected_profile(account_id)
        if profile is None:
            return None
        summary = profile.summary
        if summary is None:
            await self._enqueue(profile.google_sub, profile.location)
            return SummaryView(None, stale=False)

        now = datetime.fromtimestamp(self._clock(), UTC)
        old = summary.synced_at is not None and now - summary.synced_at > self._stale_after
        if summary.status == STATUS_OK:
            refresh = old or summary.synced_at is None
        else:
            # A failed attempt is retried once its window has passed, so that a
            # reconnection or a recovered Google ends up showing in the panel.
            refresh = now - summary.status_at > timedelta(seconds=self._window)
        if refresh:
            await self._enqueue(profile.google_sub, profile.location)
        return SummaryView(summary, stale=old)
