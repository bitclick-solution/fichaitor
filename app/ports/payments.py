"""Payments port: what `fichaitor-billing` asks of Stripe (docs/admin-facturacion.md,
sections 3 and 4.2). Only `fichaitor-billing` holds an implementation: the API never
calls Stripe (rule 2 of docs/06-estandares.md).

Rules every implementation keeps:

- One Stripe subscription per seat, one item each, with `metadata = {account_id,
  seat_id}`. No trial and no proration: a seat is charged a whole year the day it is
  bought (P2).
- Card only (decision 9.0, 5 and 6): Apple Pay and Google Pay are cards to Stripe.
- Prices are found by their `lookup_key` (`PriceKey`); no `price_…` id lives in code.
- VAT is a fixed Tax Rate per `TaxTreatment` (section 2.6), never Stripe Tax.
- Every write takes an `idempotency_key`, chosen by the caller from what it does (for
  example `seat:<seat_id>:create`), so a job cut halfway and run again does not charge
  twice. The same key with other parameters is refused (`PaymentsRejected`,
  code `idempotency_error`).
- No card data: nothing here carries a card number, an expiry, a CVC or an IBAN.
  Stripe collects them on its own pages.
- Errors carry a status and Stripe's error code, never the key, a request body or
  Stripe's message (which may echo what it was sent).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar, Protocol

from app.domain.billing import PriceKey, StripeSnapshot, TaxTreatment


class PaymentsError(Exception):
    """Stripe did not do what was asked. `retryable` says whether the same call may work
    later: the job queue retries it with backoff, or marks it failed for good."""

    retryable: ClassVar[bool] = True

    def __init__(self, message: str = "", *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class PaymentsUnavailable(PaymentsError):
    """Temporary: network error, time out, 429 or 5xx. A write may or may not have reached
    Stripe: retry it with the same idempotency key."""

    retryable = True


class PaymentsRejected(PaymentsError):
    """Permanent: Stripe refused the request (4xx). The same call will fail again."""

    retryable = False


class PaymentsNotFound(PaymentsRejected):
    """The object does not exist in this Stripe account and mode (404). An event whose
    object id was made up ends here (`outcome = 'not_found'`, section 4.2)."""


class PaymentDeclined(PaymentsRejected):
    """The card was declined (402). `code` is Stripe's (`card_declined`, ...)."""


@dataclass(frozen=True)
class Address:
    line1: str = field(repr=False)
    postal_code: str = field(repr=False)
    city: str = field(repr=False)
    country: str  # ISO 3166-1 alpha-2
    line2: str | None = field(default=None, repr=False)
    state: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class CustomerDetails:
    """What goes to Stripe of an account (section 4.8): fiscal data only. Personal fields
    stay out of `repr`, so a log of the object does not spill them."""

    legal_name: str = field(repr=False)
    tax_id: str = field(repr=False)
    tax_id_type: str  # `es_cif` or `eu_vat`, from TaxProfile.stripe_tax_id_type
    address: Address
    invoice_email: str = field(repr=False)


@dataclass(frozen=True)
class Price:
    id: str
    lookup_key: PriceKey
    unit_amount: int  # cents, without VAT
    currency: str
    interval: str | None


@dataclass(frozen=True)
class CheckoutSession:
    id: str
    url: str


@dataclass(frozen=True)
class CheckoutInfo:
    """A Checkout session read again from Stripe (`checkout.session.completed`): its
    subscription, once paid, and our metadata."""

    id: str
    status: str | None  # open, complete or expired
    subscription_id: str | None
    customer_id: str | None
    account_id: str | None  # metadata
    seat_id: str | None


@dataclass(frozen=True)
class SubscriptionInfo:
    """A subscription of a seat, as read from Stripe now."""

    id: str
    customer_id: str
    status: str  # incomplete, active, past_due, unpaid, canceled, incomplete_expired, ...
    price_key: PriceKey | None
    current_period_start: datetime
    current_period_end: datetime
    cancel_at_period_end: bool
    canceled_at: datetime | None
    cancellation_reason: str | None
    account_id: str | None  # metadata
    seat_id: str | None  # metadata
    latest_invoice_id: str | None
    latest_invoice_status: str | None
    hosted_invoice_url: str | None  # to pay a pending invoice (3DS or unblock)


@dataclass(frozen=True)
class InvoiceInfo:
    """An invoice of Stripe (a payment document, never the legal invoice: section 2.3)
    with the facts of its payment, read again from Stripe."""

    id: str
    customer_id: str
    subscription_id: str | None
    status: str | None  # draft, open, paid, void, uncollectible
    billing_reason: str | None  # subscription_create, subscription_cycle, ...
    currency: str
    subtotal: int  # cents, without VAT
    tax: int  # cents
    total: int  # cents
    amount_paid: int  # cents
    price_key: PriceKey | None
    period_start: datetime | None  # the year the line pays
    period_end: datetime | None
    paid_at: datetime | None
    hosted_invoice_url: str | None
    payment_intent_id: str | None
    payment_intent_status: str | None
    charge_id: str | None
    disputed: bool
    amount_refunded: int  # cents
    account_id: str | None  # metadata of the subscription
    seat_id: str | None


