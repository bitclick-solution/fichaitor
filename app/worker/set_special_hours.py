"""Job set_special_hours: the executor of a key date. Sends to Google the hours of one
day that the owner confirmed, by the channel or in the panel. The second write of the
product, after the reply to a review (app/worker/reply_review.py), and with its rules.

Payload: {"action_id": "act_..."}. Nothing else is read from it. The day and the hours
come from the action, written when the owner confirmed; the account and the profile that
go into Google's URL come from the stored connection.

Rules:

- No action, no write; no incoherent action either (M1 of
  docs/seguridad/revision-2026-11-12.md): the account of the action must have its Google
  user and its profile, not retired, and the action must name its day. Otherwise it
  fails without calling Google.
- No action on a profile that is no longer active (multinegocio): one the owner
  deactivated after confirming fails as `location_inactive` without calling Google.
- The change is read back from the action's text, strictly; a text that is not exactly
  the canonical form, or whose day is not the action's, fails as `internal`.
- Too late is too late: once the day has passed (in the local time of the peninsula),
  the action fails as `date_passed` without calling Google.
- Read, merge, write: the special hours Google has now are read first, the periods of
  that day are replaced and every other period is sent back as it was. Nothing of
  another date is ever dropped. If Google already has exactly those hours for that day
  (a retry whose earlier attempt arrived), nothing is written.
- "Horario normal" copies the regular hours of that weekday from Google. Without regular
  hours at all, the action fails as `no_regular_hours`.
- 401 or a lost grant: `reconnect`. A 4xx is never retried; a 5xx, a 429 or a network
  failure is, with the waits of the queue.
- Every outcome is closed together with its audit row, and told to the conversation.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, tzinfo

from app.adapters.crypto.sealed import unseal
from app.domain.actions import (
    ACTION_ID,
    ERROR_DATE_PASSED,
    ERROR_INTERNAL,
    ERROR_LOCATION_INACTIVE,
    ERROR_NO_REGULAR_HOURS,
    ERROR_RECONNECT,
    ERROR_REJECTED,
    ERROR_SUSPENDED,
    ERROR_UNAVAILABLE,
    REASON_ALREADY_APPLIED,
    SET_SPECIAL_HOURS,
)
from app.domain.channel_jobs import CHANNEL_ACTION_RESULT, RESULT_MAX_ATTEMPTS, result_key
from app.domain.conversation import DEFAULT_TIMEZONE, timezone_for
from app.domain.jobs import Job, JobError, PermanentJobError
from app.domain.special_hours import (
    JOB_MAX_ATTEMPTS,
    InvalidSpecialHours,
    NoRegularHours,
    already_there,
    from_text,
    google_periods,
    merge,
)
from app.ports.google_profile import (
    GoogleProfile,
    GoogleRejected,
    GrantRevoked,
    ReconnectRequired,
)
from app.ports.job_queue import JobEnqueuer
from app.ports.worker_store import ClaimedAction, WorkerStore
from app.worker import log
from app.worker.google_grant import record_revoked_grant

KIND = SET_SPECIAL_HOURS
MAX_ATTEMPTS = JOB_MAX_ATTEMPTS  # enqueued with it by the store that creates the action


class SetSpecialHours:
    def __init__(
        self,
        store: WorkerStore,
        google: GoogleProfile,
        private_key: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        results: JobEnqueuer | None = None,
        zone: tzinfo | None = None,
    ) -> None:
        self._store = store
        self._google = google
        self._private_key = private_key
        self._clock = clock
        self._results = results
        # The zone of the accounts does not exist yet: the peninsula for every one.
        self._zone = zone or timezone_for(DEFAULT_TIMEZONE)

    async def __call__(self, job: Job) -> None:
        action_id = _action_id(job)
        action = await self._store.claim_action(action_id, job.id, self._clock())
        if action is None:
            log.event("action_skipped", job_id=job.id, action_id=action_id)
            return

        if action.type != SET_SPECIAL_HOURS or not action.coherent:
            await self._fail(action, job, ERROR_INTERNAL)
            log.event("action_incoherent", logging.WARNING, job_id=job.id, action_id=action.id)
            raise PermanentJobError("la acción no casa con su cuenta, su ficha o su día")
        try:
            change = from_text(action.text)
        except InvalidSpecialHours:
            change = None
        if change is None or change.day != action.special_date:
            await self._fail(action, job, ERROR_INTERNAL)
            log.event("action_incoherent", logging.WARNING, job_id=job.id, action_id=action.id)
            raise PermanentJobError("el horario guardado no se puede leer")

        if not action.active:
            # The owner deactivated that profile after confirming: nothing is sent.
            await self._fail(action, job, ERROR_LOCATION_INACTIVE)
            log.event("action_location_inactive", job_id=job.id, action_id=action.id)
            return

        if await self._store.is_suspended(action.google_sub):
            await self._fail(action, job, ERROR_SUSPENDED)
            log.event("account_suspended", job_id=job.id, kind=job.kind)
            return

        if self._clock().astimezone(self._zone).date() > change.day:
            # The day has gone: a change for it now would only add noise to the profile.
            await self._fail(action, job, ERROR_DATE_PASSED)
            raise PermanentJobError("el día del horario ya ha pasado")

        connection = await self._store.latest_connection(action.google_sub)
        if connection is None:
            await self._fail(action, job, ERROR_RECONNECT)
            raise PermanentJobError("no hay conexión con Google para ese usuario")
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

        have_access = False
        applied_before = False
        try:
            access_token = await self._google.refresh_access_token(refresh_token)
            have_access = True
            hours = await self._google.hours(access_token, target.location)
            wanted = google_periods(change, hours.regular)
            applied_before = already_there(hours.special, wanted)
            if not applied_before:
                await self._google.set_special_hours(
                    access_token, target.location, merge(hours.special, change.day, wanted)
                )
        except NoRegularHours:
            await self._fail(action, job, ERROR_NO_REGULAR_HOURS)
            raise PermanentJobError("la ficha no tiene horario normal que copiar") from None
        except ReconnectRequired as exc:
            await self._fail(action, job, ERROR_RECONNECT, 401)
            if isinstance(exc, GrantRevoked):
                # invalid_grant: the owner removed the access at Google (google_grant.py).
                await record_revoked_grant(
                    self._store, action.google_sub, connection.connection_id, self._clock, job.id
                )
            raise PermanentJobError("el dueño tiene que volver a conectar con Google") from None
        except GoogleRejected as exc:
            reason = ERROR_REJECTED if have_access else ERROR_INTERNAL
            await self._fail(action, job, reason, exc.status)
            raise PermanentJobError(f"Google rechazó el horario (estado {exc.status})") from None
        except Exception as exc:
            # Temporary (5xx, 429, network) or unexpected: the queue retries it. Only the
            # type and the status are kept: the message may quote a token.
            status = getattr(exc, "status", None)
            detail = f" (estado {status})" if isinstance(status, int) else ""
            raise JobError(
                f"fallo temporal al enviar el horario: {type(exc).__name__}{detail}"
            ) from None

        held = await self._store.finish_action(
            action.id,
            job.id,
            done=True,
            at=self._clock(),
            google_status=200,
            reason=REASON_ALREADY_APPLIED if applied_before else None,
        )
        log.event(
            "special_hours_sent" if held else "action_lost",
            job_id=job.id,
            action_id=action.id,
            already_there=applied_before,
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
        """Enqueue the outcome for the conversation, which tells the chat and closes the
        proposal (also one confirmed in the panel). Never raises."""
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
        """Out of attempts: the action fails as `unavailable`, and the owner is told."""
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
        return await self._store.mark_lost_actions(kind)


def _action_id(job: Job) -> str:
    action_id = job.payload.get("action_id")
    if not isinstance(action_id, str) or not ACTION_ID.fullmatch(action_id):
        raise PermanentJobError("payload no válido: falta action_id")
    return action_id
