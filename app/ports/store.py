from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from app.domain.offboarding import CodeCheck, PendingDeletion
from app.domain.plans import ActiveLocation, Entitlement, Plan
from app.domain.profile_summary import STATUS_NEEDS_RECONNECT, ProfileFacts
from app.domain.sentiment import Analysis
from app.ports.google_oauth import ProfileLocation


@dataclass(frozen=True)
class WaitlistSignup:
    channel: str
    contact: str
    consent_version: str
    place_id: str | None = None
    business_name: str | None = None
    score: int | None = None
    source: str | None = None


@dataclass(frozen=True)
class GoogleConnection:
    google_sub: str
    email: str
    refresh_token_enc: str
    key_version: int
    scopes: tuple[str, ...]
    locations: tuple[dict, ...]


@dataclass(frozen=True)
class Account:
    id: str
    email: str
    selected_location_id: str | None
    # The session the request came with: a code to leave is tied to it.
    session_id: str | None = None
    # A scheduled leave: until it is restored, the account can only see it and undo it.
    pending_deletion: PendingDeletion | None = None
    # False after a leave of the profile, until the owner connects with Google again.
    connected: bool = True


@dataclass(frozen=True)
class Location:
    id: str
    title: str
    place_id: str | None
    verified: bool


@dataclass(frozen=True)
class StoredSummary:
    """A row of profile_summary as the API reads it. Figures are None until a first good sync."""

    status: str  # ok, needs_reconnect or error
    status_at: datetime  # last attempt
    synced_at: datetime | None  # last good read
    review_count: int | None
    average_rating: float | None
    unanswered_count: int | None
    unanswered_low_count: int | None
    last_review_at: datetime | None
    has_regular_hours: bool | None
    upcoming_special_hours: int | None
    google_changed: bool | None
    google_changed_fields: tuple[str, ...] = ()
    # What the diagnosis is computed from. None until a sync has stored them.
    facts: ProfileFacts | None = None
    # When the reviews of the profile were last stored. None: not yet.
    reviews_synced_at: datetime | None = None
    # The newest sync of the profile still waiting (due) or running in the queue, if any
    # (view profile_sync_pending). Read through shown_status (app/domain/profile_summary.py).
    sync_requested_at: datetime | None = None


@dataclass(frozen=True)
class SelectedProfile:
    """The selected profile of an account, as Google names it, and its summary if any."""

    google_sub: str
    location: str  # "locations/123"
    summary: StoredSummary | None
    title: str = ""
    place_id: str | None = None
    # The worker found the connected user without access to it (Google refused it
    # several syncs in a row) and retired it. Nothing stored of it is served: the
    # summary only says 'needs_reconnect', and no sync is asked for.
    access_lost: bool = False
    location_id: str = ""  # ours: "loc_..."


@dataclass(frozen=True)
class ActiveProfile:
    """An active profile of an account, as Google names it: what a sync needs."""

    location_id: str
    google_sub: str
    location: str  # "locations/123"


@dataclass(frozen=True)
class LocationOverview:
    """A profile of the account for the selector of the panel (docs/multinegocio.md, 5.2).

    `summary_status` is the status of its summary (ok, needs_reconnect or error), None
    when it is inactive or nothing has been synced yet; `summary_status_at` is its last
    attempt and `sync_requested_at` the newest sync of it waiting or running in the
    queue (shown_status, app/domain/profile_summary.py). `pending_reviews` and
    `pending_key_dates` are None when it is inactive."""

    id: str
    title: str
    place_id: str | None
    verified: bool
    plan: Plan | None = None
    active_since: datetime | None = None
    summary_status: str | None = None
    pending_reviews: int | None = None
    pending_key_dates: int | None = None
    summary_status_at: datetime | None = None
    sync_requested_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.plan is not None


