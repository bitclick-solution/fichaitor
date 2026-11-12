"""Job send_mail: the emails of a leave. The API enqueues; only the worker sends.

Payloads:

- {"template": "offboarding_code", "to", "scope", "code", "minutes", "code_id"}
- {"template": "offboarding_scheduled", "to", "scope", "delete_at", "token"}

The payload is the only place where a code or a restore token exists in clear, and
only until the email is out: every way out of this job empties it, the last failure
included, and `on_lost` empties what a dead worker left behind. Nothing of it is ever
logged or stored in an error: the errors of the mailer are fixed strings.
"""

import logging
from datetime import datetime
from typing import Protocol

from app.domain.jobs import Job, JobError, PermanentJobError
from app.domain.offboarding import MAIL_CODE, MAIL_FAILED, MAIL_SCHEDULED, MAIL_SENT, SCOPES
from app.domain.offboarding_mail import code_email, restore_url, scheduled_email
from app.ports.mailer import Email, Mailer, MailerError, MailRejected
from app.worker import log

KIND = "send_mail"


class MailJobs(Protocol):
    async def scrub_job(self, job_id: str) -> None: ...

    async def scrub_finished_jobs(self, kind: str) -> int: ...

    async def set_code_mail(self, code_id: str, state: str) -> None: ...


def _text(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise PermanentJobError("payload no válido")
    return value


def build_email(payload: dict, public_base_url: str) -> Email:
    """The message a payload asks for. PermanentJobError (with no detail) if it is not valid."""
    template, to, scope = _text(payload, "template"), _text(payload, "to"), _text(payload, "scope")
    if scope not in SCOPES:
        raise PermanentJobError("payload no válido")
    if template == MAIL_CODE:
        minutes = payload.get("minutes")
        if not isinstance(minutes, int):
            raise PermanentJobError("payload no válido")
        return code_email(to, scope, _text(payload, "code"), minutes)
    if template == MAIL_SCHEDULED:
        try:
            delete_at = datetime.fromisoformat(_text(payload, "delete_at"))
        except ValueError:
            raise PermanentJobError("payload no válido") from None
        url = restore_url(public_base_url, _text(payload, "token"))
        return scheduled_email(to, scope, delete_at, url)
    raise PermanentJobError("payload no válido")


class SendMail:
    def __init__(self, store: MailJobs | None, mailer: Mailer | None, public_base_url: str) -> None:
        self._store = store  # None: nothing to tidy up (the demo without a database)
        self._mailer = mailer  # None: no mail configured; every job fails at once
        self._public_base_url = public_base_url

    async def __call__(self, job: Job) -> None:
        template = job.payload.get("template")
        try:
            if self._mailer is None:
                raise PermanentJobError("no hay correo configurado")
            email = build_email(job.payload, self._public_base_url)
        except PermanentJobError:
            await self._close(job, MAIL_FAILED)
            raise
        try:
            await self._mailer.send(email)
        except MailRejected as exc:
            await self._close(job, MAIL_FAILED)
            raise PermanentJobError(str(exc)) from None
        except MailerError as exc:
            # Tried again later: the payload stays until the last attempt.
            raise JobError(str(exc)) from None
        except Exception as exc:
            raise JobError(f"fallo inesperado al enviar el correo: {type(exc).__name__}") from None
        await self._close(job, MAIL_SENT)
        log.event("mail_sent", job_id=job.id, template=template if template in _KNOWN else None)

    async def _close(self, job: Job, state: str) -> None:
        if self._store is None:
            return
        code_id = job.payload.get("code_id")
        if isinstance(code_id, str) and code_id:
            await self._store.set_code_mail(code_id, state)
        await self._store.scrub_job(job.id)
        if state == MAIL_FAILED:
            log.event("mail_failed", logging.ERROR, job_id=job.id)

    async def on_exhausted(self, job: Job) -> None:
        """Out of attempts: the owner is told (the code shows as not sent) and what the
        job carried is erased."""
        await self._close(job, MAIL_FAILED)

    async def on_lost(self, kind: str) -> int:
        """Finished jobs that still carry something: their worker died before tidying up."""
        if self._store is None:
            return 0
        return await self._store.scrub_finished_jobs(kind)


_KNOWN = (MAIL_CODE, MAIL_SCHEDULED)
