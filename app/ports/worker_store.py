"""What the worker reads and writes in the database, besides the job queue."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.domain.profile_summary import ProfileSummary


@dataclass(frozen=True)
class ConnectedLocation:
    account: str  # "accounts/123"
    location: str  # "locations/456"


@dataclass(frozen=True)
class StoredConnection:
    refresh_token_enc: str
    key_version: int
    locations: tuple[ConnectedLocation, ...]


class WorkerStore(Protocol):
    async def latest_connection(self, google_sub: str) -> StoredConnection | None:
        """The most recent connection of that Google user: the only valid one."""
        ...

    async def save_summary(
        self, google_sub: str, location: str, summary: ProfileSummary, at: datetime
    ) -> None:
        """Insert or replace the summary of a profile and its facts, with status 'ok'."""
        ...

    async def save_status(self, google_sub: str, location: str, status: str, at: datetime) -> None:
        """Set only the status; the figures and facts of the last good sync are kept."""
        ...

    async def mark_lost_syncs(self, kind: str) -> int:
        """Set status 'error' on the profiles whose sync job of that kind was closed
        as failed because its worker died during the last attempt, unless the summary
        already has a later status. Safe to repeat. Returns how many were set."""
        ...
