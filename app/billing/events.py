"""Job stripe_event: one Stripe event, processed (docs/admin-facturacion.md, 4.2).

The API only noted the ids of a signed event. Here the object is read again in Stripe:
Stripe does not keep the order, repeats events and retries for days, so the payload never
decides anything and a made-up object id ends as `not_found`. Then `reconcile` copies
the subscription of the seat and the invoice to our tables and decides, with the rules of
`app.domain.billing` (`seat_state`, `block_decision`):

- an invoice paid: the entitlement of the seat until its year ends (+2 days of margin),
  the block of that invoice closed, and on a first purchase the profile activated in its
  plan (or the seat left free, for review, if another seat already holds that profile);
- a renewal failed or a 3DS left undone: the whole account blocked at once (P3);
- a dispute: blocked; won, unblocked;
- a subscription cancelled: its rights end with its paid year, or at once if it was
  cancelled for not paying;
- a seat bought in the beta window (a Stripe trial until the window ends): its invoice
  of 0 at the start grants the right until the first charge (+2 days) and activates the
  profile, without a legal invoice (nothing was charged). The first charge, when the
  trial ends, is a `subscription_cycle` invoice: paid, the year and its legal invoice;
  failed or waiting for 3DS, the account blocked as on any renewal.
  `customer.subscription.trial_will_end` (3 days before, by default) only copies the
  seat again: the notice to the customer (`trial_ending`, 7 days before) is sent by the
  daily `billing_renewal_notice`.

Every step is idempotent: processing an event twice, or the events in any order, ends in
the same state. An event of a subscription whose metadata does not match the customer
of the account ends as `orphan`.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from app.domain.billing import (
    BillingEvent,
    BlockAction,
    SeatStatus,
    block_decision,
    seat_state,
)
from app.domain.jobs import Job, JobError, PermanentJobError
from app.ports.billing_store import BillingProcessStore, SeatRow, SeatSync
from app.ports.payments import (
    InvoiceInfo,
    Payments,
    PaymentsNotFound,
    PaymentsRejected,
    PaymentsUnavailable,
    stripe_snapshot,
)

log = logging.getLogger("fichaitor.billing")

KIND = "stripe_event"

PROCESSED = "processed"
IGNORED = "ignored"
NOT_FOUND = "not_found"
ORPHAN = "orphan"
LIVEMODE_MISMATCH = "livemode_mismatch"

_SUBSCRIPTION_EVENTS = {
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "customer.subscription.trial_will_end",
}
_INVOICE_EVENTS = {
    "invoice.paid",
    "invoice.payment_failed",
    "invoice.payment_action_required",
}
_DISPUTE_EVENTS = {"charge.dispute.created", "charge.dispute.closed"}
_FIRST_PURCHASE = "subscription_create"


class StripeEvents:
    def __init__(
        self,
        store: BillingProcessStore,
        payments: Payments,
        *,
        livemode: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._payments = payments
        self._livemode = livemode
        self._clock = clock

    async def __call__(self, job: Job) -> None:
        event_id = job.payload.get("event_id")
        if not isinstance(event_id, str):
            raise PermanentJobError("payload no válido: falta event_id")
        event = await self._store.stripe_event(event_id)
        if event is None or event.processed_at is not None:
            return
        if event.livemode != self._livemode:
            await self._store.finish_event(event.id, LIVEMODE_MISMATCH)
            return
        try:
            outcome = await self._process(event.type, event.object_id)
        except PaymentsNotFound:
            outcome = NOT_FOUND
        except PaymentsUnavailable as exc:
            raise JobError(f"Stripe no disponible ({exc.status or 'red'})") from None
        except PaymentsRejected as exc:
            log.warning("evento de Stripe no procesable: %s", exc.code or exc.status)
            outcome = "rejected"
        log.info("evento de Stripe %s: %s", event.type, outcome)
        await self._store.finish_event(event.id, outcome)

    async def _process(self, type_: str, object_id: str | None) -> str:
        if not object_id:
            return IGNORED
        if type_ == "checkout.session.completed":
            session = await self._payments.checkout_session(object_id)
            if session.subscription_id is None:
                return IGNORED
            return await self.reconcile(session.subscription_id)
        if type_ in _SUBSCRIPTION_EVENTS:
            return await self.reconcile(object_id)
        if type_ in _INVOICE_EVENTS:
            invoice = await self._payments.invoice(object_id)
            if invoice.subscription_id is None:
                return IGNORED
            return await self.reconcile(
                invoice.subscription_id, invoice, BillingEvent(type_, invoice.billing_reason)
            )
        if type_ in _DISPUTE_EVENTS:
            dispute = await self._payments.dispute(object_id)
            found = (
                await self._store.seat_of_payment_intent(dispute.payment_intent_id)
                if dispute.payment_intent_id
                else None
            )
            if found is None:
                return ORPHAN
            invoice = await self._payments.invoice(found[1])
            if invoice.subscription_id is None:
                return IGNORED
            return await self.reconcile(
                invoice.subscription_id,
                invoice,
                BillingEvent(type_, invoice.billing_reason, dispute.status),
            )
        return IGNORED

    async def _seat_of(self, subscription_id: str, seat_id: str | None) -> SeatRow | None:
        seat = await self._store.seat_by_subscription(subscription_id)
        if seat is None and seat_id:
            seat = await self._store.seat(seat_id)
            if seat is not None and seat.stripe_subscription_id not in (None, subscription_id):
                return None  # that seat belongs to another subscription
        return seat

    async def reconcile(
        self,
        subscription_id: str,
        invoice: InvoiceInfo | None = None,
        event: BillingEvent | None = None,
    ) -> str:
        """Copy a seat from Stripe and apply what it means. Idempotent."""
        sub = await self._payments.subscription(subscription_id)
        seat = await self._seat_of(sub.id, sub.seat_id)
        if seat is None:
            log.warning("suscripción de Stripe sin plaza nuestra")
            return ORPHAN
        owner = await self._store.account_of_customer(sub.customer_id)
        if owner != seat.account_id or (sub.account_id and sub.account_id != seat.account_id):
            log.warning("suscripción de Stripe de otra cuenta: no se aplica")
            return ORPHAN
        if invoice is None and sub.latest_invoice_id:
            invoice = await self._payments.invoice(sub.latest_invoice_id)
        if invoice is not None and invoice.subscription_id not in (None, sub.id):
            return ORPHAN
        state = seat_state(stripe_snapshot(sub, invoice))
        decision = block_decision(event) if event is not None else None
        paid = invoice is not None and invoice.status == "paid" and not invoice.disputed
        block = state.block
        if decision is not None and decision.action is BlockAction.OPEN:
            block = decision.reason
        unblock = paid or (decision is not None and decision.action is BlockAction.CLOSE)
        if block is not None:
            unblock = False
        end_rights = None
        if state.status is SeatStatus.CANCELED:
            end_rights = state.entitled_until or self._clock()
        sync = SeatSync(
            status=state.status,
            subscription_id=sub.id,
            current_period_start=sub.current_period_start,
            current_period_end=sub.current_period_end,
            cancel_at_period_end=sub.cancel_at_period_end,
            price_key=sub.price_key,
            invoice=invoice,
            trial_ends_at=sub.trial_end,
            entitled_until=(
                state.entitled_until if paid and state.status is SeatStatus.ACTIVE else None
            ),
            end_rights_at=end_rights,
            block=block,
            unblock=unblock,
            activate=paid and invoice is not None and invoice.billing_reason == _FIRST_PURCHASE,
        )
        applied = await self._store.apply_seat(seat.id, sync)
        if applied.duplicate:
            log.warning("segundo pago para una ficha que ya tenía plaza: queda libre, a revisión")
        if applied.activation_refused:
            log.warning("plaza pagada sin poder activar su ficha: a revisión")
        return PROCESSED
