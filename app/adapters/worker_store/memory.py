"""In-memory worker store for tests."""

from dataclasses import dataclass
from datetime import datetime

from app.domain.profile_summary import STATUS_OK, ProfileSummary
from app.ports.worker_store import StoredConnection


@dataclass
class StoredSummary:
    status: str
    status_at: datetime
    synced_at: datetime | None = None
    summary: ProfileSummary | None = None


class MemoryWorkerStore:
    def __init__(self) -> None:
        self.connections: dict[str, list[StoredConnection]] = {}
        self.summaries: dict[tuple[str, str], StoredSummary] = {}

    def add_connection(self, google_sub: str, connection: StoredConnection) -> None:
        self.connections.setdefault(google_sub, []).append(connection)

    async def latest_connection(self, google_sub: str) -> StoredConnection | None:
        history = self.connections.get(google_sub)
        return history[-1] if history else None

    async def save_summary(
        self, google_sub: str, location: str, summary: ProfileSummary, at: datetime
    ) -> None:
        self.summaries[(google_sub, location)] = StoredSummary(STATUS_OK, at, at, summary)

    async def save_status(self, google_sub: str, location: str, status: str, at: datetime) -> None:
        current = self.summaries.get((google_sub, location))
        if current is None:
            self.summaries[(google_sub, location)] = StoredSummary(status, at)
        else:
            current.status, current.status_at = status, at
