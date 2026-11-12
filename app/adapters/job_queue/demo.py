"""Stand-in for queue and worker when there is no database (MemoryStore) and Google is fake.

Enqueuing a sync_profile writes the demo summary and the demo reviews straight into the
in-memory store, with the same fake profile the real worker uses under
FICHAITOR_FAKE_GOOGLE. Enqueuing a reply_review carries out the action at once, as the
executor would: the review becomes answered and nothing leaves the process. With a
database, the real queue and the worker process are used instead.
"""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from app.adapters.google_profile.fake import demo_hours, demo_reviews
from app.adapters.store.memory import MemoryStore
from app.domain.actions import REPLY_REVIEW
from app.domain.jobs import DEFAULT_MAX_ATTEMPTS
from app.domain.profile_summary import STATUS_OK, summarize
from app.ports.store import StoredSummary


class DemoSyncQueue:
    def __init__(self, store: MemoryStore, clock: Callable[[], float]) -> None:
        self._store = store
        self._clock = clock
        self._keys: set[str] = set()

    async def enqueue(
        self,
        kind: str,
        payload: dict,
        *,
        idempotency_key: str | None = None,
        run_at: datetime | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> bool:
        if idempotency_key is not None:
            if idempotency_key in self._keys:
                return False
            self._keys.add(idempotency_key)
        tables = self._store.tables
        if kind == REPLY_REVIEW:
            now = datetime.fromtimestamp(self._clock(), UTC)
            action_id = payload.get("action_id")
            if isinstance(action_id, str) and tables.claim_action(action_id, "demo", now):
                tables.finish_action(action_id, "demo", done=True, at=now, google_status=200)
        if kind == "sync_profile":
            now = datetime.fromtimestamp(self._clock(), UTC)
            key = (payload["google_sub"], payload["location"])
            # The demo profile, with the replies sent from this process on top.
            sent = {r.gbp_review_id: r for r in tables.reviews_of(*key) if r.sent_at is not None}
            demo = demo_reviews(now)
            demo = replace(
                demo,
                reviews=tuple(
                    replace(
                        r,
                        replied=True,
                        reply_text=sent[r.review_id].reply_text,
                        reply_at=sent[r.review_id].reply_at,
                    )
                    if r.review_id in sent
                    else r
                    for r in demo.reviews
                ),
            )
            summary = summarize(demo, demo_hours(now.date()), (), now.date())
            self._store.put_summary(
                payload["google_sub"],
                payload["location"],
                StoredSummary(
                    status=STATUS_OK,
                    status_at=now,
                    synced_at=now,
                    review_count=summary.review_count,
                    average_rating=summary.average_rating,
                    unanswered_count=summary.unanswered_count,
                    unanswered_low_count=summary.unanswered_low_count,
                    last_review_at=summary.last_review_at,
                    has_regular_hours=summary.has_regular_hours,
                    upcoming_special_hours=summary.upcoming_special_hours,
                    google_changed=summary.google_changed,
                    google_changed_fields=summary.google_changed_fields,
                ),
            )
            tables.save_reviews(*key, demo.reviews, now)
        return True
