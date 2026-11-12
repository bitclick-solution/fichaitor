"""In-memory worker store for tests."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from app.adapters.job_queue.memory import LOST_WORKER_ERROR, MemoryJobQueue
from app.adapters.memory_tables import MemoryTables
from app.domain.actions import ERROR_UNAVAILABLE, SET_SPECIAL_HOURS
from app.domain.jobs import FAILED
from app.domain.profile_summary import (
    LOW_RATING_MAX,
    STATUS_ERROR,
    STATUS_NEEDS_RECONNECT,
    STATUS_OK,
    ProfileSummary,
    Review,
)
from app.domain.sentiment import Analysis, SentimentReview, StoredClassification, Usage
from app.ports.sentiment_store import SentimentContext
from app.ports.worker_store import ClaimedAction, NewReview, StoredConnection


@dataclass
class StoredSummary:
    status: str
    status_at: datetime
    synced_at: datetime | None = None
    summary: ProfileSummary | None = None


class MemoryWorkerStore:
    def __init__(
        self, queue: MemoryJobQueue | None = None, tables: MemoryTables | None = None
    ) -> None:
        self._queue = queue  # only the mark_lost_* read it, as the real one reads `job`
        # Reviews, actions and audit: shared with a MemoryStore when one is given.
        self.tables = tables if tables is not None else MemoryTables()
        self.connections: dict[str, list[StoredConnection]] = {}
        # Connections revoked by the owner at Google: (connection id, when), by user.
        self.revoked: dict[str, list[tuple[str, datetime]]] = {}
        self.summaries: dict[tuple[str, str], StoredSummary] = {}
        self.categories: dict[tuple[str, str], str] = {}  # set by tests and the demo
        self.suspended: set[str] = set()  # Google users with a scheduled leave
        self.denied: dict[tuple[str, str], int] = {}  # refusals in a row, by profile
        # Profiles retired for lack of access (the real store sets location.removed_at).
        self.lost_access: set[tuple[str, str]] = set()
        # (account, location) whose action must not pass the M1 check: the accounts are
        # not in these tables, so a test says which ones the real join would refuse.
        self.not_owned: set[tuple[str, str]] = set()
        # (account, location) no longer in use in its account (multinegocio): a test says
        # which profiles the real store would find deactivated.
        self.inactive: set[tuple[str, str]] = set()

    # --- sentiment -------------------------------------------------------------

    async def sentiment_context(self, google_sub: str, location: str) -> SentimentContext:
        stored = self.summaries.get((google_sub, location))
        facts = stored.summary.facts if stored and stored.summary else None
        category = self.categories.get((google_sub, location)) or (facts.category if facts else "")
        return SentimentContext(self.tables.last_analysis(google_sub, location), category)

    async def sentiment_reviews(self, google_sub: str, location: str) -> list[SentimentReview]:
        return self.tables.sentiment_reviews(google_sub, location)

    async def save_classifications(
        self, classified: Mapping[str, StoredClassification], at: datetime
    ) -> None:
        self.tables.classifications.update(classified)

    async def save_analysis(
        self, google_sub: str, location: str, analysis: Analysis, usage: Usage
    ) -> None:
        self.tables.save_analysis(google_sub, location, analysis, usage)

    async def save_analysis_error(self, google_sub: str, location: str, at: datetime) -> None:
        self.tables.save_analysis_error(google_sub, location, at)

    # --- connections, summaries, reviews and actions ---------------------------

    def add_connection(self, google_sub: str, connection: StoredConnection) -> None:
        self.connections.setdefault(google_sub, []).append(connection)

    async def latest_connection(self, google_sub: str) -> StoredConnection | None:
        history = self.connections.get(google_sub)
        return history[-1] if history else None

    async def revoke_connection(self, google_sub: str, connection_id: str, at: datetime) -> bool:
        # The real store keeps the row without its token; here it moves to `revoked`.
        history = self.connections.get(google_sub, [])
        used = [i for i, c in enumerate(history) if c.connection_id == connection_id]
        if used:
            self.revoked.setdefault(google_sub, []).append((history[used[-1]].connection_id, at))
            self.connections[google_sub] = history[used[-1] + 1 :]
        return not self.connections.get(google_sub)

    async def drop_replaced_connections(self, google_sub: str, connection_id: str) -> int:
        history = self.connections.get(google_sub, [])
        used = [i for i, c in enumerate(history) if c.connection_id == connection_id]
        if not used:
            return 0
        self.connections[google_sub] = history[used[-1] :]
        return used[-1]

    async def save_summary(
        self, google_sub: str, location: str, summary: ProfileSummary, at: datetime
    ) -> None:
        self.summaries[(google_sub, location)] = StoredSummary(STATUS_OK, at, at, summary)
        self.denied.pop((google_sub, location), None)

    async def record_denied(
        self, google_sub: str, location: str, at: datetime, lost_after: int
    ) -> bool:
        key = (google_sub, location)
        await self.save_status(google_sub, location, STATUS_ERROR, at)
        self.denied[key] = self.denied.get(key, 0) + 1
        if self.denied[key] < lost_after:
            return False
        self.denied[key] = 0
        self.summaries[key].status = STATUS_NEEDS_RECONNECT
        self.lost_access.add(key)
        return True

    async def save_status(self, google_sub: str, location: str, status: str, at: datetime) -> None:
        current = self.summaries.get((google_sub, location))
        if current is None:
            self.summaries[(google_sub, location)] = StoredSummary(status, at)
        else:
            current.status, current.status_at = status, at

    async def mark_lost_syncs(self, kind: str) -> int:
        marked = 0
        for job in list(self._queue.jobs.values()) if self._queue else []:
            if not (job.kind == kind and job.state == FAILED and job.finished_at):
                continue
            if job.last_error != LOST_WORKER_ERROR:
                continue
            google_sub, location = job.payload.get("google_sub"), job.payload.get("location")
            if not (isinstance(google_sub, str) and google_sub):
                continue
            if not (isinstance(location, str) and location):
                continue
            current = self.summaries.get((google_sub, location))
            if current is None or current.status_at < job.finished_at:
                await self.save_status(google_sub, location, STATUS_ERROR, job.finished_at)
                marked += 1
        return marked

    async def save_reviews(
        self, google_sub: str, location: str, reviews: Sequence[Review], at: datetime
    ) -> list[NewReview]:
        known = set(self.tables.reviews)
        self.tables.save_reviews(google_sub, location, reviews, at)
        return [
            NewReview(row.id, row.created_at)
            for row in self.tables.reviews.values()
            if row.id not in known and row.reply_text is None
        ]

    async def recent_replies(
        self, google_sub: str, location: str, since: datetime
    ) -> dict[str, tuple[str, datetime | None]]:
        return self.tables.recent_replies(google_sub, location, since)

    async def claim_action(self, action_id: str, job_id: str, at: datetime) -> ClaimedAction | None:
        action = self.tables.claim_action(action_id, job_id, at)
        if action is None:
            return None
        # The accounts are not in these tables: only the review (or the day) side of
        # the check, plus the profiles a test marks as not owned.
        review = self.tables.coherent_review(action)
        if action.type == SET_SPECIAL_HOURS:
            target = action.special_date is not None
        else:
            target = review is not None
        owned = (action.account_id, action.location) not in self.not_owned
        return ClaimedAction(
            id=action.id,
            google_sub=action.google_sub,
            location=action.location,
            gbp_review_id=review.gbp_review_id if review is not None else "",
            text=action.text,
            attempts=action.attempts,
            coherent=target and owned,
            type=action.type,
            special_date=action.special_date,
            active=(action.account_id, action.location) not in self.inactive,
        )

    async def finish_action(
        self,
        action_id: str,
        job_id: str,
        *,
        done: bool,
        at: datetime,
        google_status: int | None = None,
        reason: str | None = None,
        reply_at: datetime | None = None,
    ) -> bool:
        action = self.tables.actions.get(action_id)
        review = self.tables.reviews.get(action.review_id) if action is not None else None
        was_pending = review is not None and review.reply_text is None
        closed = self.tables.finish_action(
            action_id,
            job_id,
            done=done,
            at=at,
            google_status=google_status,
            reason=reason,
            reply_at=reply_at,
        )
        current = self.summaries.get((action.google_sub, action.location)) if closed else None
        if done and was_pending and current is not None and current.summary is not None:
            # One review less to answer; the next sync brings Google's own figures.
            low = review.rating is not None and review.rating <= LOW_RATING_MAX
            current.summary = replace(
                current.summary,
                unanswered_count=max(current.summary.unanswered_count - 1, 0),
                unanswered_low_count=max(current.summary.unanswered_low_count - int(low), 0),
            )
        return closed

    async def mark_lost_actions(self, kind: str) -> int:
        closed = 0
        for job in list(self._queue.jobs.values()) if self._queue else []:
            if not (job.kind == kind and job.state == FAILED and job.finished_at):
                continue
            action_id = job.payload.get("action_id")
            if isinstance(action_id, str) and self.tables.finish_action(
                action_id, None, done=False, at=job.finished_at, reason=ERROR_UNAVAILABLE
            ):
                closed += 1
        return closed

    # --- leaving ---------------------------------------------------------------
    # Only what the handlers that run on this store need. Making a leave effective and
    # the retention task exist in PostgreSQL only (tests/test_offboarding_postgres.py).

    async def is_suspended(self, google_sub: str) -> bool:
        return google_sub in self.suspended

    async def purge_superseded(self, google_sub: str, now: datetime) -> dict[str, int]:
        return {}
