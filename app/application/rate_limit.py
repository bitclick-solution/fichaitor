import time
from collections.abc import Callable

# Keys kept at most. 200,000 keys took 36 MB (docs/seguridad/revision-2026-11-12.md, B2);
# this many take well under 2 MB.
DEFAULT_MAX_KEYS = 10_000
# How often, at most, a full limiter looks for keys whose hits have all expired.
_SWEEP_SECONDS = 1.0


class SlidingWindowLimiter:
    """Per-key limiter in process memory, for limits per client (IP address, account).

    It does not protect the bill: with rotated addresses it is gone round, and a restart
    starts it from zero. The caps on paid calls are counted in PostgreSQL
    (app/domain/quota.py). This one stays bounded: a key whose hits have all expired is
    dropped, and at most `max_keys` keys are kept; past that the oldest one is forgotten.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: int,
        clock: Callable[[], float] = time.time,
        max_keys: int = DEFAULT_MAX_KEYS,
    ):
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._max_keys = max(1, max_keys)
        self._hits: dict[str, list[float]] = {}
        self._swept_at = float("-inf")

    def _recent(self, key: str, now: float) -> list[float]:
        """The hits of that key still inside the window. A key left without any is dropped."""
        hits = self._hits.get(key)
        if not hits:
            self._hits.pop(key, None)
            return []
        recent = [t for t in hits if now - t < self._window]
        if recent:
            self._hits[key] = recent
        else:
            del self._hits[key]
        return recent

    def _make_room(self, now: float) -> None:
        if len(self._hits) < self._max_keys:
            return
        if now - self._swept_at >= _SWEEP_SECONDS:
            self._swept_at = now
            for key in list(self._hits):
                self._recent(key, now)
        while len(self._hits) >= self._max_keys:
            del self._hits[next(iter(self._hits))]  # the oldest key

    def peek(self, key: str) -> bool:
        """Whether `allow` would let this one through now. Counts nothing."""
        return len(self._recent(key, self._clock())) < self._limit

    def allow(self, key: str) -> bool:
        now = self._clock()
        recent = self._recent(key, now)
        if len(recent) >= self._limit:
            return False
        if key not in self._hits:
            self._make_room(now)
        recent.append(now)
        self._hits[key] = recent
        return True
