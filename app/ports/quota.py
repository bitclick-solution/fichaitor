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

    async def take_quota_in(self, scope: str, limit: int, start: datetime, end: datetime) -> bool:
        """`take_quota` in a window given by its start and end, for windows that are not
        of a fixed length (a calendar month: app/domain/quota.py, `month_window`)."""
        ...

    async def quota_used(self, scope: str, start: datetime) -> int:
        """Uses counted in the window of `scope` that starts at `start`. Counts nothing."""
        ...
