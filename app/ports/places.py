from typing import Protocol

from app.domain.diagnosis import PlaceSnapshot


class PlacesUnavailable(Exception):
    """The places provider failed or timed out."""


class PlacesGateway(Protocol):
    async def find(self, query: str) -> PlaceSnapshot | None:
        """Return the best public match for a free-text query, or None."""
        ...

    async def get(self, place_id: str) -> PlaceSnapshot | None:
        """Return the place with that identifier, or None if the provider does not know it."""
        ...
