"""Job reply_review: the executor. Sends to Google the reply to a review that the owner
confirmed. The only code in the product that writes to Google.

Payload: {"action_id": "act_..."}. Nothing else is read from it. What is sent comes
from the action the API stored when the owner confirmed: the review it targets and the
exact text. The account and the profile that go into Google's URL come from the stored
connection, as in sync_profile.

Rules:

- No action, no write. The job can only carry out an action that exists, is still
  pending and has not been taken by another job. Running the job twice, or two jobs for
  the same action, sends one reply.
- No incoherent action either. Its account must have its Google user and its profile,
  and its review must be of that user and profile; otherwise it fails without calling
  Google (the API role can insert any row: docs/02-hld.md, residual risk).
- The text is sent exactly as confirmed. Nothing here edits it.
- A retry first reads the review back: if Google already has that very text (the
  earlier attempt arrived and only its answer was lost), nothing is sent again.
- 401 or a lost grant: the action fails and the owner is asked to reconnect. A 4xx is
  never retried; a 5xx, a 429 or a network failure is.
- Every outcome is closed together with its audit row. Neither the text nor the token
  appear in a log or in an error message.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from app.adapters.crypto.sealed import unseal
from app.domain.actions import (
    ACTION_ID,
    ERROR_INTERNAL,
    ERROR_RECONNECT,
    ERROR_REJECTED,
    ERROR_REVIEW_GONE,
    ERROR_SUSPENDED,
    ERROR_UNAVAILABLE,
    REASON_ALREADY_APPLIED,
    REPLY_REVIEW,
)
from app.domain.channel_jobs import CHANNEL_ACTION_RESULT, RESULT_MAX_ATTEMPTS, result_key
from app.domain.jobs import Job, JobError, PermanentJobError
from app.ports.google_profile import GoogleProfile, GoogleRejected, ReconnectRequired
from app.ports.job_queue import JobEnqueuer
from app.ports.worker_store import ClaimedAction, WorkerStore
from app.worker import log

KIND = REPLY_REVIEW


class ReplyReview:
    def __init__(
        self,
        store: WorkerStore,
        google: GoogleProfile,
        private_key: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        results: JobEnqueuer | None = None,
    ) -> None:
        self._store = store
        self._google = google
        self._private_key = private_key
        self._clock = clock
        # Where the outcome is told to the conversation (channel_action_result), so that
        # an owner who confirmed by the channel hears how it went. None: nobody is told.
        self._results = results

    async def __call__(self, job: Job) -> None:
        action_id = _action_id(job)
        action = await self._store.claim_action(action_id, job.id, self._clock())
        if action is None:
            # Finished already, taken by another job, or never created: nothing to send.
            log.event("action_skipped", job_id=job.id, action_id=action_id)
            return

        if not action.coherent:
            # The account, the Google user, the profile and the review do not agree:
            # only a row written outside the API's own route looks like this. Nothing
            # is sent, not even with the token of the Google user it names.
            await self._fail(action, job, ERROR_INTERNAL)
            log.event("action_incoherent", logging.WARNING, job_id=job.id, action_id=action.id)
            raise PermanentJobError("la acción no casa con su cuenta, su ficha o su reseña")

        if await self._store.is_suspended(action.google_sub):
            # A scheduled leave: nothing is sent to Google for that account.
            await self._fail(action, job, ERROR_SUSPENDED)
            log.event("account_suspended", job_id=job.id, kind=job.kind)
            return

        connection = await self._store.latest_connection(action.google_sub)
        if connection is None:
            await self._fail(action, job, ERROR_RECONNECT)
            raise PermanentJobError("no hay conexión con Google para ese usuario")
        # The account and location sent to Google come from the stored connection.
        target = next((p for p in connection.locations if p.location == action.location), None)
        if target is None:
            await self._fail(action, job, ERROR_INTERNAL)
            raise PermanentJobError("la ficha no pertenece a la conexión actual")
        try:
            refresh_token = unseal(self._private_key, connection.refresh_token_enc)
        except Exception:
            await self._fail(action, job, ERROR_INTERNAL)
            raise PermanentJobError(
                f"no se pudo descifrar el token (versión de clave {connection.key_version})"
            ) from None

        already_there = False
        reply_at = None
        have_access = False
        try:
            access_token = await self._google.refresh_access_token(refresh_token)
            have_access = True
            if action.attempts > 1:
                # An earlier attempt may have reached Google: look before writing again.
                current = await self._google.current_reply(
                    access_token, target.account, target.location, action.gbp_review_id
                )
                already_there = current == action.text
            if not already_there:
                reply_at = await self._google.reply_to_review(
                    access_token,
                    target.account,
                    target.location,
                    action.gbp_review_id,
                    action.text,
                )
        except ReconnectRequired:
            await self._fail(action, job, ERROR_RECONNECT, 401)
            raise PermanentJobError("el dueño tiene que volver a conectar con Google") from None
        except GoogleRejected as exc:
            if not have_access:
                # Google refused our own credentials: nothing the owner can fix.
                reason = ERROR_INTERNAL
            else:
                reason = ERROR_REVIEW_GONE if exc.status == 404 else ERROR_REJECTED
            await self._fail(action, job, reason, exc.status)
            # Only the status: Google's answer may quote the reply.
            raise PermanentJobError(f"Google rechazó la respuesta (estado {exc.status})") from None
        except Exception as exc:
            # Temporary (5xx, 429, network) or unexpected: the queue retries it. Only the
            # type and the status are kept: the message may quote the reply or a token.
            status = getattr(exc, "status", None)
            detail = f" (estado {status})" if isinstance(status, int) else ""
            raise JobError(
                f"fallo temporal al enviar la respuesta: {type(exc).__name__}{detail}"
            ) from None

        held = await self._store.finish_action(
            action.id,
            job.id,
            done=True,
            at=self._clock(),
            google_status=200,
            reason=REASON_ALREADY_APPLIED if already_there else None,
            reply_at=reply_at,
        )
        log.event(
            "reply_sent" if held else "action_lost",
            job_id=job.id,
            action_id=action.id,
            already_there=already_there,
        )
        if held:
            await self._tell(action.id, None)

    async def _fail(
        self, action: ClaimedAction, job: Job, reason: str, status: int | None = None
    ) -> None:
        held = await self._store.finish_action(
            action.id, job.id, done=False, at=self._clock(), google_status=status, reason=reason
        )
        if held:
            await self._tell(action.id, reason)

    async def _tell(self, action_id: str, error: str | None) -> None:
        """Enqueue the outcome for the conversation. Never raises: the action is
        already closed, and the panel shows it either way."""
        if self._results is None:
            return
        try:
            await self._results.enqueue(
                CHANNEL_ACTION_RESULT,
                {"action_id": action_id, "ok": error is None, "error": error},
                idempotency_key=result_key(action_id),
                max_attempts=RESULT_MAX_ATTEMPTS,
            )
        except Exception as exc:
            log.event("action_result_not_enqueued", logging.WARNING, error=type(exc).__name__)

    async def on_exhausted(self, job: Job) -> None:
        """Out of attempts: the action fails, so that the owner is told and can try again."""
        try:
            action_id = _action_id(job)
        except PermanentJobError:
            return
        held = await self._store.finish_action(
            action_id, job.id, done=False, at=self._clock(), reason=ERROR_UNAVAILABLE
        )
        if held:
            await self._tell(action_id, ERROR_UNAVAILABLE)

    async def on_lost(self, kind: str) -> int:
        """Jobs closed as failed without closing their action (their worker died during
        the last attempt): the action must not stay "en curso" for ever."""
        return await self._store.mark_lost_actions(kind)


def _action_id(job: Job) -> str:
    action_id = job.payload.get("action_id")
    if not isinstance(action_id, str) or not ACTION_ID.fullmatch(action_id):
        raise PermanentJobError("payload no válido: falta action_id")
    return action_id