@dataclass(frozen=True)
class RefundInfo:
    id: str
    invoice_id: str | None
    payment_intent_id: str | None
    charge_id: str | None
    amount: int  # cents, VAT included
    status: str | None  # pending, succeeded, failed, canceled


@dataclass(frozen=True)
class DisputeInfo:
    id: str
    charge_id: str | None
    payment_intent_id: str | None
    amount: int
    status: str  # warning_needs_response, needs_response, under_review, won, lost, ...


def stripe_snapshot(sub: SubscriptionInfo, invoice: InvoiceInfo | None) -> StripeSnapshot:
    """What `billing.seat_state` needs, from a subscription and its latest invoice."""
    return StripeSnapshot(
        subscription_status=sub.status,
        current_period_end=sub.current_period_end,
        cancel_at_period_end=sub.cancel_at_period_end,
        canceled_at=sub.canceled_at,
        cancellation_reason=sub.cancellation_reason,
        invoice_status=invoice.status if invoice else sub.latest_invoice_status,
        invoice_billing_reason=invoice.billing_reason if invoice else None,
        payment_intent_status=invoice.payment_intent_status if invoice else None,
        disputed=invoice.disputed if invoice else False,
    )


class Payments(Protocol):
    """Stripe as `fichaitor-billing` uses it. One request per call where possible; the
    implementation may retry a temporary failure with the same idempotency key."""

    async def create_customer(
        self, account_id: str, details: CustomerDetails, *, idempotency_key: str
    ) -> str:
        """The `cus_…` of a new customer with its tax id and `metadata.account_id`."""
        ...

    async def update_customer(
        self, customer_id: str, details: CustomerDetails, *, idempotency_key: str
    ) -> None:
        """Overwrite the fiscal data of the customer with ours, tax id included."""
        ...

    async def prices(self) -> dict[PriceKey, Price]:
        """The active price of each `PriceKey`. PaymentsRejected (`price_missing`) if one
        is missing: `scripts/stripe_bootstrap.py` has not run."""
        ...

    async def tax_rates(self) -> dict[TaxTreatment, str]:
        """The `txr_…` of each treatment. PaymentsRejected (`tax_rate_missing`) if one is
        missing."""
        ...

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
    ) -> CheckoutSession:
        """A hosted Checkout in `subscription` mode, card only, for a seat of a customer
        without a saved card."""
        ...

    async def checkout_session(self, session_id: str) -> CheckoutInfo:
        """A Checkout session, to find the subscription it created."""
        ...

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
        """A seat charged at once to the saved card. If the bank asks for 3DS, the
        subscription stays `incomplete` and `hosted_invoice_url` is where to confirm."""
        ...

    async def subscription(self, subscription_id: str) -> SubscriptionInfo: ...

    async def set_cancel_at_period_end(
        self, subscription_id: str, cancel: bool, *, idempotency_key: str
    ) -> SubscriptionInfo:
        """Cancel a seat at the end of its paid year, or undo it before that date."""
        ...

    async def set_renewal_price(
        self, subscription_id: str, price_key: PriceKey, *, idempotency_key: str
    ) -> SubscriptionInfo:
        """Freeze the price of the next renewal (T-30, section 3.3), with no proration."""
        ...

    async def invoice(self, invoice_id: str) -> InvoiceInfo: ...

    async def open_invoices(self, customer_id: str) -> list[InvoiceInfo]:
        """The unpaid invoices of a customer, to pay and unblock (section 3.6)."""
        ...

    async def paid_invoices_since(self, since: datetime) -> list[InvoiceInfo]:
        """Every paid invoice created at `since` or later, of every customer: the daily
        reconciliation (section 4.4) checks each has its seat, its charge and its legal
        invoice."""
        ...

    async def portal_url(self, customer_id: str, return_url: str) -> str:
        """A customer portal session, to change the card only (section 3.4)."""
        ...

    async def refund(
        self, invoice_id: str, amount: int | None, reason: str, *, idempotency_key: str
    ) -> RefundInfo:
        """An exceptional refund of an invoice (P2), whole if `amount` is None. Only an
        administrator asks for it, through `billing_refund`."""
        ...

    async def refund_info(self, refund_id: str) -> RefundInfo: ...

    async def refunds_since(self, since: datetime) -> list[RefundInfo]:
        """Every refund created at `since` or later: each needs its credit note."""
        ...

    async def dispute(self, dispute_id: str) -> DisputeInfo: ...
