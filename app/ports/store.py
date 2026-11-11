from dataclasses import dataclass
from typing import Protocol


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