@dataclass(frozen=True)
class LocationsOverview:
    """Every profile of the account, with what it may have active: what GET /me needs
    for `locations`, `plans` and `pricing` (app/domain/plans.py)."""

    locations: tuple[LocationOverview, ...]
    entitlements: tuple[Entitlement, ...]

    def active_by_plan(self) -> dict[Plan, int]:
        counts: dict[Plan, int] = {}
        for location in self.locations:
            if location.plan is not None:
                counts[location.plan] = counts.get(location.plan, 0) + 1
        return counts

    def active(self) -> list[ActiveLocation]:
        return [
            ActiveLocation(loc.id, loc.plan, loc.active_since)
            for loc in self.locations
            if loc.plan is not None and loc.active_since is not None
        ]


def lost_access_summary(at: datetime) -> StoredSummary:
    """What is shown of a profile whose access was lost: that, and nothing else."""
    return StoredSummary(
        status=STATUS_NEEDS_RECONNECT,
        status_at=at,
        synced_at=None,
        review_count=None,
        average_rating=None,
        unanswered_count=None,
        unanswered_low_count=None,
        last_review_at=None,
        has_regular_hours=None,
        upcoming_special_hours=None,
        google_changed=None,
    )


@dataclass(frozen=True)
class ReviewDraft:
    """The last reply a model drafted for a review, and what it was drafted from."""

    text: str
    source_hash: str


@dataclass(frozen=True)
class StoredReview:
    """A review as the API reads it. Nothing about who wrote it is stored."""

    id: str  # ours: "rev_..."; of that account only
    rating: int | None
    text: str  # original language
    created_at: datetime | None
    reply_text: str | None  # None: unanswered
    reply_at: datetime | None
    draft: ReviewDraft | None = None  # only read by get_review
    location_id: str | None = None  # ours, of the profile it is of; only read by get_review


@dataclass(frozen=True)
class ReviewPage:
    reviews: tuple[StoredReview, ...]
    total: int  # how many match the filter, before the limit


@dataclass(frozen=True)
class StoredAction:
    id: str  # "act_..."
    type: str
    review_id: str | None  # None for a change of hours
    state: str  # pending, running, done or failed
    error_code: str | None
    confirmed_at: datetime
    finished_at: datetime | None
    special_date: date | None = None  # set_special_hours: the day it changes


@dataclass(frozen=True)
class StoredSentiment:
    """The sentiment analysis of a profile as the API reads it, with its trend.
    The classification of each review is never read by the API."""

    analysis: Analysis
    history: tuple[tuple[date, int], ...] = ()  # (first day of the month, score), oldest first


class ReplyInProgress(Exception):
    """That review already has a reply on its way to Google."""


# --- which profile: errors the routes translate (docs/multinegocio.md, 5.1 and 5.3) ----


class NoLocationChosen(Exception):
    """No profile named and no preference: 409 «Primero elige con qué ficha quieres
    trabajar.»"""


class LocationNotFound(Exception):
    """Not a profile of that account: of another one, or it does not exist. The same
    answer in both cases: 404 «Esa ficha no está en tu cuenta.»"""


class LocationInactive(Exception):
    """A profile of that account that is not active: 409 «Esa ficha no está activa.»"""


class ActivationRefused(Exception):
    """The database refused to activate a profile or change its plan (trigger
    location_active_guard). `code` is what the routes answer with."""

    code = "refused"


class LocationLimit(ActivationRefused):
    """No room left in that plan: 402, and the web shows the offer of plans."""

    code = "limit"


class FreeTaken(ActivationRefused):
    """That Google profile is already active on the free plan in another account
    (decision 5 of the owner): 409 with code free_taken."""

    code = "free_taken"


class OffboardingPending(ActivationRefused):
    """The account has a scheduled leave: nothing can be activated. 409 with code
    offboarding_pending."""

    code = "offboarding_pending"


class LocationRemoved(ActivationRefused):
    """Google no longer lists that profile for the account: 409."""

    code = "location_removed"


