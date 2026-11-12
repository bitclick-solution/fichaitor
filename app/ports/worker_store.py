"""What the worker reads and writes in the database, besides the job queue."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.domain.offboarding import RetentionPolicy
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
class EffectiveLeave:
    """A leave that is already effective and whose tokens are still to be revoked."""

    id: str
    scope: str
    google_sub: str


@dataclass(frozen=True)
class TokenToRevoke:
    connection_id: str
    refresh_token_enc: str
    key_version: int


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

    # --- leaving and retention ----------------------------------------------------

    async def is_suspended(self, google_sub: str) -> bool:
        """Whether that Google user has a scheduled leave: nothing of theirs is read,
        analysed or sent until it is restored."""
        ...

    async def purge_superseded(self, google_sub: str, now: datetime) -> dict[str, int]:
        """Delete what is left of earlier, effective leaves of that Google user who has
        connected again since: a new connection starts from nothing. Rows deleted per
        table; empty when there was nothing to do."""
        ...

    async def begin_offboarding(self, offboarding_id: str, now: datetime) -> EffectiveLeave | None:
        """Make a scheduled leave effective if its date has come: from then on it cannot
        be restored, the account has no profiles and, when the scope is the account,
        the account is deleted and its sessions closed. Safe to repeat.

        Returns the leave while its tokens are still to be revoked; None if it was
        restored, is not due yet, does not exist or is already finished.
        """
        ...

    async def leave_tokens(self, offboarding_id: str) -> tuple[list[TokenToRevoke], bool]:
        """Every stored connection of that leave's Google user made before it became
        effective, and whether the owner has connected again since (then revoking
        would cut the new connection: the old tokens are only deleted)."""
        ...

    async def delete_connection(self, connection_id: str) -> None:
        """Delete one stored connection, encrypted token included."""
        ...

    async def finish_revocation(self, offboarding_id: str, state: str) -> None:
        """Record how the revocation ended. From then on the leave is not picked again."""
        ...

    async def due_offboardings(self, now: datetime) -> list[tuple[str, bool]]:
        """Leaves the daily pass must look at: scheduled ones past their date and
        effective ones whose revocation never finished. The flag says to give up on
        Google (the revocation has been pending for more than a day)."""
        ...

    async def run_retention(self, now: datetime, policy: RetentionPolicy) -> dict[str, int]:
        """Delete everything past its term, and nothing else, and record how many rows
        went from each table. Safe to repeat and to run from several workers."""
        ...

    async def scrub_job(self, job_id: str) -> None:
        """Empty the payload of a job: what it carried must not outlive it."""
        ...

    async def scrub_finished_jobs(self, kind: str) -> int:
        """Empty the payload of every finished job of that kind that still has one."""
        ...

    async def set_code_mail(self, code_id: str, state: str) -> None:
        """Record whether the email with a code went out."""
        ...
