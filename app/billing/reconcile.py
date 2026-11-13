"""Job billing_reconcile, once a day: the safety net of the webhooks and of the invoices
(docs/admin-facturacion.md, 4.2 and 4.4).

Stripe and Fichaitor:

1. Every invoice Stripe charged in the last RECONCILE_DAYS has its seat and its charge
   here: one we never heard of (a webhook lost, or an endpoint Stripe switched off after
   days of failures) is applied now, as `invoice.paid` would have been.
2. Every seat still alive is read again in Stripe and copied (a cancellation, a failed
   renewal or a 3DS left undone that did not arrive).
3. Every paid charge has its legal invoice: `issue_invoice` is enqueued again (a job
   keyed by the invoice: never twice at once).
4. Every refund of Stripe has its credit note; one nobody asked for here (made by hand in
   Stripe's dashboard) is logged for review. A refund request still pending is enqueued
   again.

Fichaitor and Odoo:

5. Each document issued in those days is in Odoo with the same number and total; if
   not, it is flagged (`review_reason`) for the backoffice («Cobro > Revisión»). A
   document of Odoo with a Stripe ref that is not ours is logged.
6. An invoice still pending after an hour is logged as `invoice_overdue` (4.5): the
   legal term ends on the 16th of the next month.

Nothing here sends an email nor deletes anything.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.billing.events import StripeEvents
from app.domain.billing import BillingEvent
from app.domain.jobs import Job, JobError
from app.ports.billing_store import (
    CREDIT_NOTE,
    CREDIT_NOTE_JOB,
    DOC_ISSUED,
    DOC_PENDING,
    DOCUMENT_MAX_ATTEMPTS,
    INVOICE,
    ISSUE_INVOICE_JOB,
    REFUND_JOB,
    BillingJobsStore,
)
from app.ports.invoicing import Invoicing, InvoicingError
from app.ports.payments import Payments, PaymentsNotFound, PaymentsRejected, PaymentsUnavailable

log = logging.getLogger("fichaitor.billing")

KIND = "billing_reconcile"
RECONCILE_DAYS = 35
OVERDUE = timedelta(hours=1)


@dataclass
class ReconcileReport:
    """What one pass found. Counts and codes only: no personal data."""

    charges_recovered: int = 0
    seats_read: int = 0
    seats_missing: int = 0
    invoices_queued: int = 0
    credit_notes_queued: int = 0
    refunds_unrequested: int = 0
    refunds_queued: int = 0
    odoo_flagged: dict[str, int] = field(default_factory=dict)
    odoo_unknown: int = 0
    overdue: int = 0
    odoo_checked: bool = False

    def flag(self, reason: str) -> None:
        self.odoo_flagged[reason] = self.odoo_flagged.get(reason, 0) + 1


class Reconcile:
    def __init__(
        self,
        store: BillingJobsStore,
        payments: Payments,
        invoicing: Invoicing | None,
        events: StripeEvents,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._payments = payments
        self._invoicing = invoicing
        self._events = events
        self._clock = clock
        self.last: ReconcileReport | None = None

    async def __call__(self, job: Job) -> None:
        report = ReconcileReport()
        since = self._clock() - timedelta(days=RECONCILE_DAYS)
        try:
            await self._stripe(since, report)
        except PaymentsUnavailable as exc:
            raise JobError(f"Stripe no disponible ({exc.status or 'red'})") from None
        await self._odoo(since, report)
        self.last = report
        level = logging.WARNING if (report.odoo_flagged or report.overdue) else logging.INFO
        log.log(level, "conciliación diaria: %s", report)

    async def _stripe(self, since: datetime, report: ReconcileReport) -> None:
        for invoice in await self._payments.paid_invoices_since(since):
            if invoice.subscription_id is None or invoice.amount_paid <= 0:
                continue
            if not await self._store.charge_paid_known(invoice.id):
                outcome = await self._events.reconcile(
                    invoice.subscription_id,
                    invoice,
                    BillingEvent("invoice.paid", invoice.billing_reason),
                )
                if outcome == "processed":
                    report.charges_recovered += 1
                    log.warning("cobro de Stripe sin aviso recibido: aplicado ahora")
            document = await self._store.document_of(INVOICE, invoice.id)
            if (document is None or document.state == DOC_PENDING) and await self._store.enqueue(
                ISSUE_INVOICE_JOB,
                {"stripe_invoice_id": invoice.id},
                f"{ISSUE_INVOICE_JOB}:{invoice.id}",
                DOCUMENT_MAX_ATTEMPTS,
            ):
                report.invoices_queued += 1
        for subscription_id in await self._store.live_subscriptions():
            try:
                await self._events.reconcile(subscription_id)
                report.seats_read += 1
            except PaymentsNotFound:
                report.seats_missing += 1
                log.warning("plaza con una suscripción que Stripe no tiene")
            except PaymentsRejected as exc:
                log.warning("plaza no conciliada: %s", exc.code or exc.status)
        for refund in await self._payments.refunds_since(since):
            if refund.status not in (None, "succeeded", "pending"):
                continue
            document = await self._store.document_of(CREDIT_NOTE, refund.id)
            if document is not None and document.state != DOC_PENDING:
                continue
            if await self._store.refund_request_of(refund.id) is None:
                report.refunds_unrequested += 1
                log.warning("reembolso hecho fuera de Fichaitor: necesita su rectificativa a mano")
                continue
            if await self._store.enqueue(
                CREDIT_NOTE_JOB,
                {"stripe_refund_id": refund.id},
                f"{CREDIT_NOTE_JOB}:{refund.id}",
                DOCUMENT_MAX_ATTEMPTS,
            ):
                report.credit_notes_queued += 1
        for request_id in await self._store.pending_refund_requests():
            if await self._store.enqueue(
                REFUND_JOB, {"refund_request_id": request_id}, f"{REFUND_JOB}:{request_id}"
            ):
                report.refunds_queued += 1

    async def _odoo(self, since: datetime, report: ReconcileReport) -> None:
        ours = await self._store.documents_since(since)
        now = self._clock()
        report.overdue = sum(
            1 for d in ours if d.state == DOC_PENDING and now - d.created_at > OVERDUE
        )
        if report.overdue:
            log.warning(
                "invoice_overdue: %s facturas pendientes desde hace más de una hora", report.overdue
            )
        if self._invoicing is None:
            return
        try:
            theirs = await self._invoicing.issued_since(since.date())
        except InvoicingError as exc:
            log.warning("conciliación con Odoo no hecha: %s", exc.code or exc.status)
            return
        report.odoo_checked = True
        by_ref = {(d.kind, d.stripe_id): d for d in theirs}
        known = set()
        for document in ours:
            if document.state != DOC_ISSUED:
                continue
            kind = "invoice" if document.kind == INVOICE else "credit_note"
            stripe_id = (
                document.stripe_invoice_id
                if document.kind == INVOICE
                else document.stripe_refund_id
            )
            known.add((kind, stripe_id))
            odoo = by_ref.get((kind, stripe_id))
            reason = None
            if odoo is None:
                reason = "odoo_missing"
            elif odoo.number != document.number:
                reason = "odoo_number_mismatch"
            elif document.amount_total is not None and odoo.amount_total != document.amount_total:
                reason = "odoo_total_mismatch"
            elif odoo.state != "posted":
                reason = "odoo_not_posted"
            if reason is not None and reason != document.review_reason:
                await self._store.flag_document(document.id, reason)
                report.flag(reason)
            elif reason is None and document.review_reason is not None:
                await self._store.flag_document(document.id, "")
        report.odoo_unknown = sum(1 for key in by_ref if key not in known)
