"""What the worker reads and writes in the database, besides the job queue."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.domain.profile_summary import ProfileSummary, Review


@dataclass(frozen=True)
class ConnectedLocation:
    account: str  # "accounts/123"
    location: str  # "locations/456"


@dataclass(frozen=True)
class StoredConnection:
    refresh_token_enc: str
    key_version: int
    locations: tuple[ConnectedLocation, ...]


@dataclass(frozen=True)
class ClaimedAction:
    """A confirmed action taken by a job. `text` is what the owner confirmed: the
    worker reads it and has no way to change it."""

    id: str
    google_sub: str
    location: str  # "locations/456"
    gbp_review_id: str  # Google's reviewId
    text: str
    attempts: int  # counts this one


class WorkerStore(Protocol):
    async def latest_connection(self, google_sub: str) -> StoredConnection | None:
        """The most recent connection of that Google user: the only valid one."""
        ...

    async def save_summary(
        self, google_sub: str, location: str, summary: ProfileSummary, at: datetime
    ) -> None:
        """Insert or replace the summary of a profile and its facts, with status 'ok'."""
        ...

    async def save_status(self, google_sub: str, location: str, status: str, at: datetime) -> None:
        """Set only the status; the figures and facts of the last good sync are kept."""
        ...

    async def mark_lost_syncs(self, kind: str) -> int:
        """Set status 'error' on the profiles whose sync job of that kind was closed
        as failed because its worker died during the last attempt, unless the summary
        already has a later status. Safe to repeat. Returns how many were set."""
        ...

    async def save_reviews(
        self, google_sub: str, location: str, reviews: Sequence[Review], at: datetime
    ) -> None:
        """Make the stored reviews of that profile match what Google returned.

        Known reviews keep their id. Reviews no longer returned are marked as removed,
        never deleted. Reviews without a well-formed Google id are skipped. Nothing
        about who wrote a review is stored.
        """
        ...

    async def recent_replies(
        self, google_sub: str, location: str, since: datetime
    ) -> dict[str, tuple[str, datetime | None]]:
        """Replies Fichaitor sent to reviews of that profile after `since`: Google's
        review id to the text and its date. Google may not list them yet."""
        ...

    async def claim_action(self, action_id: str, job_id: str, at: datetime) -> ClaimedAction | None:
        """Take a confirmed action for that job and mark it as running.

        None when there is nothing for this job to do: the action does not exist, is
        already finished, or belongs to another job. The same job can claim it again
        (a retry).
        """
        ...

    async def finish_action(
        self,
        action_id: str,
        job_id: str,
        *,
        done: bool,
        at: datetime,
        google_status: int | None = None,
        reason: str | None = None,
        reply_at: datetime | None = None,
    ) -> bool:
        """Close the action that job holds and write its audit row, both or neither.

        done: the stored review becomes answered with the confirmed text and, if it
        was unanswered, the unanswered counts of the summary go down. Otherwise
        `reason` is the error code.
        False if that job no longer holds the action.
        """
        ...

    async def mark_lost_actions(self, kind: str) -> int:
        """Close as failed the live actions whose job of that kind ended as failed
        without closing them (its worker died, or it ran out of attempts). Safe to
        repeat. Returns how many were closed."""
        ...
