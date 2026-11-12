"""In-memory worker store for tests."""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from app.adapters.job_queue.memory import LOST_WORKER_ERROR, MemoryJobQueue
from app.adapters.memory_tables import MemoryTables
from app.domain.actions import ERROR_UNAVAILABLE
from app.domain.jobs import FAILED
from app.domain.profile_summary import (
    LOW_RATING_MAX,
    STATUS_ERROR,
    STATUS_OK,
    ProfileSummary,
    Review,
)
from app.ports.worker_store import ClaimedAction, StoredConnection


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
        self.summaries: dict[tuple[str, str], StoredSummary] = {}

    def add_connection(self, google_sub: str, connection: StoredConnection) -> None:
        self.connections.setdefault(google_sub, []).append(connection)

    async def latest_connection(self, google_sub: str) -> StoredConnection | None:
        history = self.connections.get(google_sub)
        return history[-1] if history else None

    async def save_summary(
        self, google_sub: str, location: str, summary: ProfileSummary, at: datetime
    ) -> None:
        self.summaries[(google_sub, location)] = StoredSummary(STATUS_OK, at, at, summary)

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
    ) -> None:
        self.tables.save_reviews(google_sub, location, reviews, at)

    async def recent_replies(
        self, google_sub: str, location: str, since: datetime
    ) -> dict[str, tuple[str, datetime | None]]:
        return self.tables.recent_replies(google_sub, location, since)

    async def claim_action(self, action_id: str, job_id: str, at: datetime) -> ClaimedAction | None:
        action = self.tables.claim_action(action_id, job_id, at)
        if action is None:
            return None
        return ClaimedAction(
            id=action.id,
            google_sub=action.google_sub,
            location=action.location,
            gbp_review_id=self.tables.reviews[action.review_id].gbp_review_id,
            text=action.text,
            attempts=action.attempts,
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
