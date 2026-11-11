"""Use case: run a public diagnosis, with caching and per-client rate limiting.

Cache and counters live in process memory for now. They move to PostgreSQL
when the service runs with more than one API process.
"""

import time
from collections.abc import Callable
from datetime import date

from app.domain.diagnosis import Diagnosis, diagnose
from app.ports.places import PlacesGateway


class RateLimited(Exception):
    pass


class DiagnosisService:
    def __init__(
        self,
        places: PlacesGateway,
        *,
        rate_limit: int,
        rate_window_seconds: int,
        cache_seconds: int,
        clock: Callable[[], float] = time.time,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._places = places
        self._rate_limit = rate_limit
        self._rate_window = rate_window_seconds
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._today = today
        self._hits: dict[str, list[float]] = {}
        self._cache: dict[str, tuple[float, Diagnosis | None]] = {}

    def _check_rate(self, client: str) -> None:
        now = self._clock()
        recent = [t for t in self._hits.get(client, []) if now - t < self._rate_window]
        if len(recent) >= self._rate_limit:
            self._hits[client] = recent
            raise RateLimited
        recent.append(now)
        self._hits[client] = recent

    async def run(self, query: str, client: str) -> Diagnosis | None:
        key = " ".join(query.lower().split())
        now = self._clock()
        cached = self._cache.get(key)
        if cached and now - cached[0] < self._cache_seconds:
            return cached[1]
        self._check_rate(client)
        place = await self._places.find(key)
        result = diagnose(place, self._today()) if place else None
        self._cache[key] = (now, result)
        return result
