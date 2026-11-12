"""In-memory store for tests and local development without a database."""

import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from app.adapters.memory_tables import ActionRow, MemoryTables, ReviewRow
from app.adapters.quota.memory import MemoryQuota
from app.domain.actions import REPLY_REVIEW
from app.domain.offboarding import (
    APPLY_KIND,
    APPLY_MAX_ATTEMPTS,
    CODE_GONE,
    CODE_LOCKED,
    CODE_OK,
    CODE_WRONG,
    MAIL_MAX_ATTEMPTS,
    MAIL_PENDING,
    SCOPE_ACCOUNT,
    SEND_MAIL_KIND,
    CodeCheck,
    PendingDeletion,
    same_hash,
)
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
    lost_access_summary,
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
    id: str = ""


@dataclass
class CodeRow:
    id: str
    account_id: str
    session_id: str
    scope: str
    code_hash: str
    expires_at: datetime
    attempts: int = 0
    used_at: datetime | None = None
    invalidated_at: datetime | None = None
    mail_state: str = MAIL_PENDING


@dataclass
class OffboardingRow:
    id: str
    account_id: str
    scope: str
    confirmed_at: datetime
    delete_at: datetime
    restore_token_hash: str | None
    restored_at: datetime | None = None
    effective_at: datetime | None = None


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
        self.codes: list[CodeRow] = []  # fingerprints only, never a code
        self.offboardings: list[OffboardingRow] = []
        self.quota = MemoryQuota()
        self._fail = fail

    def _check(self) -> None:
        if self._fail:
            raise RuntimeError("fallo simulado")

    # Caps on paid calls: kept by the store, as PostgreSQL keeps them, so that a new
    # app on the same store (a restart) finds them where they were.
    async def take_quota(self, scope: str, limit: int, window_seconds: int, now: datetime) -> bool:
        return await self.quota.take_quota(scope, limit, window_seconds, now)

    async def quota_left(self, scope: str, limit: int, window_seconds: int, now: datetime) -> bool:
        return await self.quota.quota_left(scope, limit, window_seconds, now)

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
        self.sessions[token_hash] = SessionRow(account_id, expires_at, id=str(uuid.uuid4()))

    async def account_for_session(self, token_hash: str, now: datetime) -> Account | None:
        self._check()
        session = self.sessions.get(token_hash)
        if session is None or session.revoked_at is not None or session.expires_at <= now:
            return None
        row = self._account(session.account_id)
        leave = self._pending(row.id)
        pending = PendingDeletion(leave.scope, leave.delete_at) if leave else None
        return Account(row.id, row.email, row.selected_location_id, session.id, pending)

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
        if row is None or self._pending(account_id) is not None:
            return self._lost_profile(account)
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

    def _lost_profile(self, account: AccountRow) -> SelectedProfile | None:
        """The selected profile retired by the worker for lack of access, if that is it."""
        if self._pending(account.id) is not None:
            return None
        for row in self.locations:
            if row.id == account.selected_location_id and row.account_id == account.id:
                if row.removed:
                    summary = lost_access_summary(datetime.now(UTC))
                    return SelectedProfile(
                        account.google_sub, row.profile.location, summary, access_lost=True
                    )
        return None

    # --- reviews and replies ------------------------------------------------------

    def _selected_key(self, account_id: str) -> tuple[str, str] | None:
        """(google_sub, Google location) of the selected profile of that account."""
        account = next((a for a in self.accounts.values() if a.id == account_id), None)
        if account is None or self._pending(account_id) is not None:
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

    # --- leaving: code, scheduled leave and restore -------------------------------

    def _pending(self, account_id: str) -> OffboardingRow | None:
        """The scheduled leave of that account, if it has one."""
        return next(
            (
                o
                for o in self.offboardings
                if o.account_id == account_id and o.restored_at is None and o.effective_at is None
            ),
            None,
        )

    async def create_offboarding_code(
        self,
        account_id: str,
        session_id: str,
        scope: str,
        code_hash: str,
        now: datetime,
        expires_at: datetime,
        mail: dict,
    ) -> None:
        self._check()
        for code in self.codes:
            if code.account_id == account_id and code.used_at is None:
                code.invalidated_at = code.invalidated_at or now
        row = CodeRow(str(uuid.uuid4()), account_id, session_id, scope, code_hash, expires_at)
        self.codes.append(row)
        if self.jobs is not None:
            await self.jobs.enqueue(
                SEND_MAIL_KIND, {**mail, "code_id": row.id}, max_attempts=MAIL_MAX_ATTEMPTS
            )

    async def offboarding_code_mail(self, account_id: str) -> str | None:
        self._check()
        for code in reversed(self.codes):
            if code.account_id == account_id:
                live = code.used_at is None and code.invalidated_at is None
                return code.mail_state if live else None
        return None

    async def confirm_offboarding(
        self,
        account_id: str,
        candidate_hash: str,
        scope: str,
        now: datetime,
        *,
        max_attempts: int,
        delete_at: datetime,
        restore_hash: str,
        mail: dict,
    ) -> CodeCheck:
        self._check()
        code = next((c for c in reversed(self.codes) if c.account_id == account_id), None)
        if (
            code is None
            or code.used_at is not None
            or code.invalidated_at is not None
            or code.expires_at <= now
        ):
            return CodeCheck(CODE_GONE)
        if code.attempts >= max_attempts:
            code.invalidated_at = now
            return CodeCheck(CODE_LOCKED)
        code.attempts += 1
        if not same_hash(code.code_hash, candidate_hash):
            if code.attempts >= max_attempts:
                code.invalidated_at = now
                return CodeCheck(CODE_LOCKED)
            return CodeCheck(CODE_WRONG, max_attempts - code.attempts)
        code.used_at = now
        earlier = self._pending(account_id)
        if earlier is not None:
            earlier.restored_at, earlier.restore_token_hash = now, None
        leave = OffboardingRow(str(uuid.uuid4()), account_id, scope, now, delete_at, restore_hash)
        self.offboardings.append(leave)
        if scope == SCOPE_ACCOUNT:
            for session in self.sessions.values():
                if session.account_id == account_id and session.revoked_at is None:
                    session.revoked_at = now
        if self.jobs is not None:
            await self.jobs.enqueue(SEND_MAIL_KIND, dict(mail), max_attempts=MAIL_MAX_ATTEMPTS)
            await self.jobs.enqueue(
                APPLY_KIND,
                {"offboarding_id": leave.id},
                idempotency_key=f"{APPLY_KIND}:{leave.id}",
                run_at=delete_at,
                max_attempts=APPLY_MAX_ATTEMPTS,
            )
        return CodeCheck(CODE_OK, delete_at=delete_at)

    async def restore_offboarding(self, account_id: str, now: datetime) -> bool:
        self._check()
        leave = self._pending(account_id)
        if leave is None:
            return False
        leave.restored_at, leave.restore_token_hash = now, None
        return True

    async def restore_offboarding_by_token(self, token_hash: str, now: datetime) -> str | None:
        self._check()
        for leave in self.offboardings:
            if (
                leave.restore_token_hash is not None
                and same_hash(leave.restore_token_hash, token_hash)
                and leave.restored_at is None
                and leave.effective_at is None
                and leave.delete_at > now
            ):
                leave.restored_at, leave.restore_token_hash = now, None
                return leave.account_id
        return None
