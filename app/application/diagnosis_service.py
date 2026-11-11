"""Use case: run a diagnosis, with per-client rate limiting.

Public diagnoses search by free text; the diagnosis of an owner's profile looks the
place up by its identifier. Both ask Google for the content of the place every time.

What is kept between requests is only what Google's terms allow: the search text and
the place identifier it led to. Name, address, figures and the diagnosis built from
them are never kept (docs/fase0/places-coste.md, section 5). Every diagnosis is
therefore a paid request, and every diagnosis counts towards the client's limit.

The identifiers and the counters live in process memory for now. They move to
PostgreSQL when the service runs with more than one API process.
"""

import time
from collections.abc import Callable

from app.domain.diagnosis import Diagnosis, PlaceSnapshot, diagnose
from app.ports.places import PlacesGateway

MAX_REMEMBERED_SEARCHES = 10_000


class RateLimited(Exception):
    pass


class DiagnosisService:
    def __init__(
        self,
        places: PlacesGateway,
        *,
        rate_limit: int,
        rate_window_seconds: int,
        place_id_cache_seconds: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._places = places
        self._rate_limit = rate_limit
        self._rate_window = rate_window_seconds
        self._place_id_seconds = place_id_cache_seconds
        self._clock = clock
        self._hits: dict[str, list[float]] = {}
        # search text -> (when it was resolved, place identifier). Nothing else from Google.
        self._place_ids: dict[str, tuple[float, str]] = {}

    def _check_rate(self, client: str) -> None:
        now = self._clock()
        recent = [t for t in self._hits.get(client, []) if now - t < self._rate_window]
        if len(recent) >= self._rate_limit:
            self._hits[client] = recent
            raise RateLimited
        recent.append(now)
        self._hits[client] = recent

    def _remembered_id(self, text: str) -> str | None:
        entry = self._place_ids.get(text)
        if entry is None:
            return None
        if self._clock() - entry[0] >= self._place_id_seconds:
            del self._place_ids[text]
            return None
        return entry[1]

    def _remember_id(self, text: str, place_id: str) -> None:
        self._place_ids.pop(text, None)
        while len(self._place_ids) >= MAX_REMEMBERED_SEARCHES:
            del self._place_ids[next(iter(self._place_ids))]  # the oldest one
        self._place_ids[text] = (self._clock(), place_id)

    async def _place_by_text(self, query: str) -> PlaceSnapshot | None:
        text = " ".join(query.lower().split())
        place_id = self._remembered_id(text)
        if place_id is not None:
            place = await self._places.get(place_id)
            if place is not None:
                return place
            del self._place_ids[text]  # Google retired that identifier: search again
        place_id = await self._places.find_id(text)
        if place_id is None:
            return None
        place = await self._places.get(place_id)
        if place is not None:
            self._remember_id(text, place_id)
        return place

    async def run(self, query: str, client: str) -> Diagnosis | None:
        self._check_rate(client)
        place = await self._place_by_text(query)
        return diagnose(place) if place else None

    async def run_for_profile(
        self, place_id: str | None, title: str, client: str
    ) -> Diagnosis | None:
        """Diagnose an owner's profile: by place identifier, else by its title."""
        self._check_rate(client)
        place = await self._places.get(place_id) if place_id else None
        if place is None and title.strip():
            place = await self._place_by_text(title)
        return diagnose(place) if place else None
