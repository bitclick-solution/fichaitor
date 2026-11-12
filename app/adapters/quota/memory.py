"""Caps on paid calls in process memory: for tests and for local development without
a database. Production counts in PostgreSQL (PostgresStore.take_quota)."""

from datetime import datetime

from app.domain.quota import window


class MemoryQuota:
    def __init__(self) -> None:
        # (scope, window start) -> (window end, uses counted)
        self._counters: dict[tuple[str, datetime], tuple[datetime, int]] = {}

    def _hits(self, scope: str, window_seconds: int, now: datetime) -> tuple[datetime, int]:
        # Past windows are forgotten as they go: the dictionary only holds live ones.
        for key, (end, _) in list(self._counters.items()):
            if end <= now:
                del self._counters[key]
        start, end = window(now, window_seconds)
        return start, self._counters.get((scope, start), (end, 0))[1]

    async def take_quota(self, scope: str, limit: int, window_seconds: int, now: datetime) -> bool:
        start, hits = self._hits(scope, window_seconds, now)
        if hits >= limit:
            return False
        self._counters[(scope, start)] = (window(now, window_seconds)[1], hits + 1)
        return True

    async def quota_left(self, scope: str, limit: int, window_seconds: int, now: datetime) -> bool:
        return self._hits(scope, window_seconds, now)[1] < limit
