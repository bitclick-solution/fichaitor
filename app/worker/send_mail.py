"""Job send_mail: the emails of a leave and of the charging. The API and the billing
process enqueue; only the worker sends.

Payloads of a leave:

- {"template": "offboarding_code", "to", "scope", "code", "minutes", "code_id"}
- {"template": "offboarding_scheduled", "to", "scope", "delete_at", "token"}
- {"template": "offboarding_restored", "to", "scope"}

Payloads of the charging (brand/correo/LEEME.md, 5 to 10), every value already
formatted by the billing process:

- {"template": "invoice_issued", "to", "document_id", "numero", "fecha", "plan", "ficha",
  "periodo", "importe"}: the PDF is read from billing_document by `document_id`
- {"template": "credit_note_issued", "to", "document_id", "numero", "numero_original",
  "importe"}
- {"template": "renewal_notice", "to", "plan", "ficha", "fecha_renovacion", "importe"}
- {"template": "renewal_notice_unassigned", "to", "plan", "fecha_renovacion", "importe"}
- {"template": "billing_blocked", "to", "importe", "ficha", "motivo"}
- {"template": "billing_unblocked", "to", "ficha"}
- {"template": "beta_ending", "to", "fecha_fin", "oferta"}

No payload carries a link: each link is made here from the public URL of the
configuration.

The payload is the only place where a code or a restore token exists in clear, and
only until the email is out: every way out of this job empties it, the last failure
included, and `on_lost` empties what a dead worker left behind. Nothing of it is ever
logged or stored in an error: the errors of the mailer are fixed strings.
"""

import logging
from datetime import datetime
from typing import Protocol

from app.domain import billing_mail
from app.domain.jobs import Job, JobError, PermanentJobError
from app.domain.offboarding import (
    MAIL_CODE,
    MAIL_FAILED,
    MAIL_RESTORED,
    MAIL_SCHEDULED,
    MAIL_SENT,
    SCOPES,
)
from app.domain.offboarding_mail import (
    app_url,
    code_email,
    restore_url,
    restored_email,
    scheduled_email,
)
from app.ports.mailer import Email, Mailer, MailerError, MailRejected
from app.worker import log

KIND = "send_mail"


class BillingDocuments(Protocol):
    async def pdf(self, document_id: str) -> bytes | None:
        """The PDF of an issued invoice or credit note; None if there is none."""
        ...


class MailJobs(Protocol):
    async def scrub_job(self, job_id: str) -> None: ...

    async def scrub_finished_jobs(self, kind: str) -> int: ...

    async def set_code_mail(self, code_id: str, state: str) -> None: ...


def _text(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise PermanentJobError("payload no válido")
    return value


def build_email(payload: dict, public_base_url: str, pdf: bytes | None = None) -> Email:
    """The message a payload asks for. PermanentJobError (with no detail) if it is not valid.
    `pdf`: the document of an invoice or credit note, read by the job."""
    if payload.get("template") in billing_mail.TEMPLATES:
        return build_billing_email(payload, public_base_url, pdf)
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
        # The link is made from the configuration and the token alone: nothing else of
        # the payload, and nothing of the request that enqueued it.
        url = restore_url(public_base_url, _text(payload, "token"))
        return scheduled_email(to, scope, delete_at, url)
    if template == MAIL_RESTORED:
        # Like the restore link: from the configuration alone.
        return restored_email(to, scope, app_url(public_base_url))
    raise PermanentJobError("payload no válido")


def build_billing_email(payload: dict, base: str, pdf: bytes | None = None) -> Email:
    """A charging email. The link comes from the configuration (`base`) alone."""
    template, to = _text(payload, "template"), _text(payload, "to")
    t = billing_mail
    if template == t.INVOICE_ISSUED:
        if pdf is None:
            raise PermanentJobError("factura sin PDF")
        return t.invoice_issued_email(
            to,
            numero=_text(payload, "numero"),
            fecha=_text(payload, "fecha"),
            plan=_text(payload, "plan"),
            ficha=_text(payload, "ficha"),
            periodo=_text(payload, "periodo"),
            importe=_text(payload, "importe"),
            enlace=t.billing_url(base),
            pdf=pdf,
        )
    if template == t.CREDIT_NOTE_ISSUED:
        if pdf is None:
            raise PermanentJobError("rectificativa sin PDF")
        return t.credit_note_email(
            to,
            numero=_text(payload, "numero"),
            numero_original=_text(payload, "numero_original"),
            importe=_text(payload, "importe"),
            pdf=pdf,
        )
    if template == t.RENEWAL_NOTICE:
        return t.renewal_notice_email(
            to,
            plan=_text(payload, "plan"),
            ficha=_text(payload, "ficha"),
            fecha_renovacion=_text(payload, "fecha_renovacion"),
            importe=_text(payload, "importe"),
            enlace=t.billing_url(base),
        )
    if template == t.RENEWAL_NOTICE_UNASSIGNED:
        return t.renewal_unassigned_email(
            to,
            plan=_text(payload, "plan"),
            fecha_renovacion=_text(payload, "fecha_renovacion"),
            importe=_text(payload, "importe"),
            enlace=t.billing_url(base),
        )
    if template == t.BILLING_BLOCKED:
        return t.billing_blocked_email(
            to,
            importe=_text(payload, "importe"),
            ficha=_text(payload, "ficha"),
            motivo=_text(payload, "motivo"),
            enlace=t.pay_url(base),
        )
    if template == t.BILLING_UNBLOCKED:
        return t.billing_unblocked_email(to, ficha=_text(payload, "ficha"), enlace=app_url(base))
    if template == t.BETA_ENDING:
        offer = payload.get("oferta", "")
        if not isinstance(offer, str):
            raise PermanentJobError("payload no válido")
        return t.beta_ending_email(
            to, fecha_fin=_text(payload, "fecha_fin"), oferta=offer, enlace=t.billing_url(base)
        )
    raise PermanentJobError("payload no válido")


_WITH_PDF = (billing_mail.INVOICE_ISSUED, billing_mail.CREDIT_NOTE_ISSUED)


class SendMail:
    def __init__(
        self,
        store: MailJobs | None,
        mailer: Mailer | None,
        public_base_url: str,
        documents: BillingDocuments | None = None,
    ) -> None:
        self._store = store  # None: nothing to tidy up (the demo without a database)
        self._mailer = mailer  # None: no mail configured; every job fails at once
        self._public_base_url = public_base_url
        self._documents = documents  # None: no invoice can be sent

    async def _pdf(self, payload: dict) -> bytes | None:
        if payload.get("template") not in _WITH_PDF:
            return None
        document_id = payload.get("document_id")
        if self._documents is None or not isinstance(document_id, str):
            raise PermanentJobError("payload no válido")
        return await self._documents.pdf(document_id)

    async def __call__(self, job: Job) -> None:
        template = job.payload.get("template")
        try:
            if self._mailer is None:
                raise PermanentJobError("no hay correo configurado")
            email = build_email(job.payload, self._public_base_url, await self._pdf(job.payload))
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


_KNOWN = (MAIL_CODE, MAIL_SCHEDULED, MAIL_RESTORED, *billing_mail.TEMPLATES)
