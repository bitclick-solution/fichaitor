from typing import Protocol

from app.domain.diagnosis import PlaceSnapshot


class PlacesUnavailable(Exception):
    """The places provider failed or timed out."""


class PlacesGateway(Protocol):
    async def find(self, query: str) -> PlaceSnapshot | None:
        """Return the best public match for a free-text query, or None."""
        ...
