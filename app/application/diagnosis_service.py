"""Use case: run a diagnosis, with caching and per-client rate limiting.

Public diagnoses search by free text; the diagnosis of an owner's profile looks the
place up by its identifier. Both share the cache window and the rate limit.

Cache and counters live in process memory for now. They move to PostgreSQL
when the service runs with more than one API process.
"""

import time
from collections.abc import Awaitable, Callable
from datetime import date

from app.domain.diagnosis import Diagnosis, PlaceSnapshot, diagnose
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
        self._cache: dict[tuple[str, str], tuple[float, Diagnosis | None]] = {}

    def _check_rate(self, client: str) -> None:
        now = self._clock()
        recent = [t for t in self._hits.get(client, []) if now - t < self._rate_window]
        if len(recent) >= self._rate_limit:
            self._hits[client] = recent
            raise RateLimited
        recent.append(now)
        self._hits[client] = recent

    async def _cached(
        self,
        key: tuple[str, str],
        client: str,
        fetch: Callable[[], Awaitable[PlaceSnapshot | None]],
    ) -> Diagnosis | None:
        now = self._clock()
        cached = self._cache.get(key)
        if cached and now - cached[0] < self._cache_seconds:
            return cached[1]
        self._check_rate(client)
        place = await fetch()
        result = diagnose(place, self._today()) if place else None
        self._cache[key] = (now, result)
        return result

    async def run(self, query: str, client: str) -> Diagnosis | None:
        text = " ".join(query.lower().split())
        return await self._cached(("text", text), client, lambda: self._places.find(text))

    async def run_for_profile(
        self, place_id: str | None, title: str, client: str
    ) -> Diagnosis | None:
        """Diagnose an owner's profile: by place identifier, else by its title."""
        if place_id:
            result = await self._cached(
                ("id", place_id), client, lambda: self._places.get(place_id)
            )
            if result is not None:
                return result
        if not title.strip():
            return None
        return await self.run(title, client)
