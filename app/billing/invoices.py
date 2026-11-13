"""Jobs issue_invoice and issue_credit_note: the legal invoice in Bitclick's Odoo for what
Stripe charged, and the credit note of an exceptional refund (docs/admin-facturacion.md,
2.2, 2.5, 4.3 and 4.5; decision 9.0.1: Odoo through its JSON-2 API, no module).

issue_invoice, enqueued by `apply_seat` when a charge is paid (key
`issue_invoice:<in_…>`) and again by the daily reconciliation if one is missing:

1. The document is noted `pendiente` before Odoo is called (one per Stripe invoice).
2. The lines come from `billing.invoice_lines` (plan, profile, year paid, treatment)
   and must add up to what Stripe charged, to the cent.
3. Odoo creates, posts (numbers it; VERI*FACTU records it) and pays it in the «Stripe»
   journal. A call cut at any step is resumed by the adapter, which finds the invoice
   by its `ref`: never two numbers for one charge.
4. With its PDF, the document is `emitida` and its email (with the PDF attached) is
   enqueued in the same transaction.

A temporary error (network, time out, 5xx, the AEAT down) is retried for about three
days, with a wait that grows up to two hours: the customer notices nothing, the service
depends on the payment, not on the invoice. A permanent error (a 4xx, the lines do not
add up), a total that differs from Stripe's or a VERI*FACTU record refused leave the
document in `revision`, with no email to the customer. A posted invoice is never
deleted: an error is corrected with a credit note, by hand.

issue_credit_note does the same for a refund (key `issue_credit_note:<re_…>`), once
its invoice is issued, with its amount read again in Stripe.
"""

import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime

from app.domain import billing_mail
from app.domain.billing import InvoiceDraft, invoice_lines, price_plan
from app.domain.jobs import Job, JobError, PermanentJobError, backoff_seconds
from app.domain.plans import Plan
from app.ports.billing_store import (
    CREDIT_NOTE_JOB,
    DOC_ISSUED,
    DOC_PENDING,
    INVOICE,
    ISSUE_INVOICE_JOB,
    BillingJobsStore,
    ChargeContext,
)
from app.ports.invoicing import (
    CreditNoteRequest,
    InvoiceCustomer,
    InvoiceRequest,
    Invoicing,
    InvoicingRejected,
    InvoicingUnavailable,
    IssuedDocument,
)
from app.ports.payments import Payments, PaymentsRejected, PaymentsUnavailable

log = logging.getLogger("fichaitor.billing")

RETRY_CAP_SECONDS = 2 * 3600
# VERI*FACTU states that mean the AEAT did not take the record as it is (the names differ
# between the Odoo module and the OCA one): the invoice goes to review.
VERIFACTU_REFUSED = {
    "rejected",
    "registered_with_errors",
    "accepted_with_errors",
    "error",
    "incorrecto",
    "aceptado_con_errores",
    "rechazado",
}
_INVOICE_ID = re.compile(r"^in_[A-Za-z0-9]{1,250}$")
_REFUND_ID = re.compile(r"^re_[A-Za-z0-9]{1,250}$")


def retry_in(attempts: int) -> int:
    """30 s, 1 min, 2 min… up to two hours: 40 attempts last about three days."""
    return backoff_seconds(attempts, cap=RETRY_CAP_SECONDS)


def _code(text: str | None, default: str) -> str:
    clean = re.sub(r"[^a-z0-9_]+", "_", (text or "").lower()).strip("_")
    return (clean or default)[:40]


def _refused(document: IssuedDocument) -> bool:
    state = (document.verifactu_state or "").lower()
    return state in VERIFACTU_REFUSED


