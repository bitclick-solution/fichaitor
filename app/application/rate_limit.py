import time
from collections.abc import Callable


class SlidingWindowLimiter:
    """Per-key limiter in process memory. Moves to PostgreSQL with more than one API process."""

    def __init__(self, limit: int, window_seconds: int, clock: Callable[[], float] = time.time):
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = self._clock()
        recent = [t for t in self._hits.get(key, []) if now - t < self._window]
        allowed = len(recent) < self._limit
        if allowed:
            recent.append(now)
        self._hits[key] = recent
        return allowed
