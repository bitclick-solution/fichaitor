from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

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


@dataclass(frozen=True)
class SelectedProfile:
    """The selected profile of an account, as Google names it, and its summary if any."""

    google_sub: str
    location: str  # "locations/123"
    summary: StoredSummary | None


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
        """Create the account of that Google identity, or refresh its email. Returns its id."""
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
        """The account behind a live session. None if unknown, expired or revoked."""
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
