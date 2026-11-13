"""Job billing_refund: an exceptional refund (P2 of docs/admin-facturacion.md: nothing
charged is given back, except when an administrator decides it).

Only an administrator asks for it: the backoffice writes a `billing_refund_request` (with
its reason, the case and who asked) and enqueues `billing_refund:<id>`; the daily
reconciliation enqueues again any request still pending. Here:

1. Stripe refunds the invoice, whole or the amount asked, with an idempotency key of
   the request: a job cut halfway and run again never refunds twice.
2. If the refund is whole, the seat stops renewing (`cancel_at_period_end`) and the
   right of that charge ends now.
3. In one transaction: the request is `hecho`, the credit note is noted `pendiente` and
   `issue_credit_note` is enqueued (app/billing/invoices.py).

A refund Stripe refuses ends the request as `fallido`, for the backoffice.
"""

import logging
import re

from app.domain.jobs import Job, JobError, PermanentJobError
from app.ports.billing_store import REFUND_JOB, REFUND_PENDING, BillingJobsStore
from app.ports.payments import Payments, PaymentsRejected, PaymentsUnavailable

log = logging.getLogger("fichaitor.billing")

KIND = REFUND_JOB
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class Refunds:
    def __init__(self, store: BillingJobsStore, payments: Payments) -> None:
        self._store = store
        self._payments = payments

    async def __call__(self, job: Job) -> None:
        request_id = job.payload.get("refund_request_id")
        if not isinstance(request_id, str) or not _UUID.match(request_id):
            raise PermanentJobError("payload no válido: falta refund_request_id")
        request = await self._store.refund_request(request_id)
        if request is None or request.state != REFUND_PENDING:
            return
        charge = await self._store.charge_context(request.stripe_invoice_id)
        if charge is None or charge.account_id != request.account_id:
            log.error("reembolso de un cobro que no es de esa cuenta: no se hace")
            await self._store.fail_refund(request.id)
            return
        key = f"{KIND}:{request.id}"
        try:
            refund = await self._payments.refund(
                request.stripe_invoice_id, request.amount, request.reason, idempotency_key=key
            )
            invoice = await self._payments.invoice(request.stripe_invoice_id)
        except PaymentsUnavailable as exc:
            raise JobError(f"Stripe no disponible ({exc.status or 'red'})") from None
        except PaymentsRejected as exc:
            log.error("Stripe rechaza el reembolso: %s", exc.code or exc.status)
            await self._store.fail_refund(request.id)
            return
        whole = invoice.amount_refunded >= invoice.amount_paid
        if whole and charge.subscription_id is not None:
            try:
                sub = await self._payments.set_cancel_at_period_end(
                    charge.subscription_id, True, idempotency_key=f"{key}:cancel"
                )
            except PaymentsUnavailable as exc:
                raise JobError(f"Stripe no disponible ({exc.status or 'red'})") from None
            except PaymentsRejected as exc:
                # Already cancelled, most likely: the refund is done all the same.
                log.warning("la plaza reembolsada no se ha podido cancelar: %s", exc.code)
            else:
                if charge.seat_id is not None:
                    await self._store.set_cancel_at_period_end(
                        charge.seat_id, sub.cancel_at_period_end
                    )
        await self._store.finish_refund(
            request.id, refund.id, request.stripe_invoice_id, invoice.amount_refunded, whole
        )
        log.info("reembolso hecho (%s), rectificativa en cola", "total" if whole else "parcial")

    async def on_exhausted(self, job: Job) -> None:
        request_id = job.payload.get("refund_request_id")
        if isinstance(request_id, str) and _UUID.match(request_id):
            await self._store.fail_refund(request_id)
