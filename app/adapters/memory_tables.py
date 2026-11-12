"""Reviews, actions and the audit trail in memory, for tests and for local development
without a database.

MemoryStore (the API's side) and MemoryWorkerStore (the worker's side) share one of
these, as the API and the worker share the tables in PostgreSQL. The rules are the
ones the SQL of the PostgreSQL adapters enforces.
"""

import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from app.domain.actions import (
    DONE,
    EVENT_CONFIRMED,
    EVENT_FAILED,
    EVENT_SENT,
    FAILED,
    LIVE_STATES,
    PENDING,
    REPLY_REVIEW,
    RUNNING,
)
from app.domain.profile_summary import LOW_RATING_MAX, Review
from app.domain.reviews import storable_reviews, storable_text
from app.domain.sentiment import (
    SOURCE_RULES,
    STATUS_ERROR,
    STATUS_OK,
    TRIGGER_FIRST,
    Analysis,
    Figures,
    LastAnalysis,
    SentimentReview,
    StoredClassification,
    Usage,
    first_history_month,
    month_of,
)


@dataclass
class ReviewRow:
    id: str
    google_sub: str
    location: str
    gbp_review_id: str
    rating: int | None
    text: str
    created_at: datetime | None
    reply_text: str | None = None
    reply_at: datetime | None = None
    sent_at: datetime | None = None
    removed_at: datetime | None = None


@dataclass
class ActionRow:
    id: str
    account_id: str
    type: str
    google_sub: str
    location: str
    review_id: str
    text: str
    confirmed_at: datetime
    state: str = PENDING
    job_id: str | None = None
    attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    google_status: int | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class AuditRow:
    at: datetime
    written_by: str  # "api" or "worker"
    event: str
    account_id: str
    action_id: str
    action_type: str
    location: str
    review_id: str
    gbp_review_id: str
    text: str
    confirmed_at: datetime
    google_status: int | None = None
    reason: str | None = None


