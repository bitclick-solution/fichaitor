"""In-memory store for tests and local development without a database."""

import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime

from app.adapters.memory_tables import ActionRow, MemoryTables, ReviewRow
from app.adapters.quota.memory import MemoryQuota
from app.domain.actions import REPLY_REVIEW, SET_SPECIAL_HOURS
from app.domain.conversation import ChannelIdentity
from app.domain.key_dates import (
    ANSWERABLE_STATES,
    OUTCOME_DISMISSED,
    REFUSED_PROFILE,
    PanelRefused,
    panel_confirm_path,
    panel_dismiss_path,
    panel_event_id,
)
from app.domain.link_codes import LinkCode
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
from app.domain.plans import (
    BETA_PRO_QUANTITY,
    PAID_PLANS,
    Entitlement,
    Plan,
    activation_allowed,
)
from app.domain.proposals import (
    CANCELLED_BY_DEACTIVATION,
    CANCELLED_BY_LEAVE,
    OUTCOME_LOCATION_INACTIVE,
    OUTCOME_OFFBOARDING,
    Proposal,
    ProposalState,
    ProposalType,
    advance,
)
from app.domain.special_hours import JOB_MAX_ATTEMPTS as HOURS_MAX_ATTEMPTS
from app.domain.special_hours import SpecialHoursChange, describe
from app.ports.channel_links import LinkCodeTaken
from app.ports.google_oauth import ProfileLocation
from app.ports.job_queue import JobEnqueuer
from app.ports.key_dates import KeyDateNotFound, KeyDateView, SpecialHoursInProgress
from app.ports.store import (
    Account,
    ActiveProfile,
    FreeTaken,
    GoogleConnection,
    Location,
    LocationInactive,
    LocationLimit,
    LocationNotFound,
    LocationOverview,
    LocationRemoved,
    LocationsOverview,
    NoLocationChosen,
    OffboardingPending,
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
    # Every new account gets the beta one (db/schema.sql, account_beta_entitlement).
    entitlements: list[Entitlement] = field(
        default_factory=lambda: [
            Entitlement(Plan.PRO, BETA_PRO_QUANTITY, datetime(2026, 1, 1, tzinfo=UTC))
        ]
    )


@dataclass
class LocationRow:
    id: str
    account_id: str
    profile: ProfileLocation
    removed: bool = False
    # What location_active_guard keeps in PostgreSQL: active means a plan.
    plan: Plan | None = None
    active_since: datetime | None = None
    deactivated_at: datetime | None = None

    @property
    def active(self) -> bool:
        # A retired profile is never active (in PostgreSQL, the trigger sees to it); a
        # test that stands in for the worker may only set `removed`.
        return self.plan is not None and not self.removed


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
        # Syncs waiting or running in the queue, by the same key, with when they were
        # asked for (the view profile_sync_pending). There is no queue here: tests set
        # them by hand, and put_summary (the worker finishing) takes the key out.
        self.syncs_requested: dict[tuple[str, str], datetime] = {}
        self.codes: list[CodeRow] = []  # fingerprints only, never a code
        self.offboardings: list[OffboardingRow] = []
        self.quota = MemoryQuota()
        # Messaging channels: what the conversation would have linked, and the codes.
        self.channels: list[ChannelIdentity] = []
        self.link_codes: list[LinkCode] = []  # hashes only, never a code
        # Key date proposals, as the conversation would have created them (by id).
        self.key_date_proposals: dict[str, Proposal] = {}
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
            if row.plan is not None:
                row.plan, row.active_since, row.deactivated_at = None, None, now
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
        row = self._own(account_id, location_id)
        if row is None or row.removed:
            return False
        if row.plan is None:  # until step 9 (docs/multinegocio.md, 5.5)
            self._activate(row, None)
        self._account(account_id).selected_location_id = location_id
        return True

    # --- profiles of the account, active or not (docs/multinegocio.md) -------------

    def _own(self, account_id: str, location_id: str | None) -> LocationRow | None:
        return next(
            (r for r in self.locations if r.id == location_id and r.account_id == account_id),
            None,
        )

    def _active_by_plan(self, account_id: str, leave_out: str = "") -> dict[Plan, int]:
        counts: dict[Plan, int] = {}
        for r in self.locations:
            if r.account_id == account_id and r.active and r.id != leave_out:
                counts[r.plan] = counts.get(r.plan, 0) + 1
        return counts

    def _fits(self, row: LocationRow, plan: Plan) -> type[Exception] | None:
        """What location_active_guard would refuse, or None."""
        if self._pending(row.account_id) is not None:
            return OffboardingPending
        account = self._account(row.account_id)
        now = datetime.now(UTC)
        if not activation_allowed(
            self._active_by_plan(row.account_id, row.id), account.entitlements, plan, now
        ):
            return LocationLimit
        if plan is Plan.GRATIS and any(
            r.profile.location == row.profile.location
            and r.account_id != row.account_id
            and r.plan is Plan.GRATIS
            for r in self.locations
        ):
            return FreeTaken
        return None

    def _activate(self, row: LocationRow, plan: Plan | None) -> Plan:
        if row.plan is not None and plan in (None, row.plan):
            return row.plan
        # As PostgresStore._activate: without a plan, only the plans with room in the
        # account are tried (_default_plans), each refusal of the last one is what is
        # raised, and only FreeTaken and LocationLimit move on to the next plan.
        if plan is not None:
            candidates = [plan]
        else:
            account = self._account(row.account_id)
            active = self._active_by_plan(row.account_id, row.id)
            now = datetime.now(UTC)
            candidates = [
                p
                for p in (Plan.GRATIS, *PAID_PLANS)
                if activation_allowed(active, account.entitlements, p, now)
            ]
        if not candidates:
            raise LocationLimit
        for index, candidate in enumerate(candidates):
            refused = self._fits(row, candidate)
            if refused is None:
                row.plan = candidate
                row.active_since = row.active_since or datetime.now(UTC)
                row.deactivated_at = None
                return candidate
            last = index + 1 == len(candidates)
            if last or refused not in (FreeTaken, LocationLimit):
                raise refused
        raise LocationLimit  # not reached: the last candidate returns or raises

    async def activate(self, account_id: str, location_id: str, plan: Plan | None = None) -> Plan:
        self._check()
        row = self._own(account_id, location_id)
        if row is None:
            raise LocationNotFound
        if row.removed:
            raise LocationRemoved
        return self._activate(row, plan)

    async def deactivate(self, account_id: str, location_id: str) -> bool:
        self._check()
        row = self._own(account_id, location_id)
        if row is None or row.removed:
            return False
        if row.plan is None:
            return True
        row.plan, row.active_since, row.deactivated_at = None, None, datetime.now(UTC)
        for proposal_id, proposal in list(self.key_date_proposals.items()):
            if (
                proposal.account_id == account_id
                and proposal.payload.get("location_id") == location_id
                and proposal.state in CANCELLED_BY_DEACTIVATION
            ):
                self.key_date_proposals[proposal_id] = replace(
                    proposal,
                    state=ProposalState.CANCELLED,
                    outcome_reason=OUTCOME_LOCATION_INACTIVE,
                    step_token=secrets.token_hex(3),
                    message_refs={},
                )
        account = self._account(account_id)
        if account.selected_location_id == location_id:
            oldest = sorted(
                (r for r in self.locations if r.account_id == account_id and r.active),
                key=lambda r: (r.active_since, r.id),
            )
            account.selected_location_id = oldest[0].id if oldest else None
        return True

    async def resolve_location(self, account_id: str, location_id: str | None) -> str:
        self._check()
        account = self._account(account_id)
        chosen = location_id if location_id is not None else account.selected_location_id
        row = self._own(account_id, chosen)
        if row is None:
            raise NoLocationChosen if location_id is None else LocationNotFound
        if row.removed:
            if row.id == account.selected_location_id:
                return row.id
            raise LocationNotFound
        if row.plan is None:
            raise LocationInactive
        return row.id

    async def locations_overview(self, account_id: str, today: date) -> LocationsOverview:
        self._check()
        account = self._account(account_id)
        found = []
        for location in await self.list_locations(account_id):
            row = self._own(account_id, location.id)
            assert row is not None
            if row.plan is None:
                found.append(
                    LocationOverview(
                        location.id, location.title, location.place_id, location.verified
                    )
                )
                continue
            key = (account.google_sub, row.profile.location)
            summary = self.summaries.get(key)
            key_dates = sum(
                1
                for p in self.key_date_proposals.values()
                if p.account_id == account_id
                and p.payload.get("location_id") == row.id
                and p.state in ANSWERABLE_STATES
                and p.payload.get("date", "") >= today.isoformat()
            )
            found.append(
                LocationOverview(
                    location.id,
                    location.title,
                    location.place_id,
                    location.verified,
                    plan=row.plan,
                    active_since=row.active_since,
                    summary_status=summary.status if summary is not None else None,
                    pending_reviews=(summary.unanswered_count or 0) if summary else 0,
                    pending_key_dates=key_dates,
                    summary_status_at=summary.status_at if summary is not None else None,
                    sync_requested_at=self.syncs_requested.get(key),
                )
            )
        return LocationsOverview(tuple(found), tuple(account.entitlements))

    async def active_profiles(self, account_id: str) -> list[ActiveProfile]:
        self._check()
        if self._pending(account_id) is not None:
            return []
        account = self._account(account_id)
        rows = sorted(
            (r for r in self.locations if r.account_id == account_id and r.active),
            key=lambda r: (r.active_since, r.id),
        )
        return [ActiveProfile(r.id, account.google_sub, r.profile.location) for r in rows]

    def put_summary(self, google_sub: str, location: str, summary: StoredSummary) -> None:
        """Stand-in for the worker: the API itself never writes a summary."""
        self.summaries[(google_sub, location)] = summary
        self.syncs_requested.pop((google_sub, location), None)

    async def selected_profile(self, account_id: str) -> SelectedProfile | None:
        return await self.profile(account_id)

    async def profile(
        self, account_id: str, location_id: str | None = None
    ) -> SelectedProfile | None:
        self._check()
        account = self._account(account_id)
        row = self._active_row(account_id, location_id)
        if row is None:
            return self._lost_profile(account, location_id)
        key = (account.google_sub, row.profile.location)
        summary = self.summaries.get(key)
        if summary is not None:
            summary = replace(summary, sync_requested_at=self.syncs_requested.get(key))
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
            location_id=row.id,
        )

    def _lost_profile(self, account: AccountRow, location_id: str | None) -> SelectedProfile | None:
        """The preferred profile retired by the worker for lack of access, if that is it."""
        if self._pending(account.id) is not None:
            return None
        if location_id not in (None, account.selected_location_id):
            return None
        for row in self.locations:
            if row.id == account.selected_location_id and row.account_id == account.id:
                if row.removed:
                    summary = lost_access_summary(datetime.now(UTC))
                    return SelectedProfile(
                        account.google_sub,
                        row.profile.location,
                        summary,
                        access_lost=True,
                        location_id=row.id,
                    )
        return None

    # --- reviews and replies ------------------------------------------------------

    def _active_row(self, account_id: str, location_id: str | None) -> LocationRow | None:
        """That active profile of that account (None: the preference), unless the
        account has a scheduled leave."""
        account = next((a for a in self.accounts.values() if a.id == account_id), None)
        if account is None or self._pending(account_id) is not None:
            return None
        chosen = location_id if location_id is not None else account.selected_location_id
        row = self._own(account_id, chosen)
        return row if row is not None and row.active else None

    def _selected_key(
        self, account_id: str, location_id: str | None = None
    ) -> tuple[str, str] | None:
        """(google_sub, Google location) of that active profile of that account."""
        row = self._active_row(account_id, location_id)
        return (self._account(account_id).google_sub, row.profile.location) if row else None

    def _review_row(self, account_id: str, review_id: str) -> tuple[ReviewRow, str] | None:
        """A review of any active profile of that account, and the id of its profile."""
        row = self.tables.reviews.get(review_id)
        if row is None or row.removed_at is not None or self._pending(account_id) is not None:
            return None
        account = next((a for a in self.accounts.values() if a.id == account_id), None)
        if account is None or row.google_sub != account.google_sub:
            return None
        for location in self.locations:
            if (
                location.account_id == account_id
                and location.active
                and location.profile.location == row.location
            ):
                return row, location.id
        return None

    @staticmethod
    def _stored_review(row: ReviewRow, draft: ReviewDraft | None = None) -> StoredReview:
        return StoredReview(
            row.id, row.rating, row.text, row.created_at, row.reply_text, row.reply_at, draft
        )

    async def list_reviews(
        self,
        account_id: str,
        *,
        unanswered_only: bool,
        limit: int,
        location_id: str | None = None,
    ) -> ReviewPage:
        self._check()
        key = self._selected_key(account_id, location_id)
        rows = self.tables.reviews_of(*key) if key is not None else []
        if unanswered_only:
            rows = [r for r in rows if r.reply_text is None]
        rows.sort(key=review_order)
        return ReviewPage(tuple(self._stored_review(r) for r in rows[:limit]), len(rows))

    async def get_review(self, account_id: str, review_id: str) -> StoredReview | None:
        self._check()
        found = self._review_row(account_id, review_id)
        if found is None:
            return None
        row, location_id = found
        stored = self._stored_review(row, self.drafts.get(row.id))
        return replace(stored, location_id=location_id)

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
        found = self._review_row(account_id, review_id)
        if found is None:
            return None
        action = self.tables.create_action(account_id, found[0], text, now)
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
            action.review_id if action.type == REPLY_REVIEW else None,
            action.state,
            action.error_code,
            action.confirmed_at,
            action.finished_at,
            action.special_date,
        )

    # --- key dates: the hours of one day (app/ports/key_dates.py) -----------------

    async def create_special_hours_action(
        self, account_id: str, location_id: str, change: SpecialHoursChange, now: datetime
    ) -> str | None:
        self._check()
        key = self._selected_key(account_id, location_id)
        if key is None:
            return None
        action = self.tables.create_special_hours_action(
            account_id, key[0], key[1], change.day, change.to_text(), now
        )
        if action is None:
            raise SpecialHoursInProgress
        if self.jobs is not None:
            await self.jobs.enqueue(
                SET_SPECIAL_HOURS,
                {"action_id": action.id},
                idempotency_key=f"{SET_SPECIAL_HOURS}:{action.id}",
                max_attempts=HOURS_MAX_ATTEMPTS,
            )
        return action.id

    async def _key_date_view(self, account_id: str, proposal: Proposal) -> KeyDateView:
        action = None
        if proposal.action_id is not None:
            action = await self.get_action(account_id, proposal.action_id)
        payload = proposal.payload
        return KeyDateView(
            id=proposal.id,
            day=date.fromisoformat(payload["date"]),
            name=str(payload.get("name", "")),
            kind=str(payload.get("kind", "")),
            state=proposal.state,
            location_id=str(payload.get("location_id", "")),
            expires_at=proposal.expires_at,
            confirmed=dict(proposal.slots.get("confirmed") or {}),
            action=action,
        )

    def _own_key_dates(self, account_id: str, location_id: str | None = None) -> list[Proposal]:
        """Of that active profile; with None, of any active profile."""
        if self._pending(account_id) is not None:
            return []
        if location_id is None:
            active = {r.id for r in self.locations if r.account_id == account_id and r.active}
        else:
            row = self._active_row(account_id, location_id)
            active = {row.id} if row is not None else set()
        return [
            p
            for p in self.key_date_proposals.values()
            if p.account_id == account_id
            and p.type is ProposalType.KEY_DATE
            and p.payload.get("location_id") in active
        ]

    async def list_key_dates(
        self, account_id: str, today: date, location_id: str | None = None
    ) -> list[KeyDateView]:
        self._check()
        chosen = location_id or self._account(account_id).selected_location_id
        if chosen is None:
            return []
        found = [
            p
            for p in self._own_key_dates(account_id, chosen)
            if p.payload["date"] >= today.isoformat()
        ]
        found.sort(key=lambda p: (p.payload["date"], p.id))
        return [await self._key_date_view(account_id, p) for p in found]

    async def get_key_date(self, account_id: str, proposal_id: str) -> KeyDateView | None:
        self._check()
        for proposal in self._own_key_dates(account_id):
            if proposal.id == proposal_id:
                return await self._key_date_view(account_id, proposal)
        return None

    def _own_key_date(self, account_id: str, proposal_id: str) -> Proposal:
        proposal = self.key_date_proposals.get(proposal_id)
        if proposal is None or proposal.account_id != account_id:
            raise KeyDateNotFound
        return proposal

    async def answer_key_date(
        self,
        account_id: str,
        proposal_id: str,
        change: SpecialHoursChange,
        now: datetime,
        today: date,
    ) -> str:
        self._check()
        proposal = self._own_key_date(account_id, proposal_id)
        if proposal.payload.get("date") != change.day.isoformat():
            raise KeyDateNotFound
        path = panel_confirm_path(proposal.state, change.day, today)
        location_id = str(proposal.payload.get("location_id", ""))
        action_id = await self.create_special_hours_action(account_id, location_id, change, now)
        if action_id is None:
            raise PanelRefused(REFUSED_PROFILE)
        confirmed = {"kind": change.kind, "periods": [list(p) for p in change.periods]}
        self.key_date_proposals[proposal_id] = replace(
            proposal,
            state=advance(proposal.state, *path),
            step_token=secrets.token_hex(3),
            message_refs={},
            slots={**proposal.slots, "confirmed": confirmed, "answered_in": "panel"},
            confirmed_at=now,
            confirmed_event_id=panel_event_id(now, secrets.token_hex(4)),
            confirmed_summary=f"{change.day.isoformat()}: {describe(change)}",
            action_id=action_id,
            outcome_reason=None,
        )
        return action_id

    async def dismiss_key_date(self, account_id: str, proposal_id: str, now: datetime) -> None:
        self._check()
        proposal = self._own_key_date(account_id, proposal_id)
        path = panel_dismiss_path(proposal.state)
        self.key_date_proposals[proposal_id] = replace(
            proposal,
            state=advance(proposal.state, *path),
            step_token=secrets.token_hex(3),
            message_refs={},
            outcome_reason=OUTCOME_DISMISSED,
        )

    async def sentiment(
        self, account_id: str, now: datetime, location_id: str | None = None
    ) -> StoredSentiment | None:
        self._check()
        key = self._selected_key(account_id, location_id)
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
        # What waited for an answer is not sent any more (B12).
        for proposal_id, proposal in list(self.key_date_proposals.items()):
            if proposal.account_id == account_id and proposal.state in CANCELLED_BY_LEAVE:
                self.key_date_proposals[proposal_id] = replace(
                    proposal,
                    state=ProposalState.CANCELLED,
                    outcome_reason=OUTCOME_OFFBOARDING,
                    message_refs={},
                )
        if scope == SCOPE_ACCOUNT:
            for session in self.sessions.values():
                if session.account_id == account_id and session.revoked_at is None:
                    session.revoked_at = now
            self._unlink_channels(account_id, now)
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

    async def _restore(self, leave: OffboardingRow, now: datetime, mail: dict) -> None:
        # The email first: if it cannot be enqueued nothing has changed, as the
        # transaction of PostgreSQL would leave it.
        to = self._account(leave.account_id).email
        if to and self.jobs is not None:
            await self.jobs.enqueue(
                SEND_MAIL_KIND,
                {**mail, "to": to, "scope": leave.scope},
                max_attempts=MAIL_MAX_ATTEMPTS,
            )
        leave.restored_at, leave.restore_token_hash = now, None

    async def restore_offboarding(self, account_id: str, now: datetime, *, mail: dict) -> bool:
        self._check()
        leave = self._pending(account_id)
        if leave is None:
            return False
        await self._restore(leave, now, mail)
        return True

    async def restore_offboarding_by_token(
        self, token_hash: str, now: datetime, *, mail: dict
    ) -> str | None:
        self._check()
        for leave in self.offboardings:
            if (
                leave.restore_token_hash is not None
                and same_hash(leave.restore_token_hash, token_hash)
                and leave.restored_at is None
                and leave.effective_at is None
                and leave.delete_at > now
            ):
                await self._restore(leave, now, mail)
                return leave.account_id
        return None

    # --- messaging channels (app/ports/channel_links.py) ---------------------------
    # Without a database there is no conversation process: nothing links an address
    # here. Tests (and a demo) add them to `channels` by hand.

    async def save_link_code(self, code: LinkCode) -> None:
        self._check()
        hashes = {code.code_hash, code.token_hash} - {None}
        if any(c.code_hash in hashes or c.token_hash in hashes for c in self.link_codes):
            raise LinkCodeTaken
        self.link_codes = [
            replace(c, used_at=code.created_at)
            if c.account_id == code.account_id and c.used_at is None
            else c
            for c in self.link_codes
        ]
        self.link_codes.append(code)

    async def list_channels(self, account_id: str) -> list[ChannelIdentity]:
        self._check()
        found = [c for c in self.channels if c.account_id == account_id and c.active]
        return sorted(found, key=lambda c: (c.verified_at, c.id))

    async def set_channel_filter(
        self, account_id: str, identity_id: str, location_filter: tuple[str, ...] | None
    ) -> ChannelIdentity | None:
        self._check()
        if location_filter is not None and any(
            self._own(account_id, loc) is None for loc in location_filter
        ):
            return None  # as location_filter_guard
        for index, channel in enumerate(self.channels):
            if channel.id == identity_id and channel.account_id == account_id and channel.active:
                self.channels[index] = replace(channel, location_filter=location_filter)
                return self.channels[index]
        return None

    async def unlink_channel(self, account_id: str, identity_id: str, now: datetime) -> bool:
        self._check()
        for index, channel in enumerate(self.channels):
            if channel.id == identity_id and channel.account_id == account_id and channel.active:
                self.channels[index] = replace(channel, unlinked_at=now)
                return True
        return False

    def _unlink_channels(self, account_id: str, now: datetime) -> None:
        self.channels = [
            replace(c, unlinked_at=now) if c.account_id == account_id and c.active else c
            for c in self.channels
        ]
        self.link_codes = [
            replace(c, used_at=now) if c.account_id == account_id and c.used_at is None else c
            for c in self.link_codes
        ]
