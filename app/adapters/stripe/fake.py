"""Fake Stripe for tests and local development: customers, one subscription per seat,
their invoices, refunds and disputes, in memory.

Keeps the contract of `app.ports.payments`: an idempotency key returns the first result
and is refused with other parameters, unknown ids are PaymentsNotFound, card only, a
whole year with no proration (or a trial until `trial_end`, the beta window, with a
first invoice of 0), and nothing about the card. `card` sets what the next
charge does (`ok`, `declined` or `3ds`); `fail_next` makes the next calls fail; `renew`
and `open_dispute` move time and money as Stripe would."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from app.domain.billing import TAX_PERCENT, PriceKey, TaxTreatment, price_cents
from app.ports.payments import (
    CheckoutInfo,
    CheckoutSession,
    CustomerDetails,
    DisputeInfo,
    InvoiceInfo,
    PaymentsError,
    PaymentsNotFound,
    PaymentsRejected,
    Price,
    RefundInfo,
    SubscriptionInfo,
)

CardOutcome = Literal["ok", "declined", "3ds"]
TAX_ID_TYPES = ("es_cif", "eu_vat")


def _next_year(day: datetime) -> datetime:
    try:
        return day.replace(year=day.year + 1)
    except ValueError:  # 29 February
        return day.replace(year=day.year + 1, day=28)


@dataclass
class _Seat:
    subscription: SubscriptionInfo
    treatment: TaxTreatment
    renewal_key: PriceKey


class FakePayments:
    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.now = now
        self.card: CardOutcome = "ok"
        self.customers: dict[str, CustomerDetails] = {}
        self.customer_accounts: dict[str, str] = {}
        self.seats: dict[str, _Seat] = {}
        self.invoices: dict[str, InvoiceInfo] = {}
        self.checkouts: dict[str, dict[str, Any]] = {}
        self.refunds: dict[str, RefundInfo] = {}
        self.disputes: dict[str, DisputeInfo] = {}
        self.failures: list[PaymentsError] = []
        self.calls: list[str] = []
        self._idempotency: dict[str, tuple[str, Any, Any]] = {}
        self._counter = 0

    # --- test helpers --------------------------------------------------------------

    def fail_next(self, *errors: PaymentsError) -> None:
        self.failures.extend(errors)

    def _start(self, operation: str) -> None:
        self.calls.append(operation)
        if self.failures:
            raise self.failures.pop(0)

    def _new(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_fake{self._counter:05d}"

    def _once(self, key: str, operation: str, params: Any, make: Callable[[], Any]) -> Any:
        """Stripe's idempotency: the first result again, or a refusal with other params."""
        if not key:
            raise ValueError("idempotency_key is required")
        if key in self._idempotency:
            done_operation, done_params, result = self._idempotency[key]
            if (done_operation, done_params) != (operation, params):
                raise PaymentsRejected(
                    f"Stripe {operation}: clave de idempotencia reutilizada",
                    status=400,
                    code="idempotency_error",
                )
            return result
        result = make()
        self._idempotency[key] = (operation, params, result)
        return result

    def _seat(self, subscription_id: str) -> _Seat:
        seat = self.seats.get(subscription_id)
        if seat is None:
            raise PaymentsNotFound("Stripe: no existe", status=404, code="resource_missing")
        return seat

    def _customer(self, customer_id: str) -> None:
        if customer_id not in self.customers:
            raise PaymentsNotFound("Stripe: no existe", status=404, code="resource_missing")

    def _charge(
        self,
        seat: _Seat,
        key: PriceKey,
        start: datetime,
        end: datetime,
        reason: str,
        *,
        free: bool = False,
    ) -> InvoiceInfo:
        """An invoice of the seat. `free`: the one of 0 at the start of a trial, paid at
        once and without a payment."""
        sub = seat.subscription
        base = 0 if free else price_cents(key)
        tax = (base * TAX_PERCENT[seat.treatment] + 50) // 100
        outcome = "free" if free else self.card
        paid = outcome in ("ok", "free")
        intent_status = {
            "ok": "succeeded",
            "declined": "requires_payment_method",
            "free": None,
        }.get(outcome, "requires_action")
        invoice_id = self._new("in")
        invoice = InvoiceInfo(
            id=invoice_id,
            customer_id=sub.customer_id,
            subscription_id=sub.id,
            status="paid" if paid else "open",
            billing_reason=reason,
            currency="eur",
            subtotal=base,
            tax=tax,
            total=base + tax,
            amount_paid=base + tax if paid else 0,
            price_key=key,
            period_start=start,
            period_end=end,
            paid_at=self.now() if paid else None,
            hosted_invoice_url=f"https://invoice.stripe.com/i/test_{invoice_id}",
            payment_intent_id=None if free else self._new("pi"),
            payment_intent_status=intent_status,
            charge_id=self._new("ch") if paid and not free else None,
            disputed=False,
            amount_refunded=0,
            account_id=sub.account_id,
            seat_id=sub.seat_id,
        )
        self.invoices[invoice_id] = invoice
        return invoice

    def renew(self, subscription_id: str, card: CardOutcome | None = None) -> InvoiceInfo:
        """The anniversary of a seat (or the end of its trial): a new year charged at the
        frozen price, or the end of a seat cancelled at period end."""
        seat = self._seat(subscription_id)
        sub = seat.subscription
        if card is not None:
            self.card = card
        if sub.cancel_at_period_end:
            seat.subscription = replace(
                sub,
                status="canceled",
                canceled_at=sub.current_period_end,
                cancellation_reason="cancellation_requested",
            )
            raise PaymentsRejected("Stripe: la plaza no se renueva", code="canceled")
        start, end = sub.current_period_end, _next_year(sub.current_period_end)
        invoice = self._charge(seat, seat.renewal_key, start, end, "subscription_cycle")
        paid = invoice.status == "paid"
        seat.subscription = replace(
            sub,
            status="active" if paid else "past_due",
            price_key=seat.renewal_key,
            current_period_start=start,
            current_period_end=end,
            latest_invoice_id=invoice.id,
            latest_invoice_status=invoice.status,
            hosted_invoice_url=invoice.hosted_invoice_url,
        )
        return invoice

    def complete_checkout(self, session_id: str) -> SubscriptionInfo:
        """The customer pays in the hosted Checkout."""
        pending = self.checkouts[session_id]
        params = {k: pending[k] for k in ("customer_id", "price_key", "treatment", "account_id")}
        sub = self._subscribe(
            **params, seat_id=pending["seat_id"], trial_end=pending.get("trial_end")
        )
        pending["subscription_id"] = sub.id
        pending["status"] = "complete"
        return sub

    def open_dispute(self, invoice_id: str) -> DisputeInfo:
        invoice = self.invoices[invoice_id]
        self.invoices[invoice_id] = replace(invoice, disputed=True)
        dispute = DisputeInfo(
            id=self._new("dp"),
            charge_id=invoice.charge_id,
            payment_intent_id=invoice.payment_intent_id,
            amount=invoice.amount_paid,
            status="needs_response",
        )
        self.disputes[dispute.id] = dispute
        return dispute

    # --- the port ------------------------------------------------------------------

    async def create_customer(
        self, account_id: str, details: CustomerDetails, *, idempotency_key: str
    ) -> str:
        self._start("customers.create")
        if details.tax_id_type not in TAX_ID_TYPES:
            raise PaymentsRejected("Stripe: tipo de NIF", status=400, code="tax_id_invalid")

        def make() -> str:
            customer_id = self._new("cus")
            self.customers[customer_id] = details
            self.customer_accounts[customer_id] = account_id
            return customer_id

        return self._once(idempotency_key, "customers.create", (account_id, details), make)

    async def update_customer(
        self, customer_id: str, details: CustomerDetails, *, idempotency_key: str
    ) -> None:
        self._start("customers.update")
        self._customer(customer_id)
        if details.tax_id_type not in TAX_ID_TYPES:
            raise PaymentsRejected("Stripe: tipo de NIF", status=400, code="tax_id_invalid")

        def make() -> None:
            self.customers[customer_id] = details

        self._once(idempotency_key, "customers.update", (customer_id, details), make)

    async def prices(self) -> dict[PriceKey, Price]:
        self._start("prices.list")
        return {
            key: Price(f"price_fake_{key.value}", key, price_cents(key), "eur", "year")
            for key in PriceKey
        }

    async def tax_rates(self) -> dict[TaxTreatment, str]:
        self._start("tax_rates.list")
        return {t: f"txr_fake_{t.value}" for t in TaxTreatment}

    async def create_checkout(
        self,
        customer_id: str,
        price_key: PriceKey,
        treatment: TaxTreatment,
        account_id: str,
        seat_id: str,
        success_url: str,
        cancel_url: str,
        *,
        idempotency_key: str,
        trial_end: datetime | None = None,
    ) -> CheckoutSession:
        self._start("checkout.sessions.create")
        self._customer(customer_id)
        if trial_end is not None and trial_end < self.now() + timedelta(hours=48):
            # As Stripe: a trial_end has to be at least 48 hours in the future.
            raise PaymentsRejected("Stripe: trial_end", status=400, code="parameter_invalid")
        params = (
            customer_id,
            price_key,
            treatment,
            account_id,
            seat_id,
            success_url,
            cancel_url,
            trial_end,
        )

        def make() -> CheckoutSession:
            session_id = self._new("cs")
            self.checkouts[session_id] = {
                "customer_id": customer_id,
                "price_key": price_key,
                "treatment": treatment,
                "account_id": account_id,
                "seat_id": seat_id,
                "trial_end": trial_end,
                "payment_method_collection": "always",
            }
            return CheckoutSession(session_id, f"https://checkout.stripe.com/c/pay/{session_id}")

        return self._once(idempotency_key, "checkout.sessions.create", params, make)

    def _subscribe(
        self,
        customer_id: str,
        price_key: PriceKey,
        treatment: TaxTreatment,
        account_id: str,
        seat_id: str,
        trial_end: datetime | None = None,
    ) -> SubscriptionInfo:
        start = self.now()
        end = trial_end or _next_year(start)
        subscription_id = self._new("sub")
        sub = SubscriptionInfo(
            id=subscription_id,
            customer_id=customer_id,
            status="incomplete",
            price_key=price_key,
            current_period_start=start,
            current_period_end=end,
            cancel_at_period_end=False,
            canceled_at=None,
            cancellation_reason=None,
            account_id=account_id,
            seat_id=seat_id,
            latest_invoice_id=None,
            latest_invoice_status=None,
            hosted_invoice_url=None,
            trial_end=trial_end,
        )
        seat = _Seat(sub, treatment, price_key)
        self.seats[subscription_id] = seat
        invoice = self._charge(
            seat, price_key, start, end, "subscription_create", free=trial_end is not None
        )
        status = "active" if invoice.status == "paid" else "incomplete"
        seat.subscription = replace(
            sub,
            status="trialing" if trial_end is not None else status,
            latest_invoice_id=invoice.id,
            latest_invoice_status=invoice.status,
            hosted_invoice_url=invoice.hosted_invoice_url,
        )
        return seat.subscription

    async def checkout_session(self, session_id: str) -> CheckoutInfo:
        self._start("checkout.sessions.retrieve")
        found = self.checkouts.get(session_id)
        if found is None:
            raise PaymentsNotFound("Stripe: no existe", status=404, code="resource_missing")
        return CheckoutInfo(
            id=session_id,
            status=found.get("status", "open"),
            subscription_id=found.get("subscription_id"),
            customer_id=found["customer_id"],
            account_id=found["account_id"],
            seat_id=found["seat_id"],
        )

    async def create_subscription(
        self,
        customer_id: str,
        price_key: PriceKey,
        treatment: TaxTreatment,
        account_id: str,
        seat_id: str,
        *,
        idempotency_key: str,
    ) -> SubscriptionInfo:
        self._start("subscriptions.create")
        self._customer(customer_id)
        params = (customer_id, price_key, treatment, account_id, seat_id)
        return self._once(
            idempotency_key,
            "subscriptions.create",
            params,
            lambda: self._subscribe(*params),
        )

    async def subscription(self, subscription_id: str) -> SubscriptionInfo:
        self._start("subscriptions.retrieve")
        return self._seat(subscription_id).subscription

    async def set_cancel_at_period_end(
        self, subscription_id: str, cancel: bool, *, idempotency_key: str
    ) -> SubscriptionInfo:
        self._start("subscriptions.update")
        seat = self._seat(subscription_id)

        def make() -> SubscriptionInfo:
            seat.subscription = replace(seat.subscription, cancel_at_period_end=cancel)
            return seat.subscription

        return self._once(
            idempotency_key, "subscriptions.update", (subscription_id, "cancel", cancel), make
        )

    async def set_renewal_price(
        self, subscription_id: str, price_key: PriceKey, *, idempotency_key: str
    ) -> SubscriptionInfo:
        self._start("subscriptions.update")
        seat = self._seat(subscription_id)

        def make() -> SubscriptionInfo:
            seat.renewal_key = price_key
            seat.subscription = replace(seat.subscription, price_key=price_key)
            return seat.subscription

        return self._once(
            idempotency_key, "subscriptions.update", (subscription_id, "price", price_key), make
        )

    async def invoice(self, invoice_id: str) -> InvoiceInfo:
        self._start("invoices.retrieve")
        invoice = self.invoices.get(invoice_id)
        if invoice is None:
            raise PaymentsNotFound("Stripe: no existe", status=404, code="resource_missing")
        return invoice

    async def open_invoices(self, customer_id: str) -> list[InvoiceInfo]:
        self._start("invoices.list")
        return [
            i for i in self.invoices.values() if i.customer_id == customer_id and i.status == "open"
        ]

    async def paid_invoices_since(self, since: datetime) -> list[InvoiceInfo]:
        self._start("invoices.list")
        return [
            i
            for i in self.invoices.values()
            if i.status == "paid" and i.paid_at is not None and i.paid_at >= since
        ]

    async def portal_url(self, customer_id: str, return_url: str) -> str:
        self._start("billing_portal.sessions.create")
        self._customer(customer_id)
        return f"https://billing.stripe.com/p/session/test_{customer_id}"

    async def refund(
        self, invoice_id: str, amount: int | None, reason: str, *, idempotency_key: str
    ) -> RefundInfo:
        self._start("refunds.create")
        invoice = await self.invoice(invoice_id)
        if invoice.status != "paid":
            raise PaymentsRejected("Stripe: la factura no está cobrada", code="not_paid")

        def make() -> RefundInfo:
            current = self.invoices[invoice_id]
            left = current.amount_paid - current.amount_refunded
            value = left if amount is None else amount
            if not 0 < value <= left:
                raise PaymentsRejected("Stripe: importe", status=400, code="amount_too_large")
            refund = RefundInfo(
                id=self._new("re"),
                invoice_id=invoice_id,
                payment_intent_id=current.payment_intent_id,
                charge_id=current.charge_id,
                amount=value,
                status="succeeded",
            )
            self.refunds[refund.id] = refund
            self.invoices[invoice_id] = replace(
                current, amount_refunded=current.amount_refunded + value
            )
            return refund

        return self._once(idempotency_key, "refunds.create", (invoice_id, amount, reason), make)

    async def refund_info(self, refund_id: str) -> RefundInfo:
        self._start("refunds.retrieve")
        refund = self.refunds.get(refund_id)
        if refund is None:
            raise PaymentsNotFound("Stripe: no existe", status=404, code="resource_missing")
        return refund

    async def refunds_since(self, since: datetime) -> list[RefundInfo]:
        self._start("refunds.list")
        return list(self.refunds.values())

    async def dispute(self, dispute_id: str) -> DisputeInfo:
        self._start("disputes.retrieve")
        found = self.disputes.get(dispute_id)
        if found is None:
            raise PaymentsNotFound("Stripe: no existe", status=404, code="resource_missing")
        return found
