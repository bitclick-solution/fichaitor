"""Job billing_request: what the web asked of the billing process (docs/admin-facturacion.md,
4.1 and 4.7). The API never calls Stripe: it writes a `billing_request` and enqueues this
job; the web polls GET /me/billing/requests/{id}.

- seat: a hosted Checkout (card only) for a seat of a plan for one profile, at the price
  of `billing.seat_price` over the seats in force (the 5 % from the second one), with the
  VAT of the customer's fiscal profile. The idempotency key of the Checkout is derived
  from the request: a job cut halfway and run again never opens two. An account of the
  beta that buys in its window (30 days from the launch) gets a Stripe trial until the
  window ends (`billing.first_charge_at`): the card is taken now and the first year is
  charged then. Every other seat is charged at once.
- cancel and resume: `cancel_at_period_end` of that seat's subscription.
- pay: the hosted page of the oldest unpaid invoice, to unblock the account.
- portal: a session of the customer portal, to change the card.

A ready request always carries an https URL of Stripe (`*.stripe.com`), never one of
ours: the web opens nothing else. A failed one carries a `code` and a `detail` ready to
show, in Spanish. A temporary failure of Stripe is retried by the queue; when the
attempts run out, the request fails with `unavailable`.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit

from app.adapters.stripe.real import CHECKOUT_TTL_SECONDS
from app.domain import plans
from app.domain.billing import first_charge_at, seat_price, seats_counting_at
from app.domain.jobs import Job, JobError, PermanentJobError
from app.ports.billing_store import (
    CANCEL,
    FAILED,
    PAY,
    PENDING,
    PORTAL,
    READY,
    RESUME,
    SEAT,
    BillingProcessStore,
    BillingRequest,
    Customer,
    seats_in_force,
)
from app.ports.payments import (
    Address,
    CustomerDetails,
    Payments,
    PaymentsRejected,
    PaymentsUnavailable,
)

log = logging.getLogger("fichaitor.billing")

KIND = "billing_request"

# code -> detail, as the web shows them.
FAILURES = {
    "beta_no_charge": "Durante la beta no se cobra: activa la ficha en Pro desde «Tus negocios».",
    "profile_incomplete": "Antes de pagar, completa tus datos de facturación.",
    "location_not_found": "Esa ficha ya no está en tu cuenta.",
    "seat_not_found": "Esa plaza ya no se puede cambiar.",
    "nothing_to_pay": "No tienes ninguna factura pendiente.",
    "no_customer": "Todavía no tienes ningún pago con nosotros.",
    "stripe_rejected": "Stripe no ha aceptado la operación. Revisa tus datos de facturación.",
    "unavailable": "No hemos podido hablar con Stripe. Prueba otra vez en unos minutos.",
    "bad_url": "No hemos podido preparar el pago. Prueba otra vez en unos minutos.",
}


def is_stripe_url(url: str | None) -> bool:
    """An https URL of Stripe: stripe.com or one of its subdomains, without credentials
    or a port. The only kind a ready request carries."""
    if not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (
        parts.scheme == "https"
        and parts.port is None
        and parts.username is None
        and (host == "stripe.com" or host.endswith(".stripe.com"))
    )


def checkout_urls(public_base_url: str, location_id: str) -> tuple[str, str]:
    """Where Checkout comes back to: the panel, with only `pago` and `ficha`."""
    base = public_base_url.rstrip("/") + "/app?"
    return (
        base + urlencode({"pago": "ok", "ficha": location_id}),
        base + urlencode({"pago": "cancelado", "ficha": location_id}),
    )


def customer_details(customer: Customer) -> CustomerDetails:
    p = customer.profile
    return CustomerDetails(
        legal_name=p.legal_name,
        tax_id=p.tax_id,
        tax_id_type=p.tax_id_type,
        address=Address(
            line1=p.address_line1,
            line2=p.address_line2,
            postal_code=p.postal_code,
            city=p.city,
            state=p.province,
            country=p.country,
        ),
        invoice_email=p.invoice_email,
    )


def seat_id_for(request_id: str) -> str:
    """The seat a seat request creates: derived from the request, so a retry finds it."""
    return "seat_" + request_id.replace("-", "")[:24]


class Failed(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BillingRequests:
    def __init__(
        self,
        store: BillingProcessStore,
        payments: Payments,
        public_base_url: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._payments = payments
        self._base = public_base_url
        self._clock = clock

    async def __call__(self, job: Job) -> None:
        request_id = job.payload.get("request_id")
        if not isinstance(request_id, str):
            raise PermanentJobError("payload no válido: falta request_id")
        request = await self._store.get_request_by_id(request_id)
        if request is None or request.state != PENDING:
            return  # already resolved, or gone
        try:
            url, expires_at = await self._handle(request)
        except Failed as exc:
            await self._fail(request.id, exc.code)
            return
        except PaymentsUnavailable as exc:
            raise JobError(f"Stripe no disponible ({exc.status or 'red'})") from None
        except PaymentsRejected as exc:
            log.warning("petición de cobro rechazada por Stripe: %s", exc.code or exc.status)
            await self._fail(request.id, "stripe_rejected")
            return
        if url is not None and not is_stripe_url(url):
            log.error("URL de pago que no es de Stripe: se descarta")
            await self._fail(request.id, "bad_url")
            return
        await self._store.finish_request(request.id, READY, url=url, expires_at=expires_at)

    async def on_exhausted(self, job: Job) -> None:
        request_id = job.payload.get("request_id")
        if isinstance(request_id, str):
            await self._fail(request_id, "unavailable")

    async def _fail(self, request_id: str, code: str) -> None:
        await self._store.finish_request(request_id, FAILED, code=code, detail=FAILURES[code])

    async def _handle(self, request: BillingRequest) -> tuple[str | None, datetime | None]:
        if request.kind == SEAT:
            return await self._seat(request)
        if request.kind in (CANCEL, RESUME):
            return await self._cancel(request, cancel=request.kind == CANCEL), None
        customer = await self._store.customer(request.account_id)
        if customer is None or customer.stripe_customer_id is None:
            raise Failed("no_customer")
        if request.kind == PAY:
            unpaid = await self._payments.open_invoices(customer.stripe_customer_id)
            unpaid = [i for i in unpaid if i.hosted_invoice_url]
            if not unpaid:
                raise Failed("nothing_to_pay")
            oldest = min(unpaid, key=lambda i: i.period_start or datetime.max.replace(tzinfo=UTC))
            return oldest.hosted_invoice_url, None
        if request.kind == PORTAL:
            url = await self._payments.portal_url(
                customer.stripe_customer_id, self._base.rstrip("/") + "/app"
            )
            return url, None
        raise PermanentJobError(f"petición desconocida: {request.kind}")

    async def _seat(self, request: BillingRequest) -> tuple[str, datetime]:
        assert request.location_id is not None and request.plan is not None
        now = self._clock()
        launch_at = await self._store.launch_at()
        if not plans.charging(now, launch_at):
            raise Failed("beta_no_charge")
        customer = await self._store.customer(request.account_id)
        if customer is None:
            raise Failed("profile_incomplete")
        if not await self._store.location_owned(request.account_id, request.location_id):
            raise Failed("location_not_found")
        customer_id = await self._ensure_customer(customer)
        seat_id = seat_id_for(request.id)
        await self._store.create_seat(
            seat_id, request.account_id, request.plan, request.location_id, request.id
        )
        others = [
            s
            for s in seats_in_force(await self._store.seats_of(request.account_id), now)
            if s.seat_id != seat_id
        ]
        price_key = seat_price(request.plan, seat_id, seats_counting_at(others, now))
        success, cancel = checkout_urls(self._base, request.location_id)
        trial_end = first_charge_at(
            now, launch_at, beta_until=await self._store.beta_until(request.account_id)
        )
        session = await self._payments.create_checkout(
            customer_id,
            price_key,
            customer.profile.tax_treatment,
            request.account_id,
            seat_id,
            success,
            cancel,
            idempotency_key=f"billing_request:{request.id}:checkout",
            trial_end=trial_end,
        )
        await self._store.record_funnel("checkout_started", request.plan.value)
        return session.url, now + timedelta(seconds=CHECKOUT_TTL_SECONDS)

    async def _ensure_customer(self, customer: Customer) -> str:
        """The Stripe customer of the account, created once and kept up to date with the
        fiscal profile. Keyed by the time of the profile: a retry repeats the same call."""
        details = customer_details(customer)
        version = int(customer.updated_at.timestamp() * 1000)
        if customer.stripe_customer_id is None:
            created = await self._payments.create_customer(
                customer.account_id,
                details,
                idempotency_key=f"customer:{customer.account_id}:{version}",
            )
            await self._store.set_stripe_customer(customer.account_id, created)
            return created
        await self._payments.update_customer(
            customer.stripe_customer_id,
            details,
            idempotency_key=f"customer:{customer.stripe_customer_id}:{version}",
        )
        return customer.stripe_customer_id

    async def _cancel(self, request: BillingRequest, *, cancel: bool) -> None:
        seat = await self._store.seat(request.seat_id or "")
        if (
            seat is None
            or seat.account_id != request.account_id
            or seat.stripe_subscription_id is None
        ):
            raise Failed("seat_not_found")
        sub = await self._payments.set_cancel_at_period_end(
            seat.stripe_subscription_id, cancel, idempotency_key=f"billing_request:{request.id}"
        )
        await self._store.set_cancel_at_period_end(seat.id, sub.cancel_at_period_end)