class _Documents:
    def __init__(
        self,
        store: BillingJobsStore,
        invoicing: Invoicing | None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._invoicing = invoicing
        self._clock = clock

    def _odoo(self, job: Job) -> Invoicing:
        if self._invoicing is None:
            # Without Odoo configured nothing is issued, and nothing is lost: the
            # document stays pending and the daily reconciliation enqueues it again.
            raise JobError("Odoo sin configurar", retry_in=retry_in(job.attempts))
        return self._invoicing

    async def _pdf(self, job: Job, document_id: str, issued: IssuedDocument) -> bytes | None:
        try:
            return await self._odoo(job).pdf(issued.move_id)
        except InvoicingUnavailable as exc:
            raise JobError(
                f"PDF de Odoo no disponible ({exc.code or exc.status or 'red'})",
                retry_in=retry_in(job.attempts),
            ) from None
        except InvoicingRejected as exc:
            await self._store.review_document(
                document_id,
                _code(exc.code, "pdf_rejected"),
                issued.number,
                issued.move_id,
                issued.amount_total,
            )
            return None

    async def _checked(self, document_id: str, issued: IssuedDocument) -> bool:
        """A document Odoo posted is only good if its total is Stripe's and VERI*FACTU
        took it. If not, review, without email."""
        reason = None
        if not issued.total_ok:
            reason = "total_mismatch"
        elif _refused(issued):
            reason = "verifactu_refused"
        elif issued.number is None or issued.state != "posted":
            reason = "not_posted"
        if reason is None:
            return True
        log.error("documento de Odoo %s a revisión: %s", issued.move_id, reason)
        await self._store.review_document(
            document_id, reason, issued.number, issued.move_id, issued.amount_total
        )
        return False


class IssueInvoice(_Documents):
    KIND = ISSUE_INVOICE_JOB

    async def __call__(self, job: Job) -> None:
        invoice_id = job.payload.get("stripe_invoice_id")
        if not isinstance(invoice_id, str) or not _INVOICE_ID.match(invoice_id):
            raise PermanentJobError("payload no válido: falta stripe_invoice_id")
        charge = await self._store.charge_context(invoice_id)
        if charge is None:
            raise PermanentJobError("cobro desconocido")
        if charge.status != "paid" or charge.amount_paid <= 0:
            log.warning("factura pedida para un cobro sin pagar: no se emite")
            return
        document = await self._store.open_document(INVOICE, charge.account_id, invoice_id, None)
        if document.state != DOC_PENDING:
            return  # issued already, or waiting in review
        built = self._request(charge)
        if built is None:
            await self._store.review_document(document.id, "missing_data")
            return
        request, (start, end), plan = built
        try:
            issued = await self._odoo(job).issue_invoice(request)
        except InvoicingUnavailable as exc:
            raise JobError(
                f"Odoo no disponible ({exc.code or exc.status or 'red'})",
                retry_in=retry_in(job.attempts),
            ) from None
        except InvoicingRejected as exc:
            log.error("Odoo rechaza la factura de un cobro: %s", exc.code or exc.status)
            await self._store.review_document(document.id, _code(exc.code, "odoo_rejected"))
            return
        if not await self._checked(document.id, issued):
            return
        pdf = await self._pdf(job, document.id, issued)
        if pdf is None:
            return
        mail = billing_mail.invoice_mail(
            request.customer.email or "",
            number=issued.number or "",
            issued_on=request.invoice_date,
            plan=plan,
            title=charge.location_title,
            period_start=start,
            period_end=end,
            total=issued.amount_total,
        )
        done = await self._store.issue_document(
            document.id,
            issued.number,
            issued.move_id,
            (issued.amount_untaxed, issued.amount_tax, issued.amount_total),
            issued.verifactu_state,
            pdf,
            mail,
        )
        if done:
            log.info("factura emitida en Odoo (move %s)", issued.move_id)

    def _request(
        self, charge: ChargeContext
    ) -> tuple[InvoiceRequest, tuple[date, date], Plan] | None:
        """What Odoo is asked, the year paid (in Spain) and the plan; None if the charge
        lacks what a full invoice needs."""
        customer = charge.customer
        if (
            customer is None
            or customer.stripe_customer_id is None
            or charge.price_key is None
            or charge.period_start is None
            or charge.period_end is None
        ):
            return None
        plan = price_plan(charge.price_key)
        profile = customer.profile
        start = billing_mail.madrid_date(charge.period_start)
        end = billing_mail.madrid_date(charge.period_end)
        try:
            draft: InvoiceDraft = invoice_lines(
                charge.price_key,
                charge.location_title,
                start,
                end,
                profile.tax_treatment,
                profile.territory,
            )
        except ValueError:
            return None
        request = InvoiceRequest(
            stripe_invoice_id=charge.stripe_invoice_id,
            customer=InvoiceCustomer(
                stripe_customer_id=customer.stripe_customer_id,
                legal_name=profile.legal_name,
                tax_id=profile.tax_id,
                country=profile.country,
                postal_code=profile.postal_code,
                city=profile.city,
                line1=profile.address_line1,
                line2=profile.address_line2,
                email=profile.invoice_email,
            ),
            draft=draft,
            treatment=profile.tax_treatment,
            invoice_date=billing_mail.madrid_date(self._clock()),
            operation_date=billing_mail.madrid_date(charge.paid_at or self._clock()),
            paid_cents=charge.amount_paid,
            payment_reference=charge.payment_reference or charge.stripe_invoice_id,
        )
        return request, (start, end), plan

    async def on_exhausted(self, job: Job) -> None:
        # The document stays pending: the backoffice shows it in «Salud» and the runbook
        # of docs/despliegue.md («Facturas pendientes») says how to issue it by hand
        # with the same ref, so that the next try finds it.
        log.error("invoice_overdue: reintentos agotados")


class IssueCreditNote(_Documents):
    KIND = CREDIT_NOTE_JOB

    def __init__(
        self,
        store: BillingJobsStore,
        invoicing: Invoicing | None,
        payments: Payments,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(store, invoicing, clock)
        self._payments = payments

    async def __call__(self, job: Job) -> None:
        refund_id = job.payload.get("stripe_refund_id")
        if not isinstance(refund_id, str) or not _REFUND_ID.match(refund_id):
            raise PermanentJobError("payload no válido: falta stripe_refund_id")
        context = await self._store.credit_note_context(refund_id)
        if context is None:
            raise PermanentJobError("reembolso sin rectificativa pendiente")
        document = context.document
        if document.state != DOC_PENDING:
            return
        original = context.original
        if original is None or original.state != DOC_ISSUED or not original.number:
            # The invoice it corrects is not issued yet: wait for it.
            raise JobError(
                "la factura original aún no está emitida", retry_in=retry_in(job.attempts)
            )
        try:
            refund = await self._payments.refund_info(refund_id)
        except PaymentsUnavailable:
            raise JobError("Stripe no disponible", retry_in=retry_in(job.attempts)) from None
        except PaymentsRejected:
            await self._store.review_document(document.id, "refund_unknown")
            return
        reason = context.refund.reason if context.refund else "decision_comercial"
        request = CreditNoteRequest(
            stripe_refund_id=refund_id,
            stripe_invoice_id=original.stripe_invoice_id or "",
            amount_cents=refund.amount,
            reason=reason,
            credit_date=billing_mail.madrid_date(self._clock()),
        )
        try:
            issued = await self._odoo(job).issue_credit_note(request)
        except InvoicingUnavailable as exc:
            raise JobError(
                f"Odoo no disponible ({exc.code or exc.status or 'red'})",
                retry_in=retry_in(job.attempts),
            ) from None
        except InvoicingRejected as exc:
            await self._store.review_document(document.id, _code(exc.code, "odoo_rejected"))
            return
        if not await self._checked(document.id, issued):
            return
        pdf = await self._pdf(job, document.id, issued)
        if pdf is None or context.invoice_email is None:
            return
        mail = billing_mail.credit_note_mail(
            context.invoice_email,
            number=issued.number or "",
            original_number=original.number,
            amount=issued.amount_total,
        )
        await self._store.issue_document(
            document.id,
            issued.number,
            issued.move_id,
            (issued.amount_untaxed, issued.amount_tax, issued.amount_total),
            issued.verifactu_state,
            pdf,
            mail,
        )

    async def on_exhausted(self, job: Job) -> None:
        log.error("credit_note_overdue: reintentos agotados")
