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
