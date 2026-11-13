"""The billing store in memory, for tests and the demo without a database. One object
plays both sides (the API and the billing process) over the tables of a MemoryStore: the
profiles, entitlements and blocks it reads are those of that store."""

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from app.adapters.store.memory import BlockRow, MemoryStore
from app.domain.billing import SeatStatus
from app.domain.plans import Entitlement, Plan
from app.ports.billing_store import (
    CANCEL,
    EVENT_JOB,
    FAILED,
    PAY,
    PENDING,
    PORTAL,
    READY,
    REQUEST_JOB,
    RESUME,
    SEAT,
    AlreadyActive,
    BillingRequest,
    Customer,
    FiscalProfile,
    InvoiceDocument,
    SeatApplied,
    SeatPending,
    SeatRow,
    SeatSync,
    StripeEventRow,
)
from app.ports.store import LocationNotFound

DUPLICATE_PAYMENT = "duplicate_payment"
ACTIVATION_REFUSED = "activation_refused"


@dataclass
class _Customer:
    profile: FiscalProfile
    updated_at: datetime
    stripe_customer_id: str | None = None


@dataclass
class _Paid:
    """A paid entitlement (source 'pago'), keyed by its invoice."""

    seat_id: str
    entitlement: Entitlement


@dataclass
class MemoryBillingStore:
    store: MemoryStore
    clock: object = field(default=lambda: datetime.now(UTC))
    customers: dict[str, _Customer] = field(default_factory=dict)
    seat_rows: dict[str, SeatRow] = field(default_factory=dict)
    requests: dict[str, BillingRequest] = field(default_factory=dict)
    events: dict[str, StripeEventRow] = field(default_factory=dict)
    charges: dict[str, object] = field(default_factory=dict)
    paid: dict[str, _Paid] = field(default_factory=dict)  # by invoice id
    documents: list[tuple[str, InvoiceDocument, bytes]] = field(default_factory=list)
    # What the API enqueued, as (kind, payload): there is no queue here.
    jobs: list[tuple[str, dict]] = field(default_factory=list)
    outcomes: dict[str, str] = field(default_factory=dict)  # stripe_event.outcome
    funnel: list[tuple[str, str | None]] = field(default_factory=list)  # funnel_event

    def _now(self) -> datetime:
        return self.clock()  # type: ignore[operator]

    # --- shared ----------------------------------------------------------------------

    async def launch_at(self) -> datetime | None:
        return self.store.launch_at

    # --- API side --------------------------------------------------------------------

    async def profile(self, account_id: str) -> FiscalProfile | None:
        found = self.customers.get(account_id)
        return found.profile if found else None

    async def save_profile(self, account_id: str, profile: FiscalProfile) -> None:
        found = self.customers.get(account_id)
        if found is None:
            self.customers[account_id] = _Customer(profile, self._now())
        else:
            found.profile, found.updated_at = profile, self._now()

    async def seats(self, account_id: str) -> list[SeatRow]:
        return await self.seats_of(account_id)

    def _new_request(self, account_id: str, kind: str, **fields) -> str:
        request_id = str(uuid.uuid4())
        self.requests[request_id] = BillingRequest(
            request_id, account_id, kind, PENDING, self._now(), **fields
        )
        self.jobs.append((REQUEST_JOB, {"request_id": request_id}))
        return request_id

    async def request_seat(self, account_id: str, location_id: str, plan: Plan) -> str:
        row = self.store._own(account_id, location_id)
        if row is None or row.removed:
            raise LocationNotFound
        now = self._now()
        for request in sorted(self.requests.values(), key=lambda r: r.created_at, reverse=True):
            if (
                request.kind == SEAT
                and request.account_id == account_id
                and request.location_id == location_id
                and (
                    request.state == PENDING
                    or (request.state == READY and request.expires_at and request.expires_at > now)
                )
            ):
                raise SeatPending(request.id)
        for seat in self.seat_rows.values():
            if (
                seat.account_id == account_id
                and seat.location_id == location_id
                and seat.status is SeatStatus.ACTIVE
                and seat.current_period_end is not None
                and seat.current_period_end > now
                and row.plan is not seat.plan
            ):
                raise SeatPending(seat.request_id)
        if row.plan is plan:
            raise AlreadyActive
        return self._new_request(account_id, SEAT, location_id=location_id, plan=plan)

    async def request_for_seat(self, account_id: str, kind: str, seat_id: str) -> str | None:
        seat = self.seat_rows.get(seat_id)
        if (
            seat is None
            or seat.account_id != account_id
            or seat.collection != "auto"
            or seat.stripe_subscription_id is None
            or seat.status is SeatStatus.CANCELED
            or kind not in (CANCEL, RESUME)
        ):
            return None
        return self._new_request(account_id, kind, seat_id=seat_id)

    async def request(self, account_id: str, kind: str) -> str:
        if kind not in (PAY, PORTAL):
            raise ValueError(kind)
        return self._new_request(account_id, kind)

    async def get_request(self, account_id: str, request_id: str) -> BillingRequest | None:
        found = self.requests.get(request_id)
        return found if found is not None and found.account_id == account_id else None

    async def record_stripe_event(
        self, event_id: str, type_: str, object_id: str | None, livemode: bool, created: datetime
    ) -> bool:
        if event_id in self.events:
            return False
        self.events[event_id] = StripeEventRow(event_id, type_, object_id, livemode)
        self.jobs.append((EVENT_JOB, {"event_id": event_id}))
        return True

    async def invoices(self, account_id: str) -> list[InvoiceDocument]:
        return [d for owner, d, _ in self.documents if owner == account_id]

    async def invoice_pdf(self, account_id: str, document_id: str) -> bytes | None:
        return next(
            (
                pdf
                for owner, d, pdf in self.documents
                if owner == account_id and d.id == document_id
            ),
            None,
        )

    # --- billing process side --------------------------------------------------------

    async def get_request_by_id(self, request_id: str) -> BillingRequest | None:
        return self.requests.get(request_id)

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
        found = self.requests.get(request_id)
        if found is None or found.state != PENDING or state not in (READY, FAILED):
            return
        self.requests[request_id] = replace(
            found, state=state, url=url, code=code, detail=detail, expires_at=expires_at
        )

    async def customer(self, account_id: str) -> Customer | None:
        found = self.customers.get(account_id)
        if found is None:
            return None
        return Customer(account_id, found.profile, found.stripe_customer_id, found.updated_at)

    async def account_of_customer(self, customer_id: str) -> str | None:
        return next(
            (a for a, c in self.customers.items() if c.stripe_customer_id == customer_id), None
        )

    async def set_stripe_customer(self, account_id: str, customer_id: str) -> None:
        found = self.customers.get(account_id)
        if found is not None and found.stripe_customer_id is None:
            found.stripe_customer_id = customer_id

    async def location_owned(self, account_id: str, location_id: str) -> bool:
        row = self.store._own(account_id, location_id)
        return row is not None and not row.removed

    async def seats_of(self, account_id: str) -> list[SeatRow]:
        return sorted(
            (s for s in self.seat_rows.values() if s.account_id == account_id),
            key=lambda s: (s.created_at, s.id),
        )

    async def seat(self, seat_id: str) -> SeatRow | None:
        return self.seat_rows.get(seat_id)

    async def seat_by_subscription(self, subscription_id: str) -> SeatRow | None:
        return next(
            (s for s in self.seat_rows.values() if s.stripe_subscription_id == subscription_id),
            None,
        )

    async def create_seat(
        self, seat_id: str, account_id: str, plan: Plan, location_id: str, request_id: str
    ) -> None:
        self.seat_rows.setdefault(
            seat_id,
            SeatRow(
                seat_id,
                account_id,
                plan,
                SeatStatus.PENDING,
                self._now(),
                location_id=location_id,
                request_id=request_id,
            ),
        )

    async def set_cancel_at_period_end(self, seat_id: str, cancel: bool) -> None:
        seat = self.seat_rows.get(seat_id)
        if seat is not None:
            self.seat_rows[seat_id] = replace(seat, cancel_at_period_end=cancel)

    async def seat_of_payment_intent(self, payment_intent_id: str) -> tuple[str, str] | None:
        for invoice_id, (seat_id, invoice) in self.charges.items():  # type: ignore[misc]
            if invoice.payment_intent_id == payment_intent_id:
                return seat_id, invoice_id
        return None

    def _sync_entitlements(self, account_id: str) -> None:
        account = self.store._account(account_id)
        paid = {id(p.entitlement) for p in self.paid.values()}
        others = [e for e in account.entitlements if id(e) not in paid]
        mine = [
            p.entitlement
            for p in self.paid.values()
            if self.seat_rows[p.seat_id].account_id == account_id
        ]
        account.entitlements = others + mine

    async def apply_seat(self, seat_id: str, sync: SeatSync) -> SeatApplied:
        seat = self.seat_rows.get(seat_id)
        if seat is None:
            return SeatApplied()
        now = self._now()
        seat = replace(
            seat,
            stripe_subscription_id=seat.stripe_subscription_id or sync.subscription_id,
            status=sync.status,
            current_period_start=sync.current_period_start,
            current_period_end=sync.current_period_end,
            cancel_at_period_end=sync.cancel_at_period_end,
            next_price_key=sync.price_key or seat.next_price_key,
        )
        self.seat_rows[seat_id] = seat
        invoice = sync.invoice
        if invoice is not None:
            self.charges[invoice.id] = (seat_id, invoice)
        granted = False
        if invoice is not None and sync.entitled_until is not None and invoice.id not in self.paid:
            for other in self.paid.values():
                old = other.entitlement
                if other.seat_id == seat_id and (old.ends_at is None or old.ends_at > now):
                    other.entitlement = replace(old, ends_at=now)
            self.paid[invoice.id] = _Paid(
                seat_id, Entitlement(seat.plan, 1, now, sync.entitled_until)
            )
            granted = True
        if sync.end_rights_at is not None:
            for other in self.paid.values():
                old = other.entitlement
                if other.seat_id == seat_id and (
                    old.ends_at is None or old.ends_at > sync.end_rights_at
                ):
                    other.entitlement = replace(old, ends_at=sync.end_rights_at)
        self._sync_entitlements(seat.account_id)
        blocks = self.store.billing_blocks
        if invoice is not None and sync.block is not None:
            if not any(
                b.account_id == seat.account_id
                and b.stripe_invoice_id == invoice.id
                and b.unblocked_at is None
                for b in blocks
            ):
                blocks.append(BlockRow(seat.account_id, sync.block.value, now, invoice.id, seat_id))
        if invoice is not None and sync.unblock:
            for block in blocks:
                if (
                    block.account_id == seat.account_id
                    and block.stripe_invoice_id == invoice.id
                    and block.unblocked_at is None
                ):
                    block.unblocked_at, block.unblocked_by = now, "payment"
        if not (sync.activate and granted) or seat.location_id is None:
            return SeatApplied(granted=granted)
        if any(
            s.id != seat_id
            and s.account_id == seat.account_id
            and s.location_id == seat.location_id
            and s.status is SeatStatus.ACTIVE
            and s.current_period_end is not None
            and s.current_period_end > now
            for s in self.seat_rows.values()
        ):
            self.seat_rows[seat_id] = replace(
                seat, location_id=None, review_reason=DUPLICATE_PAYMENT
            )
            return SeatApplied(granted=granted, duplicate=True)
        row = self.store._own(seat.account_id, seat.location_id)
        try:
            if row is None:
                raise LocationNotFound
            self.store._activate(row, seat.plan)
        except Exception as exc:  # as the database refusing: the seat stays, for review
            self.seat_rows[seat_id] = replace(seat, review_reason=ACTIVATION_REFUSED)
            return SeatApplied(granted=granted, activation_refused=type(exc).__name__)
        return SeatApplied(granted=granted, activated=True)

    async def stripe_event(self, event_id: str) -> StripeEventRow | None:
        return self.events.get(event_id)

    async def finish_event(self, event_id: str, outcome: str) -> None:
        found = self.events.get(event_id)
        if found is not None:
            self.events[event_id] = replace(found, processed_at=self._now())
            self.outcomes[event_id] = outcome

    async def record_funnel(self, kind: str, source: str | None = None) -> None:
        self.funnel.append((kind, source))
