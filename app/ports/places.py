from typing import Protocol

from app.domain.diagnosis import PlaceSnapshot


class PlacesUnavailable(Exception):
    """The places provider failed or timed out."""


class PlacesGateway(Protocol):
    """Two steps: text to identifier, identifier to content.

    Only the identifier may be kept between requests; the content of a place may not.
    """

    async def find_id(self, query: str) -> str | None:
        """Return the identifier of the best public match for a free-text query, or None."""
        ...

    async def get(self, place_id: str) -> PlaceSnapshot | None:
        """Return the place with that identifier, or None if the provider does not know it."""
        ...