@dataclass
class MemoryTables:
    reviews: dict[str, ReviewRow] = field(default_factory=dict)  # by our id
    actions: dict[str, ActionRow] = field(default_factory=dict)
    audit: list[AuditRow] = field(default_factory=list)  # append only
    reviews_synced_at: dict[tuple[str, str], datetime] = field(default_factory=dict)
    # Sentiment: classification by our review id; analysis, history and usage by profile.
    classifications: dict[str, StoredClassification] = field(default_factory=dict)
    analyses: dict[tuple[str, str], Analysis] = field(default_factory=dict)
    history: dict[tuple[str, str], dict[date, int]] = field(default_factory=dict)
    usage: list[tuple[str, str, str, Usage]] = field(default_factory=list)  # append only

    # --- reviews ---------------------------------------------------------------

    def reviews_of(self, google_sub: str, location: str) -> list[ReviewRow]:
        """The live reviews of one profile of one connection."""
        return [
            row
            for row in self.reviews.values()
            if row.google_sub == google_sub and row.location == location and not row.removed_at
        ]

    def unanswered(self, google_sub: str, location: str) -> tuple[int, int]:
        """Unanswered reviews of a profile: all of them, and those of 1 or 2 stars."""
        pending = [r for r in self.reviews_of(google_sub, location) if r.reply_text is None]
        low = sum(1 for r in pending if r.rating is not None and r.rating <= LOW_RATING_MAX)
        return len(pending), low

    def save_reviews(
        self, google_sub: str, location: str, reviews: Sequence[Review], at: datetime
    ) -> None:
        known = {
            row.gbp_review_id: row
            for row in self.reviews.values()
            if row.google_sub == google_sub and row.location == location
        }
        seen: set[str] = set()
        for review in storable_reviews(reviews):
            seen.add(review.review_id)
            row = known.get(review.review_id)
            if row is None:
                row = ReviewRow(
                    f"rev_{secrets.token_hex(8)}",
                    google_sub,
                    location,
                    review.review_id,
                    None,
                    "",
                    None,
                )
                self.reviews[row.id] = row
            row.rating = review.rating
            row.text = storable_text(review.text)
            row.created_at = review.created_at
            row.removed_at = None
            row.reply_text = (
                storable_text(review.reply_text) if review.reply_text is not None else None
            )
            row.reply_at = review.reply_at
        for gbp_id, row in known.items():
            if gbp_id not in seen and row.removed_at is None:
                row.removed_at = at
        self.reviews_synced_at[(google_sub, location)] = at

    def recent_replies(
        self, google_sub: str, location: str, since: datetime
    ) -> dict[str, tuple[str, datetime | None]]:
        return {
            row.gbp_review_id: (row.reply_text, row.reply_at)
            for row in self.reviews_of(google_sub, location)
            if row.sent_at is not None and row.sent_at > since and row.reply_text is not None
        }

    # --- sentiment -------------------------------------------------------------

    def sentiment_reviews(self, google_sub: str, location: str) -> list[SentimentReview]:
        return [
            SentimentReview(
                row.id, row.rating, row.text, row.created_at, self.classifications.get(row.id)
            )
            for row in self.reviews_of(google_sub, location)
        ]

    def last_analysis(self, google_sub: str, location: str) -> LastAnalysis | None:
        analysis = self.analyses.get((google_sub, location))
        if analysis is None:
            return None
        figures = analysis.figures
        return LastAnalysis(figures.status, analysis.generated_at, figures.based_on)

    def save_analysis(
        self, google_sub: str, location: str, analysis: Analysis, usage: Usage
    ) -> None:
        key = (google_sub, location)
        self.analyses[key] = analysis
        if analysis.figures.status == STATUS_OK:
            month = month_of(analysis.generated_at)
            self.history.setdefault(key, {})[month] = analysis.figures.score
        self.usage.append((google_sub, location, analysis.trigger, usage))

    def save_analysis_error(self, google_sub: str, location: str, at: datetime) -> None:
        key = (google_sub, location)
        if key not in self.analyses:
            figures = Figures(STATUS_ERROR, 0, 0, True, None, None, None)
            self.analyses[key] = Analysis(figures, None, None, SOURCE_RULES, TRIGGER_FIRST, at)

    def sentiment_of(
        self, google_sub: str, location: str, now: datetime
    ) -> tuple[Analysis, list[tuple[date, int]]] | None:
        """The analysis of a profile and its trend, oldest month first."""
        analysis = self.analyses.get((google_sub, location))
        if analysis is None:
            return None
        first = first_history_month(now)
        months = self.history.get((google_sub, location), {})
        return analysis, sorted((m, s) for m, s in months.items() if m >= first)

    # --- actions ---------------------------------------------------------------

    def create_action(
        self, account_id: str, review: ReviewRow, text: str, now: datetime
    ) -> ActionRow | None:
        """A pending action and its audit row. None if that review already has one live."""
        if any(a.review_id == review.id and a.state in LIVE_STATES for a in self.actions.values()):
            return None
        action = ActionRow(
            id=f"act_{secrets.token_hex(8)}",
            account_id=account_id,
            type=REPLY_REVIEW,
            google_sub=review.google_sub,
            location=review.location,
            review_id=review.id,
            text=text,
            confirmed_at=now,
        )
        self.actions[action.id] = action
        self._audit(action, EVENT_CONFIRMED, "api", now)
        return action

    def claim_action(self, action_id: str, job_id: str, at: datetime) -> ActionRow | None:
        action = self.actions.get(action_id)
        if action is None:
            return None
        if not (action.state == PENDING or (action.state == RUNNING and action.job_id == job_id)):
            return None
        action.state, action.job_id, action.started_at = RUNNING, job_id, at
        action.attempts += 1
        return action

    def finish_action(
        self,
        action_id: str,
        job_id: str | None,
        *,
        done: bool,
        at: datetime,
        google_status: int | None = None,
        reason: str | None = None,
        reply_at: datetime | None = None,
    ) -> bool:
        """Close a running action held by that job (or any live one when job_id is
        None: the queue has given up on its job). False if there was nothing to close."""
        action = self.actions.get(action_id)
        if action is None:
            return False
        if job_id is None:
            if action.state not in LIVE_STATES:
                return False
        elif not (action.state == RUNNING and action.job_id == job_id):
            return False
        action.state = DONE if done else FAILED
        action.finished_at, action.google_status = at, google_status
        action.error_code = None if done else reason
        self._audit(action, EVENT_SENT if done else EVENT_FAILED, "worker", at, reason)
        if done:
            review = self.reviews[action.review_id]
            review.reply_text, review.reply_at, review.sent_at = action.text, reply_at or at, at
        return True

    def _audit(
        self,
        action: ActionRow,
        event: str,
        written_by: str,
        at: datetime,
        reason: str | None = None,
    ) -> None:
        self.audit.append(
            AuditRow(
                at=at,
                written_by=written_by,
                event=event,
                account_id=action.account_id,
                action_id=action.id,
                action_type=action.type,
                location=action.location,
                review_id=action.review_id,
                gbp_review_id=self.reviews[action.review_id].gbp_review_id,
                text=action.text,
                confirmed_at=action.confirmed_at,
                google_status=action.google_status,
                reason=reason,
            )
        )
