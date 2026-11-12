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


def _profile_hash(google_sub: str, location: str) -> str:
    return hashlib.sha256(f"{google_sub}\n{location}".encode()).hexdigest()[:32]


def sync_key(google_sub: str, location: str, now: float, window_seconds: int) -> str:
    """One key per profile and time window: an idempotency key is unique forever, so the
    window is what lets the same profile be synced again later.

    The profile is hashed: the key has a length limit and needs no identifiers in clear.
    """
    return f"{SYNC_PROFILE_KIND}:{_profile_hash(google_sub, location)}:{int(now // window_seconds)}"


# A reconnection asks for a sync at once, whatever the window says. Its key has its own
# prefix, so it never meets the key of a window, and its own short period, so connecting
# again and again cannot enqueue more than one job per profile and minute.
RECONNECT_WINDOW_SECONDS = 60


def reconnect_key(google_sub: str, location: str, now: float) -> str:
    """Key of the sync asked for when the owner connects with Google again."""
    profile = _profile_hash(google_sub, location)
    return f"{SYNC_PROFILE_KIND}:reconnect:{profile}:{int(now // RECONNECT_WINDOW_SECONDS)}"


@dataclass(frozen=True)
class SummaryView:
    summary: StoredSummary | None  # None: nothing written yet, a sync has been requested
    stale: bool  # the figures are old and a sync has been requested
    title: str = ""  # of the selected profile, as the connection listed it
    place_id: str | None = None


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

    async def _enqueue(self, google_sub: str, location: str, *, reconnected: bool = False) -> None:
        """Ask for a sync. Never raises: the route of the user must not fail because of it."""
        try:
            if reconnected:
                key = reconnect_key(google_sub, location, self._clock())
            else:
                key = sync_key(google_sub, location, self._clock(), self._window)
            await self._jobs.enqueue(
                SYNC_PROFILE_KIND,
                {"google_sub": google_sub, "location": location},
                idempotency_key=key,
            )
        except Exception as exc:  # identifiers and connection strings stay out of the logs
            log.warning("sincronización de la ficha no encolada: %s", type(exc).__name__)

    async def request_for_account(self, account_id: str, *, reconnected: bool = False) -> None:
        """Sync the selected profile of that account, if it has one. Never raises.

        reconnected: the owner has just connected with Google again, so the sync is
        asked for at once instead of once per window.
        """
        try:
            profile = await self._store.selected_profile(account_id)
        except Exception as exc:
            log.warning("sincronización de la ficha no encolada: %s", type(exc).__name__)
            return
        if profile is not None:
            await self._enqueue(profile.google_sub, profile.location, reconnected=reconnected)

    async def view(self, account_id: str, *, need_reviews: bool = False) -> SummaryView | None:
        """The summary of the selected profile of that account. None if none is selected.

        need_reviews: the caller is going to list the reviews, so a summary whose
        reviews were never stored (synced before they were kept) is synced again.
        """
        profile = await self._store.selected_profile(account_id)
        if profile is None:
            return None
        summary = profile.summary
        if summary is None:
            await self._enqueue(profile.google_sub, profile.location)
            return SummaryView(None, False, profile.title, profile.place_id)

        now = datetime.fromtimestamp(self._clock(), UTC)
        old = summary.synced_at is not None and now - summary.synced_at > self._stale_after
        if summary.status == STATUS_OK:
            # A summary written before the facts existed is synced again to get them.
            refresh = old or summary.synced_at is None or summary.facts is None
            refresh = refresh or (need_reviews and summary.reviews_synced_at is None)
        else:
            # A failed attempt is retried once its window has passed, so that a
            # reconnection or a recovered Google ends up showing in the panel.
            refresh = now - summary.status_at > timedelta(seconds=self._window)
        if refresh:
            await self._enqueue(profile.google_sub, profile.location)
        return SummaryView(summary, old, profile.title, profile.place_id)
