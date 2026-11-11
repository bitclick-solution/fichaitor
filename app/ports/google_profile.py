"""Read access to Google Business Profile, as the worker needs it."""

from typing import Protocol

from app.domain.profile_summary import MediaSummary, ProfileDetails, ReviewSet


class GoogleProfileError(Exception):
    """Google did not answer properly (5xx, 429, network). Worth retrying later.
    The message never carries tokens or response bodies."""


class GoogleRejected(GoogleProfileError):
    """Google refused the request (4xx). Retrying the same call will not help."""


class ReconnectRequired(Exception):
    """The owner's grant is gone (revoked or expired): they must connect again.
    Never retried."""


class GoogleProfile(Protocol):
    async def refresh_access_token(self, refresh_token: str) -> str: ...

    async def reviews(self, access_token: str, account: str, location: str) -> ReviewSet: ...

    async def details(self, access_token: str, location: str) -> ProfileDetails:
        """The profile as its owner has it: hours, categories, description, contact."""
        ...

    async def attribute_count(self, access_token: str, location: str) -> int:
        """How many attributes the owner has declared."""
        ...

    async def media(self, access_token: str, account: str, location: str) -> MediaSummary:
        """How many photos and videos the owner has uploaded, and when the last one."""
        ...

    async def google_updated_fields(self, access_token: str, location: str) -> tuple[str, ...]:
        """Fields that Google has changed on its own (diffMask of getGoogleUpdated)."""
        ...
