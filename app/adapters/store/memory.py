"""In-memory store for tests and local development without a database."""

import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from app.adapters.memory_tables import ActionRow, MemoryTables, ReviewRow
from app.domain.actions import REPLY_REVIEW
from app.ports.google_oauth import ProfileLocation
from app.ports.job_queue import JobEnqueuer
from app.ports.store import (
    Account,
    GoogleConnection,
    Location,
    ReplyInProgress,
    ReviewDraft,
    ReviewPage,
    SelectedProfile,
    StoredAction,
    StoredReview,
    StoredSentiment,
    StoredSummary,
    WaitlistSignup,
)

REPLY_MAX_ATTEMPTS = 3


def review_order(row: ReviewRow) -> tuple:
    """Unanswered first; then fewer stars; then the newest."""
    age = -row.created_at.timestamp() if row.created_at else float("inf")
    return (row.reply_text is not None, row.rating if row.rating is not None else 6, age, row.id)


@dataclass
class AccountRow:
    id: str
    google_sub: str
    email: str
    selected_location_id: str | None = None


@dataclass
class LocationRow:
    id: str
    account_id: str
    profile: ProfileLocation
    removed: bool = False


@dataclass
class SessionRow:
    account_id: str
    expires_at: datetime
    revoked_at: datetime | None = None


