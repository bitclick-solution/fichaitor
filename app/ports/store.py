from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from app.domain.offboarding import CodeCheck, PendingDeletion
from app.domain.profile_summary import ProfileFacts
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


@dataclass(frozen=True)
class SelectedProfile:
    """The selected profile of an account, as Google names it, and its summary if any."""

    google_sub: str
    location: str  # "locations/123"
    summary: StoredSummary | None
    title: str = ""
    place_id: str | None = None


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


@dataclass(frozen=True)
class ReviewPage:
    reviews: tuple[StoredReview, ...]
    total: int  # how many match the filter, before the limit


@dataclass(frozen=True)
class StoredAction:
    id: str  # "act_..."
    type: str
    review_id: str
    state: str  # pending, running, done or failed
    error_code: str | None
    confirmed_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class StoredSentiment:
    """The sentiment analysis of a profile as the API reads it, with its trend.
    The classification of each review is never read by the API."""

    analysis: Analysis
    history: tuple[tuple[date, int], ...] = ()  # (first day of the month, score), oldest first


class ReplyInProgress(Exception):
    """That review already has a reply on its way to Google."""


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
        """Select a profile. False, and nothing changes, if it is not of that account."""
        ...

    async def selected_profile(self, account_id: str) -> SelectedProfile | None:
        """The selected profile of that account and its summary. None if nothing is selected.

        The summary is looked up by the Google identity of that very account and its own
        selected profile: an account never reads the summary written for another one,
        not even when both manage the same Google profile.
        """
        ...

    async def list_reviews(
        self, account_id: str, *, unanswered_only: bool, limit: int
    ) -> ReviewPage:
        """Reviews of the selected profile of that account, and of no other.

        Unanswered first; within each group, fewer stars first and then the newest.
        Removed reviews are left out. Empty when no profile is selected.
        """
        ...

    async def get_review(self, account_id: str, review_id: str) -> StoredReview | None:
        """One review of the selected profile of that account, with its stored draft.
        None if it is not there: unknown, removed, of another profile or of another
        account. The answer is the same in every case."""
        ...

    async def save_draft(
        self, account_id: str, review_id: str, source_hash: str, text: str, now: datetime
    ) -> None:
        """Keep the last draft of that review, replacing the previous one. Nothing is
        stored unless the review is of the selected profile of that account."""
        ...

    async def create_reply_action(
        self, account_id: str, review_id: str, text: str, now: datetime
    ) -> str | None:
        """Record that the owner confirmed `text` as the reply to that review: a pending
        action, its audit row and the job that will carry it out, all or nothing.

        `text` is stored exactly as given. Returns the id of the action; None if the
        review is not of the selected profile of that account. Raises ReplyInProgress
        if that review already has a reply on its way.
        """
        ...

    async def get_action(self, account_id: str, action_id: str) -> StoredAction | None:
        """An action of that account. None if it is unknown or of another account."""
        ...

    async def sentiment(self, account_id: str, now: datetime) -> StoredSentiment | None:
        """The sentiment analysis of the selected profile of that account, and of no
        other, with the scores of the last twelve months. None if there is none yet or
        no profile is selected.

        Looked up like the summary: by the Google identity of that very account and its
        own selected profile.
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

    async def restore_offboarding(self, account_id: str, now: datetime) -> bool:
        """Cancel the scheduled leave of that account. False if it has none."""
        ...

    async def restore_offboarding_by_token(self, token_hash: str, now: datetime) -> str | None:
        """Cancel the scheduled leave that restore token belongs to and return its
        account. None if the token is unknown, used, or its leave is past its date."""
        ...
