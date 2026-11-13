"""Billing store: what the API and the billing process keep of the charging with Stripe
(docs/admin-facturacion.md, sections 4 and 5; db/schema_billing.sql).

Two sides, two database roles:

- `BillingStore`, the API (fichaitor_api): the fiscal profile, the requests to the
  billing process, the ids of the Stripe events that arrive signed, and reads of seats,
  documents, settings and blocks. It never writes seats, entitlements nor blocks, and it
  never calls Stripe.
- `BillingProcessStore`, the billing process (fichaitor_billing): the only one that talks
  to Stripe. It resolves the requests, copies seats and charges from what it reads again
  in Stripe, grants the paid entitlements, blocks and unblocks, and activates the profile
  of a new seat.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.domain.billing import BlockReason, PriceKey, Seat, SeatStatus, TaxTreatment, Territory
from app.domain.plans import Plan
from app.ports.payments import InvoiceInfo

# billing_request.kind
SEAT = "seat"
CANCEL = "cancel"
RESUME = "resume"
PAY = "pay"
PORTAL = "portal"
REQUEST_KINDS = (SEAT, CANCEL, RESUME, PAY, PORTAL)

# billing_request.state: in Spanish, as the actions (docs/contrato-web-api.md).
PENDING = "pendiente"
READY = "lista"
FAILED = "fallida"

# The job kinds of the billing process, with their idempotency key "<kind>:<id>".
REQUEST_JOB = "billing_request"
EVENT_JOB = "stripe_event"


@dataclass(frozen=True)
class FiscalProfile:
    """`billing_customer`, as the API keeps it. The derived fields (type of tax id,
    territory and treatment) come from `billing.tax_treatment`, never from the web."""

    legal_name: str = field(repr=False)
    tax_id: str = field(repr=False)
    tax_id_type: str  # es_cif or eu_vat
    tax_id_kind: str  # nif, nie, cif or eu_vat
    address_line1: str = field(repr=False)
    postal_code: str = field(repr=False)
    city: str = field(repr=False)
    country: str
    territory: Territory
    tax_treatment: TaxTreatment
    invoice_email: str = field(repr=False)
    address_line2: str | None = field(default=None, repr=False)
    province: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Customer:
    """A fiscal profile with its Stripe customer, for the billing process."""

    account_id: str
    profile: FiscalProfile
    stripe_customer_id: str | None
    updated_at: datetime


@dataclass(frozen=True)
class SeatRow:
    id: str
    account_id: str
    plan: Plan
    status: SeatStatus
    created_at: datetime
    location_id: str | None = None
    collection: str = "auto"
    stripe_subscription_id: str | None = None
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    cancel_at_period_end: bool = False
    next_price_key: PriceKey | None = None
    request_id: str | None = None
    review_reason: str | None = None

    def domain(self) -> Seat:
        return Seat(
            self.id,
            self.plan,
            self.created_at,
            self.current_period_end,
            self.cancel_at_period_end,
            self.location_id,
        )


def seats_in_force(seats: Sequence[SeatRow], now: datetime) -> list[Seat]:
    """The paid seats of an account that count for the 5 % now: charged at least once
    (a period) and whose paid year has not ended."""
    return [
        s.domain()
        for s in seats
        if s.current_period_end is not None
        and s.current_period_end > now
        and s.status is not SeatStatus.PENDING
    ]


@dataclass(frozen=True)
class BillingRequest:
    id: str
    account_id: str
    kind: str
    state: str
    created_at: datetime
    location_id: str | None = None
    plan: Plan | None = None
    seat_id: str | None = None
    url: str | None = None
    code: str | None = None
    detail: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True)
class InvoiceDocument:
    """A legal invoice (or credit note) of Odoo, once issued (step 9)."""

    id: str
    kind: str  # factura or rectificativa
    number: str | None
    amount_total: int | None
    issued_at: datetime | None


@dataclass(frozen=True)
class StripeEventRow:
    id: str
    type: str
    object_id: str | None
    livemode: bool
    processed_at: datetime | None = None


class SeatPending(Exception):
    """A seat for that profile is already on its way: a request pending, a Checkout open
    and not expired, or a seat charged that has not activated the profile yet. Answered
    with 409 `seat_pending` and the id of that request, so that a second tab does not
    pay twice."""

    def __init__(self, request_id: str | None) -> None:
        super().__init__("seat_pending")
        self.request_id = request_id


class AlreadyActive(Exception):
    """That profile is already active in the plan asked for."""


class BillingStore(Protocol):
    """The API side. Every method is scoped to the account of the session."""

    async def launch_at(self) -> datetime | None:
        """`billing_setting.launch_at`: None during the beta (nothing is charged)."""
        ...

    async def profile(self, account_id: str) -> FiscalProfile | None: ...

    async def save_profile(self, account_id: str, profile: FiscalProfile) -> None: ...

    async def seats(self, account_id: str) -> list[SeatRow]: ...

    async def request_seat(self, account_id: str, location_id: str, plan: Plan) -> str:
        """Create a `seat` request for that profile and enqueue it, atomically with the
        checks of `SeatPending` (two tabs at once get the same answer). Raises
        LocationNotFound (not a profile of the account, or removed), AlreadyActive or
        SeatPending."""
        ...

    async def request_for_seat(self, account_id: str, kind: str, seat_id: str) -> str | None:
        """A cancel or resume request of a seat of the account with a subscription, and
        enqueue it. None if there is no such seat."""
        ...

    async def request(self, account_id: str, kind: str) -> str:
        """A pay or portal request, enqueued."""
        ...

    async def get_request(self, account_id: str, request_id: str) -> BillingRequest | None:
        """None for a request of another account or that does not exist."""
        ...

    async def record_stripe_event(
        self, event_id: str, type_: str, object_id: str | None, livemode: bool, created: datetime
    ) -> bool:
        """Note the ids of a signed Stripe event and enqueue its processing, once per
        event id. Never its payload. False if it had arrived already."""
        ...

    async def invoices(self, account_id: str) -> list[InvoiceDocument]: ...

    async def invoice_pdf(self, account_id: str, document_id: str) -> bytes | None: ...


@dataclass(frozen=True)
class SeatSync:
    """What the billing process copies to a seat, and decides, after reading Stripe."""

    status: SeatStatus
    subscription_id: str
    current_period_start: datetime | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    price_key: PriceKey | None
    invoice: InvoiceInfo | None = None
    # A paid entitlement for that invoice until this date (None: grant nothing new).
    entitled_until: datetime | None = None
    # The right of the seat ends here (a cancellation): never later than it was.
    end_rights_at: datetime | None = None
    block: BlockReason | None = None  # open a block for `invoice`
    unblock: bool = False  # close the block of `invoice`: it was paid
    activate: bool = False  # first purchase paid: activate its profile in its plan


@dataclass(frozen=True)
class SeatApplied:
    granted: bool = False
    activated: bool = False
    duplicate: bool = False  # a second seat paid for a profile that had one: left free
    activation_refused: str | None = None


class BillingProcessStore(Protocol):
    """The billing process side (fichaitor_billing)."""

    async def launch_at(self) -> datetime | None: ...

    async def get_request_by_id(self, request_id: str) -> BillingRequest | None: ...

    async def finish_request(
        self,
        request_id: str,
        state: str,
        *,
        url: str | None = None,
        code: str | None = None,
        detail: str | None = None,
        expires_at: datetime | None = None,
    ) -> None:
        """Resolve a pending request once (lista or fallida)."""
        ...

    async def customer(self, account_id: str) -> Customer | None: ...

    async def account_of_customer(self, customer_id: str) -> str | None: ...

    async def set_stripe_customer(self, account_id: str, customer_id: str) -> None: ...

    async def location_owned(self, account_id: str, location_id: str) -> bool:
        """A profile of the account, not removed."""
        ...

    async def seats_of(self, account_id: str) -> list[SeatRow]: ...

    async def seat(self, seat_id: str) -> SeatRow | None: ...

    async def seat_by_subscription(self, subscription_id: str) -> SeatRow | None: ...

    async def create_seat(
        self, seat_id: str, account_id: str, plan: Plan, location_id: str, request_id: str
    ) -> None:
        """A pending seat for a Checkout. Repeating it changes nothing."""
        ...

    async def set_cancel_at_period_end(self, seat_id: str, cancel: bool) -> None: ...

    async def apply_seat(self, seat_id: str, sync: SeatSync) -> SeatApplied:
        """In one transaction: copy the seat and its invoice, grant the entitlement of a
        paid invoice once (by its id), end the rights of a cancelled seat, open or close
        the block of the invoice and, on a first purchase, activate its profile (or leave
        the seat free if another seat already holds that profile)."""
        ...

    async def seat_of_payment_intent(self, payment_intent_id: str) -> tuple[str, str] | None:
        """(seat id, invoice id) of the charge paid by that PaymentIntent."""
        ...

    async def stripe_event(self, event_id: str) -> StripeEventRow | None: ...

    async def finish_event(self, event_id: str, outcome: str) -> None: ...
