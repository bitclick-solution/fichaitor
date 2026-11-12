"""Use case: run a public diagnosis, with per-client rate limiting.

Public diagnoses search by free text and ask Google for the content of the place
every time. The diagnosis of an owner's connected profile does not come through here:
it is computed from what the worker read (app/domain/profile_diagnosis.py).

What is kept between requests is only what Google's terms allow: the search text and
the place identifier it led to. Name, address, figures and the diagnosis built from
them are never kept (docs/fase0/places-coste.md, section 5). Every diagnosis is
therefore a paid request, and every diagnosis counts towards the client's limit.

Two limits, both checked before Google is asked: one per client IP (in process memory)
and a cap per day for all clients together (in the quota store, PostgreSQL in
production), which is what bounds the bill when someone rotates addresses.

The identifiers live in process memory for now. They move to PostgreSQL when the
service runs with more than one API process.
"""

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from app.adapters.quota.memory import MemoryQuota
from app.application.rate_limit import SlidingWindowLimiter
from app.domain.diagnosis import Diagnosis, PlaceSnapshot, diagnose
from app.domain.quota import DAY_SECONDS, DIAGNOSIS_DAY
from app.ports.places import PlacesGateway, PlacesUnavailable
from app.ports.quota import Quota

log = logging.getLogger("fichaitor.diagnosis")

MAX_REMEMBERED_SEARCHES = 10_000


class RateLimited(Exception):
    pass


class DailyLimitReached(Exception):
    """The cap on paid diagnoses per day, for all clients together, has been hit."""


class DiagnosisService:
    def __init__(
        self,
        places: PlacesGateway,
        *,
        rate_limit: int,
        rate_window_seconds: int,
        place_id_cache_seconds: int,
        daily_limit: int = 500,
        quota: Quota | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._places = places
        self._per_client = SlidingWindowLimiter(rate_limit, rate_window_seconds, clock)
        self._daily_limit = daily_limit
        self._quota = quota if quota is not None else MemoryQuota()
        self._place_id_seconds = place_id_cache_seconds
        self._clock = clock
        # search text -> (when it was resolved, place identifier). Nothing else from Google.
        self._place_ids: dict[str, tuple[float, str]] = {}

    async def _check_limits(self, client: str) -> None:
        if not self._per_client.allow(client):
            raise RateLimited
        now = datetime.fromtimestamp(self._clock(), UTC)
        try:
            taken = await self._quota.take_quota(DIAGNOSIS_DAY, self._daily_limit, DAY_SECONDS, now)
        except Exception as exc:  # no paid call without a working cap
            log.warning("tope diario de diagnósticos no disponible: %s", type(exc).__name__)
            raise PlacesUnavailable("quota") from None
        if not taken:
            log.warning("límite diario de diagnósticos alcanzado")
            raise DailyLimitReached

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
        await self._check_limits(client)
        place = await self._place_by_text(query)
        return diagnose(place) if place else None
