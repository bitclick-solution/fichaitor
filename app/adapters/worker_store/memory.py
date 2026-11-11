"""In-memory worker store for tests."""

from dataclasses import dataclass
from datetime import datetime

from app.adapters.job_queue.memory import LOST_WORKER_ERROR, MemoryJobQueue
from app.domain.jobs import FAILED
from app.domain.profile_summary import STATUS_ERROR, STATUS_OK, ProfileSummary
from app.ports.worker_store import StoredConnection


@dataclass
class StoredSummary:
    status: str
    status_at: datetime
    synced_at: datetime | None = None
    summary: ProfileSummary | None = None


class MemoryWorkerStore:
    def __init__(self, queue: MemoryJobQueue | None = None) -> None:
        self._queue = queue  # only mark_lost_syncs reads it, as the real one reads `job`
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

    async def mark_lost_syncs(self, kind: str) -> int:
        marked = 0
        for job in list(self._queue.jobs.values()) if self._queue else []:
            if not (job.kind == kind and job.state == FAILED and job.finished_at):
                continue
            if job.last_error != LOST_WORKER_ERROR:
                continue
            google_sub, location = job.payload.get("google_sub"), job.payload.get("location")
            if not (isinstance(google_sub, str) and google_sub):
                continue
            if not (isinstance(location, str) and location):
                continue
            current = self.summaries.get((google_sub, location))
            if current is None or current.status_at < job.finished_at:
                await self.save_status(google_sub, location, STATUS_ERROR, job.finished_at)
                marked += 1
        return marked