class Store(Protocol):
    async def add_signup(self, signup: WaitlistSignup) -> None:
        """Save a signup. Saving the same contact twice is not an error."""
        ...

    async def record_event(
        self,
        kind: str,
        source: str | None = None,
        place_id: str | None = None,
        score: int | None = None,
    ) -> None: ...

    async def add_google_connection(self, connection: GoogleConnection) -> None:
        """Append a connection. A reconnection is a new row; the newest one wins."""
        ...

    async def upsert_account(self, google_sub: str, email: str) -> str:
        """Create the account of that Google identity, or refresh its email. Returns its id.

        Connecting again after a leave of the profile makes the account connected again.
        A deleted account is never found: the same Google identity gets a new, empty one.
        """
        ...

    async def sync_locations(
        self, account_id: str, locations: Sequence[ProfileLocation], now: datetime
    ) -> None:
        """Make the account's profiles match what Google lists now.

        Known profiles keep their id. Profiles no longer listed are retired, never
        deleted, and stop being the selected one.
        """
        ...

    async def create_session(
        self, account_id: str, token_hash: str, now: datetime, expires_at: datetime
    ) -> None: ...

    async def account_for_session(self, token_hash: str, now: datetime) -> Account | None:
        """The account behind a live session, with the id of that session and its
        scheduled leave if it has one. None if the session is unknown, expired or
        revoked, or the account is deleted."""
        ...

    async def revoke_session(self, token_hash: str, now: datetime) -> None: ...

    async def list_locations(self, account_id: str) -> list[Location]:
        """Profiles of that account only, ordered by title."""
        ...

    async def select_location(self, account_id: str, location_id: str) -> bool:
        """Make an ACTIVE profile the preference of the panel (the profile it shows
        when a request names none). It activates nothing (step 9 of
        docs/multinegocio.md): an inactive profile of that account raises
        LocationInactive, and the web activates it first. False, and nothing changes,
        if it is not of that account or Google no longer lists it.
        """
        ...

    # --- profiles of the account, active or not (docs/multinegocio.md) -------------

    async def resolve_location(self, account_id: str, location_id: str | None) -> str:
        """The profile a request works on: `location_id` or, if None, the preference.

        Raises NoLocationChosen (None and no preference), LocationNotFound (not of that
        account; also a named profile Google no longer lists) or LocationInactive. A
        preferred profile Google no longer lists is returned: `profile` serves it with
        access_lost.
        """
        ...

    async def locations_overview(self, account_id: str, today: date) -> LocationsOverview:
        """Every profile of that account Google lists, ordered as list_locations, each
        with its plan and, if active, the status of its summary, its unanswered reviews
        and its key dates still to answer on `today` or later; and the entitlements of
        the account."""
        ...

    async def quota_used(self, scope: str, start: datetime) -> int:
        """app/ports/quota.py: the store is also the quota store of the API."""
        ...

    async def location_plan(self, account_id: str, location_id: str) -> Plan | None:
        """The plan of that profile if it is an active one of that account, without a
        scheduled leave; None otherwise (of another account, unknown or inactive)."""
        ...

    async def active_profiles(self, account_id: str) -> list[ActiveProfile]:
        """The active profiles of that account, as Google names them. Empty while the
        account has a scheduled leave."""
        ...

    async def activate(self, account_id: str, location_id: str, plan: Plan | None = None) -> Plan:
        """Activate a profile of that account, or change the plan of an active one.
        Returns the plan it ends up with.

        Without a plan: an active profile stays as it is; an inactive one gets the free
        plan while it is left (also in the other accounts: decision 5) and otherwise the
        first paid plan with room (app/domain/plans.py, default_plan).

        Raises LocationNotFound, LocationRemoved, LocationLimit, FreeTaken or
        OffboardingPending. The database decides (trigger location_active_guard): two
        activations at once are counted one after the other.
        """
        ...

    async def deactivate(self, account_id: str, location_id: str) -> bool:
        """Deactivate a profile of that account. In one transaction, holding the thread
        of the conversation: the profile stops being active, its open proposals are
        cancelled (outcome location_inactive, new token) and its lane lets them go. If
        it was the preference, the preference moves to the oldest active profile, or to
        none. False, and nothing changes, if it is not of that account.
        Deactivating an inactive profile changes nothing and is not an error."""
        ...

    async def profile(
        self, account_id: str, location_id: str | None = None
    ) -> SelectedProfile | None:
        """That active profile of that account and its summary; with None, the
        preference. None if it is not an active profile of that account (of another
        account, unknown or inactive) or the account has a scheduled leave.

        A preferred profile the worker retired for lack of access comes back with
        access_lost and lost_access_summary, without anything that was read from Google.

        The summary is looked up by the Google identity of that very account and its own
        profile: an account never reads the summary written for another one, not even
        when both manage the same Google profile.
        """
        ...

    async def list_reviews(
        self,
        account_id: str,
        *,
        unanswered_only: bool,
        limit: int,
        location_id: str | None = None,
    ) -> ReviewPage:
        """Reviews of that active profile of that account (None: the preference), and
        of no other.

        Unanswered first; within each group, fewer stars first and then the newest.
        Removed reviews are left out. Empty when it is not an active profile of that
        account.
        """
        ...

    async def get_review(self, account_id: str, review_id: str) -> StoredReview | None:
        """One review of any active profile of that account, with its stored draft and
        the id of its profile. None if it is not there: unknown, removed, of an inactive
        profile or of another account. The answer is the same in every case."""
        ...

    async def save_draft(
        self, account_id: str, review_id: str, source_hash: str, text: str, now: datetime
    ) -> None:
        """Keep the last draft of that review, replacing the previous one. Nothing is
        stored unless the review is of an active profile of that account."""
        ...

    async def create_reply_action(
        self, account_id: str, review_id: str, text: str, now: datetime
    ) -> str | None:
        """Record that the owner confirmed `text` as the reply to that review: a pending
        action, its audit row and the job that will carry it out, all or nothing.

        `text` is stored exactly as given. Returns the id of the action; None if the
        review is not of an active profile of that account. Raises ReplyInProgress
        if that review already has a reply on its way.
        """
        ...

    async def get_action(self, account_id: str, action_id: str) -> StoredAction | None:
        """An action of that account. None if it is unknown or of another account."""
        ...

    async def sentiment(
        self, account_id: str, now: datetime, location_id: str | None = None
    ) -> StoredSentiment | None:
        """The sentiment analysis of that active profile of that account (None: the
        preference), and of no other, with the scores of the last twelve months. None if
        there is none yet or it is not an active profile of that account.

        Looked up like the summary: by the Google identity of that very account and its
        own profile.
        """
        ...

    # --- leaving: code, scheduled leave and restore -------------------------------

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
        """Keep the fingerprint of a new code, void the earlier ones of that account and
        enqueue the email that carries it (`mail` is the payload of that job; the id of
        the code is added to it), all or nothing. The code itself is never stored here."""
        ...

    async def offboarding_code_mail(self, account_id: str) -> str | None:
        """How the email of the latest code of that account went: pending, sent or
        failed. None if it has no code that can still be used."""
        ...

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
        """Try a code and, if it is the right one, schedule the leave: all or nothing.

        `candidate_hash` is the fingerprint of what was typed, for the session and the
        scope of the request; it is compared in constant time with the stored one of
        the latest code of the account. Every try counts; at `max_attempts` the code
        is dead. A right code is spent, the leave is scheduled for `delete_at`, the
        email with the restore link is enqueued and, when the scope is the account,
        every session of the account is closed.
        """
        ...

    async def restore_offboarding(self, account_id: str, now: datetime, *, mail: dict) -> bool:
        """Cancel the scheduled leave of that account. False if it has none.

        In the same write, all or nothing, the email that tells the owner is enqueued:
        `mail` is the payload of that job, to which `to` (the address the account has at
        this moment) and `scope` (of the leave) are added. If the account has no
        address left, the leave is restored and no email is enqueued.
        """
        ...

    async def restore_offboarding_by_token(
        self, token_hash: str, now: datetime, *, mail: dict
    ) -> str | None:
        """Cancel the scheduled leave that restore token belongs to and return its
        account. None if the token is unknown, used, or its leave is past its date.

        The email is enqueued as in restore_offboarding. The token is spent by the very
        write that restores: if anything fails, it is still valid."""
        ...
