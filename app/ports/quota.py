from datetime import datetime
from typing import Protocol


class QuotaUnavailable(Exception):
    """The counter could not be read or written. The caller must not make the paid call."""


class Quota(Protocol):
    """Counters of paid calls per scope and fixed window (app/domain/quota.py).

    In PostgreSQL in production, so that a restart or a second API process does not
    start them from zero. Implementations raise QuotaUnavailable when they fail.
    """

    async def take_quota(self, scope: str, limit: int, window_seconds: int, now: datetime) -> bool:
        """Count one use of `scope` in the window `now` falls in, unless `limit` uses
        have been counted there already. True if it was counted. Atomic: two processes
        asking at once never go over the limit."""
        ...

    async def quota_left(self, scope: str, limit: int, window_seconds: int, now: datetime) -> bool:
        """Whether one more use would be counted now. Counts nothing."""
        ...
