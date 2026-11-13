"""Job sync_profile: read a Business Profile from Google and store its summary,
the facts the diagnosis is computed from and its reviews. When it ends, it enqueues
the sentiment analysis if one is due (see app/worker/analyze_sentiment.py).

Of each review it stores Google's id, the stars, the original text, the date and the
owner's reply if there is one. Never the name or the photo of whoever wrote it. A
review is data: nothing in its text is ever acted upon here.

Payload: {"google_sub": "...", "location": "locations/123"}.

The refresh token is decrypted here, lives only in local variables and is never
written anywhere: every error message leaving this module is scrubbed first.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from app.adapters.crypto.sealed import unseal
from app.domain.jobs import Job, JobError, PermanentJobError, safe_error
from app.domain.profile_summary import (
    ACCESS_LOST_AFTER,
    ACCESS_LOST_STATUSES,
    GBP_LOCATION,
    GOOGLE_SUB,
    STATUS_ERROR,
    STATUS_NEEDS_RECONNECT,
    collect_facts,
    summarize,
)
from app.domain.reviews import REPLY_GRACE, with_sent_replies
from app.ports.google_profile import (
    GoogleProfile,
    GoogleRejected,
    GrantRevoked,
    ReconnectRequired,
)
from app.ports.worker_store import WorkerStore
from app.worker import log
from app.worker.analyze_sentiment import SentimentTrigger
from app.worker.google_grant import record_revoked_grant
from app.worker.propose_review import ReviewProposals

KIND = "sync_profile"


async def _optional[T](what: str, read: Awaitable[T]) -> T | None:
    """A read the diagnosis can do without: if Google refuses it (4xx), the fact is
    left unknown instead of failing the whole sync. A lost grant or a temporary
    failure still goes up, as in any other read."""
    try:
        return await read
    except GoogleRejected:
        log.event("optional_read_rejected", logging.WARNING, read=what)
        return None


class SyncProfile:
    def __init__(
        self,
        store: WorkerStore,
        google: GoogleProfile,
        private_key: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sentiment: SentimentTrigger | None = None,
        proposals: ReviewProposals | None = None,
    ) -> None:
        self._store = store
        self._google = google
        self._private_key = private_key
        self._clock = clock
        # Asked once the reviews are stored; None: no sentiment analysis is chained.
        self._sentiment = sentiment
        # Told of the new reviews without a reply, to propose them by the messaging
        # channels (docs/canales.md, step 5). None: nothing is proposed.
        self._proposals = proposals

    async def __call__(self, job: Job) -> None:
        google_sub, location = _target(job)

        # A scheduled leave: the profile is not read until it is restored. Nothing is
        # written either: restoring asks for a new sync.
        if await self._store.is_suspended(google_sub):
            log.event("account_suspended", job_id=job.id, kind=job.kind)
            return
        # Blocked for non-payment (step 7 of docs/admin-facturacion.md): Google is not
        # called and nothing is written; what is stored stays. Once it pays, the next
        # sync of the planner (or of the panel) picks it up again.
        if await self._store.billing_blocked(google_sub):
            log.event("account_billing_blocked", job_id=job.id, kind=job.kind)
            return
        # Connected again after a leave: what was left of before goes first, so that
        # the new connection starts from nothing.
        purged = await self._store.purge_superseded(google_sub, self._clock())
        if purged:
            log.event("leave_purged_on_reconnect", job_id=job.id, deleted=purged)

        # Every way out of here leaves a status in the summary: the API cannot read
        # the queue, so the summary is the only place where the panel sees a failure.
        connection = await self._store.latest_connection(google_sub)
        if connection is None:
            await self._store.save_status(
                google_sub, location, STATUS_NEEDS_RECONNECT, self._clock()
            )
            raise PermanentJobError("no hay conexión con Google para ese usuario")
        # The account and location sent to Google come from the stored connection;
        # the payload only chooses among them.
        target = next((loc for loc in connection.locations if loc.location == location), None)
        if target is None:
            await self._store.save_status(google_sub, location, STATUS_ERROR, self._clock())
            raise PermanentJobError("la ficha no pertenece a la conexión actual")

        try:
            refresh_token = unseal(self._private_key, connection.refresh_token_enc)
        except Exception:
            await self._store.save_status(google_sub, location, STATUS_ERROR, self._clock())
            raise PermanentJobError(
                f"no se pudo descifrar el token (versión de clave {connection.key_version})"
            ) from None

        secrets = [refresh_token]
        have_access = False
        try:
            access_token = await self._google.refresh_access_token(refresh_token)
            have_access = True
            secrets.append(access_token)
            reviews = await self._google.reviews(access_token, target.account, target.location)
            details = await self._google.details(access_token, target.location)
            changed = await self._google.google_updated_fields(access_token, target.location)
            attributes = await _optional(
                "attributes", self._google.attribute_count(access_token, target.location)
            )
            media = await _optional(
                "media", self._google.media(access_token, target.account, target.location)
            )
        except ReconnectRequired as exc:
            await self._store.save_status(
                google_sub, location, STATUS_NEEDS_RECONNECT, self._clock()
            )
            if isinstance(exc, GrantRevoked):
                # invalid_grant: the owner removed the access at Google. The token goes,
                # and the countdown to delete what came from Google may start.
                await record_revoked_grant(
                    self._store, google_sub, connection.connection_id, self._clock, job.id
                )
            raise PermanentJobError("el dueño tiene que volver a conectar con Google") from None
        except GoogleRejected as exc:
            if have_access and exc.status in ACCESS_LOST_STATUSES:
                # Google says this user may no longer read the profile. Twice in a row,
                # and what is stored of it stops being served (B5 of
                # docs/seguridad/revision-2026-11-12.md).
                lost = await self._store.record_denied(
                    google_sub, location, self._clock(), ACCESS_LOST_AFTER
                )
                if lost:
                    log.event("profile_access_lost", logging.WARNING, job_id=job.id)
            else:
                await self._store.save_status(google_sub, location, STATUS_ERROR, self._clock())
            raise PermanentJobError(safe_error(str(exc), secrets)) from None
        except Exception as exc:
            # Temporary (5xx, network) or unexpected: the queue retries it.
            # `from None` drops the original exception, which may quote a token.
            raise JobError(safe_error(f"{type(exc).__name__}: {exc}", secrets)) from None

        now = self._clock()
        # A reply Fichaitor has just sent may not be in Google's list yet: it is not
        # allowed to turn the review back into "unanswered" for the next day.
        reviews = with_sent_replies(
            reviews, await self._store.recent_replies(google_sub, location, now - REPLY_GRACE)
        )
        facts = collect_facts(details, media, attributes, now.date())
        summary = summarize(reviews, details.hours, changed, now.date(), facts)
        await self._store.save_summary(google_sub, location, summary, now)
        new_reviews = await self._store.save_reviews(google_sub, location, reviews.reviews, now)
        await self._drop_replaced(job, google_sub, connection.connection_id)
        if self._proposals is not None:
            # Never raises: a review that is not proposed stays in the panel.
            await self._proposals.after_sync(new_reviews or (), now)
        if self._sentiment is not None:
            # The sentiment analysis is a job of its own, enqueued right here when one
            # is due: the first one is ready seconds after the first sync. Never raises.
            await self._sentiment.after_sync(google_sub, location, now)

    async def _drop_replaced(self, job: Job, google_sub: str, connection_id: str) -> None:
        """The connection just worked: the ones it replaced, each with a refresh token
        still valid at Google, are deleted (B4 of docs/seguridad/revision-2026-11-12.md).
        Not revoked: Google revokes the whole grant, and that would cut this one too.
        Never fails the sync: the next good one tries again."""
        try:
            dropped = await self._store.drop_replaced_connections(google_sub, connection_id)
        except Exception as exc:
            log.event("replaced_connections_kept", logging.WARNING, error=type(exc).__name__)
            return
        if dropped:
            log.event("replaced_connections_deleted", job_id=job.id, deleted=dropped)

    async def on_exhausted(self, job: Job) -> None:
        """Out of attempts: say so in the summary, keeping the last good figures."""
        try:
            google_sub, location = _target(job)
        except PermanentJobError:
            return
        await self._store.save_status(google_sub, location, STATUS_ERROR, self._clock())

    async def on_lost(self, kind: str) -> int:
        """Jobs closed as failed because their worker died during the last attempt:
        nobody ran on_exhausted for them, so their summaries are set to error here."""
        return await self._store.mark_lost_syncs(kind)


def _target(job: Job) -> tuple[str, str]:
    """The Google user and the profile of the job, checked before anything is read or
    written: a malformed one (only a row inserted outside the API's routes can be) fails
    at once, without a summary row and without retries."""
    google_sub = job.payload.get("google_sub")
    location = job.payload.get("location")
    if not (isinstance(google_sub, str) and isinstance(location, str)):
        raise PermanentJobError("payload no válido: faltan google_sub o location")
    if not (GOOGLE_SUB.fullmatch(google_sub) and GBP_LOCATION.fullmatch(location)):
        raise PermanentJobError("payload no válido: google_sub o location mal formados")
    return google_sub, location
