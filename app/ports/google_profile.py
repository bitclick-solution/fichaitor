"""Access to Google Business Profile, as the worker needs it: the reads of a sync and
the two writes: the reply to a review and the special hours of a day."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from app.domain.profile_summary import MediaSummary, ProfileDetails, ReviewSet


class GoogleProfileError(Exception):
    """Google did not answer properly (5xx, 429, network). Worth retrying later.
    The message never carries tokens, request bodies or response bodies.
    `status` is the HTTP status when there was one."""

    def __init__(self, message: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GoogleRejected(GoogleProfileError):
    """Google refused the request (4xx). Retrying the same call will not help."""


class ReconnectRequired(Exception):
    """The owner's grant is gone (revoked or expired): they must connect again.
    Never retried."""


class GrantRevoked(ReconnectRequired):
    """Google answered `invalid_grant` to the refresh of the token: that refresh token
    is dead for good (the owner removed the access in their Google account, or it
    expired). Only `refresh_access_token` raises it. Not a 5xx, a 429 or a network
    failure: those are GoogleProfileError and are tried again. The worker marks the
    connection as revoked and deletes its token (docs/retencion.md)."""


@dataclass(frozen=True)
class LocationHours:
    """The hours of a profile as Google has them now, in Google's own shapes:
    `regularHours.periods` and `specialHours.specialHourPeriods`."""

    regular: tuple[Mapping[str, Any], ...] = ()
    special: tuple[Mapping[str, Any], ...] = ()


class GoogleProfile(Protocol):
    async def refresh_access_token(self, refresh_token: str) -> str: ...

    async def revoke_token(self, refresh_token: str) -> None:
        """Revoke the owner's grant at Google. Returns normally when Google revoked it
        or says the token is no longer valid (there is nothing left to revoke).
        GoogleRejected when Google refuses the request for good; GoogleProfileError
        when it is worth trying again. One request and no retry inside."""
        ...

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

    async def current_reply(
        self, access_token: str, account: str, location: str, review_id: str
    ) -> str | None:
        """The owner's reply to that review as Google has it now; None if there is none.
        GoogleRejected with status 404 when the review no longer exists."""
        ...

    async def reply_to_review(
        self, access_token: str, account: str, location: str, review_id: str, text: str
    ) -> datetime | None:
        """Create the owner's reply to that review, or replace the one there is. THE
        WRITE: public, and Google notifies whoever wrote the review.

        `text` is sent exactly as given. One request and no retry inside: a temporary
        failure (GoogleProfileError) may or may not have reached Google, and deciding
        what to do next is up to the caller. Returns Google's update time if it gives one.
        """
        ...

    async def hours(self, access_token: str, location: str) -> LocationHours:
        """The regular and the special hours of the profile, read right now."""
        ...

    async def set_special_hours(
        self, access_token: str, location: str, periods: list[dict[str, Any]]
    ) -> None:
        """Replace the whole list of special hours of the profile with `periods`. THE
        WRITE: public once Google accepts it. The caller sends every period it read,
        changed only on the day it means to change.

        One request and no retry inside, as with the reply to a review.
        """
        ...