class MemoryStore:
    def __init__(self, fail: bool = False, reply_job_kind: str = REPLY_REVIEW) -> None:
        # Reviews, actions and audit trail: what the API shares with the worker.
        self.tables = MemoryTables()
        self.drafts: dict[str, ReviewDraft] = {}  # by review id
        # Where create_reply_action enqueues the job. create_app sets it.
        self.jobs: JobEnqueuer | None = None
        self._reply_job_kind = reply_job_kind
        self.connections: list[GoogleConnection] = []
        self.signups: dict[tuple[str, str], WaitlistSignup] = {}
        self.events: list[tuple[str, str | None, str | None, int | None]] = []
        self.accounts: dict[str, AccountRow] = {}  # by google_sub
        self.locations: list[LocationRow] = []
        self.sessions: dict[str, SessionRow] = {}  # by token hash, never the token
        # What the worker would write, by (google_sub, Google location name).
        self.summaries: dict[tuple[str, str], StoredSummary] = {}
        self._fail = fail

    def _check(self) -> None:
        if self._fail:
            raise RuntimeError("fallo simulado")

    async def add_signup(self, signup: WaitlistSignup) -> None:
        self._check()
        self.signups.setdefault((signup.channel, signup.contact), signup)

    async def add_google_connection(self, connection: GoogleConnection) -> None:
        self._check()
        self.connections.append(connection)

    async def record_event(
        self,
        kind: str,
        source: str | None = None,
        place_id: str | None = None,
        score: int | None = None,
    ) -> None:
        self._check()
        self.events.append((kind, source, place_id, score))

    def _account(self, account_id: str) -> AccountRow:
        return next(a for a in self.accounts.values() if a.id == account_id)

    async def upsert_account(self, google_sub: str, email: str) -> str:
        self._check()
        row = self.accounts.setdefault(google_sub, AccountRow(str(uuid.uuid4()), google_sub, email))
        row.email = email
        return row.id

    async def sync_locations(
        self, account_id: str, locations: Sequence[ProfileLocation], now: datetime
    ) -> None:
        self._check()
        stale = {r.profile.location: r for r in self.locations if r.account_id == account_id}
        for profile in locations:
            row = stale.pop(profile.location, None)
            if row is None:
                row = LocationRow(f"loc_{secrets.token_hex(6)}", account_id, profile)
                self.locations.append(row)
            row.profile = profile
            row.removed = False
        account = self._account(account_id)
        for row in stale.values():
            row.removed = True
            if account.selected_location_id == row.id:
                account.selected_location_id = None

    async def create_session(
        self, account_id: str, token_hash: str, now: datetime, expires_at: datetime
    ) -> None:
        self._check()
        self.sessions[token_hash] = SessionRow(account_id, expires_at)

    async def account_for_session(self, token_hash: str, now: datetime) -> Account | None:
        self._check()
        session = self.sessions.get(token_hash)
        if session is None or session.revoked_at is not None or session.expires_at <= now:
            return None
        row = self._account(session.account_id)
        return Account(row.id, row.email, row.selected_location_id)

    async def revoke_session(self, token_hash: str, now: datetime) -> None:
        self._check()
        session = self.sessions.get(token_hash)
        if session is not None and session.revoked_at is None:
            session.revoked_at = now

    async def list_locations(self, account_id: str) -> list[Location]:
        self._check()
        found = [
            Location(r.id, r.profile.title, r.profile.place_id or None, r.profile.verified)
            for r in self.locations
            if r.account_id == account_id and not r.removed
        ]
        return sorted(found, key=lambda loc: (loc.title, loc.id))

    async def select_location(self, account_id: str, location_id: str) -> bool:
        self._check()
        owned = any(
            r.id == location_id and r.account_id == account_id and not r.removed
            for r in self.locations
        )
        if owned:
            self._account(account_id).selected_location_id = location_id
        return owned

    def put_summary(self, google_sub: str, location: str, summary: StoredSummary) -> None:
        """Stand-in for the worker: the API itself never writes a summary."""
        self.summaries[(google_sub, location)] = summary

    async def selected_profile(self, account_id: str) -> SelectedProfile | None:
        self._check()
        account = self._account(account_id)
        row = next(
            (
                r
                for r in self.locations
                if r.id == account.selected_location_id
                and r.account_id == account_id
                and not r.removed
            ),
            None,
        )
        if row is None:
            return None
        key = (account.google_sub, row.profile.location)
        summary = self.summaries.get(key)
        synced_at = self.tables.reviews_synced_at.get(key)
        if summary is not None and synced_at is not None and summary.unanswered_count is not None:
            # Stand-in for the worker, which lowers the counts when a reply is sent:
            # here they are simply read from the stored reviews.
            pending, low = self.tables.unanswered(*key)
            summary = replace(
                summary,
                unanswered_count=pending,
                unanswered_low_count=low,
                reviews_synced_at=synced_at,
            )
        return SelectedProfile(
            account.google_sub,
            row.profile.location,
            summary,
            row.profile.title,
            row.profile.place_id or None,
        )

    # --- reviews and replies ------------------------------------------------------

    def _selected_key(self, account_id: str) -> tuple[str, str] | None:
        """(google_sub, Google location) of the selected profile of that account."""
        account = next((a for a in self.accounts.values() if a.id == account_id), None)
        if account is None:
            return None
        for row in self.locations:
            if (
                row.id == account.selected_location_id
                and row.account_id == account_id
                and not row.removed
            ):
                return account.google_sub, row.profile.location
        return None

    def _review_row(self, account_id: str, review_id: str) -> ReviewRow | None:
        key = self._selected_key(account_id)
        row = self.tables.reviews.get(review_id)
        if key is None or row is None or row.removed_at is not None:
            return None
        return row if (row.google_sub, row.location) == key else None

    @staticmethod
    def _stored_review(row: ReviewRow, draft: ReviewDraft | None = None) -> StoredReview:
        return StoredReview(
            row.id, row.rating, row.text, row.created_at, row.reply_text, row.reply_at, draft
        )

    async def list_reviews(
        self, account_id: str, *, unanswered_only: bool, limit: int
    ) -> ReviewPage:
        self._check()
        key = self._selected_key(account_id)
        rows = self.tables.reviews_of(*key) if key is not None else []
        if unanswered_only:
            rows = [r for r in rows if r.reply_text is None]
        rows.sort(key=review_order)
        return ReviewPage(tuple(self._stored_review(r) for r in rows[:limit]), len(rows))

    async def get_review(self, account_id: str, review_id: str) -> StoredReview | None:
        self._check()
        row = self._review_row(account_id, review_id)
        return self._stored_review(row, self.drafts.get(row.id)) if row is not None else None

    async def save_draft(
        self, account_id: str, review_id: str, source_hash: str, text: str, now: datetime
    ) -> None:
        self._check()
        if self._review_row(account_id, review_id) is not None:
            self.drafts[review_id] = ReviewDraft(text, source_hash)

    async def create_reply_action(
        self, account_id: str, review_id: str, text: str, now: datetime
    ) -> str | None:
        self._check()
        row = self._review_row(account_id, review_id)
        if row is None:
            return None
        action = self.tables.create_action(account_id, row, text, now)
        if action is None:
            raise ReplyInProgress
        if self.jobs is not None:
            await self.jobs.enqueue(
                self._reply_job_kind,
                {"action_id": action.id},
                idempotency_key=f"{self._reply_job_kind}:{action.id}",
                max_attempts=REPLY_MAX_ATTEMPTS,
            )
        return action.id

    async def get_action(self, account_id: str, action_id: str) -> StoredAction | None:
        self._check()
        action: ActionRow | None = self.tables.actions.get(action_id)
        if action is None or action.account_id != account_id:
            return None
        return StoredAction(
            action.id,
            action.type,
            action.review_id,
            action.state,
            action.error_code,
            action.confirmed_at,
            action.finished_at,
        )

    async def sentiment(self, account_id: str, now: datetime) -> StoredSentiment | None:
        self._check()
        key = self._selected_key(account_id)
        found = self.tables.sentiment_of(*key, now) if key is not None else None
        return StoredSentiment(found[0], tuple(found[1])) if found is not None else None
