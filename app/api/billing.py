"""The paywall: fiscal profile, seats, the requests to the billing process and the
invoices (docs/admin-facturacion.md, 4.7; docs/contrato-web-api.md, «Cobro y paywall»).

The API never calls Stripe: each action writes a `billing_request` (and its job) and
answers 202 with its id; the web polls GET /me/billing/requests/{id} until it is `lista`
(with an https URL of Stripe to open) or `fallida` (with `code` and `detail`).

Every route works with the account blocked: paying is the way out of the block. Writes
need the header X-Requested-With and count against a cap per account.
"""

import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.session import (
    CODE_ACCOUNT_BLOCKED,
    MSG_LEAVE_PENDING,
    MSG_LOCATION_NOT_FOUND,
    account_guard,
    location_id_or_404,
    require_csrf_header,
)
from app.domain import plans
from app.domain.billing import (
    RENEWAL_NOTICE_DAYS,
    BillingInputError,
    SeatStatus,
    new_seat_prices,
    price_cents,
    seat_price,
    seats_counting_at,
    tax_treatment,
)
from app.domain.plans import Plan
from app.ports.billing_store import (
    CANCEL,
    PAY,
    PORTAL,
    RESUME,
    AlreadyActive,
    BillingStore,
    FiscalProfile,
    SeatPending,
    SeatRow,
    seats_in_force,
)
from app.ports.quota import Quota
from app.ports.store import Account, LocationNotFound, OffboardingPending

log = logging.getLogger("fichaitor.billing.api")

PREFIX = "/api/v1/me/billing"
# Writes per account and hour: a person never needs more; a script gets a 429.
WRITE_LIMIT = 30
WRITE_WINDOW_SECONDS = 3600
SEAT_ID = re.compile(r"seat_[0-9a-f]{8,32}")
_NOTICE = timedelta(days=RENEWAL_NOTICE_DAYS)

# What the plan is called on screen: pro_whatsapp is Pro+.
PLAN_NAMES = {Plan.PRO: "Pro", Plan.PRO_WHATSAPP: "Pro+"}

MSG_PROFILE_INVALID = {
    "tax_id_invalid": "Ese NIF no es válido. Revisa las letras y los números.",
    "country_not_supported": "De momento solo facturamos a empresas de España y de la UE.",
    "postal_code_invalid": "Ese código postal no es válido.",
}
MSG_BETA_NO_CHARGE = (
    "Durante la beta no se cobra. Activa la ficha en Pro desde «Tus negocios»: usa una de "
    "las fichas Pro de la beta."
)
MSG_PROFILE_INCOMPLETE = "Antes de pagar, completa tus datos de facturación."
MSG_SEAT_PENDING = (
    "Ya hay un pago en marcha para esta ficha. Termínalo, o espera unos minutos si acabas de "
    "pagar: la ficha se activa sola."
)
MSG_ALREADY_ACTIVE = "Esta ficha ya está activa en ese plan."
MSG_ACCOUNT_BLOCKED_BUY = "Antes de comprar otra plaza, paga la factura pendiente."
MSG_RATE = "Has hecho muchas operaciones de cobro seguidas. Prueba dentro de un rato."
MSG_SEAT_NOT_FOUND = "Esa plaza no está en tu cuenta."
MSG_REQUEST_NOT_FOUND = "Esa petición no está en tu cuenta."


# --- the billing block of GET /me ---------------------------------------------------


class BlockedOut(BaseModel):
    since: datetime
    reason: str  # payment_failed, action_required, dispute or manual_unpaid


class SeatOut(BaseModel):
    id: str
    plan: str  # pro or pro_whatsapp (Pro+)
    location_id: str | None  # None: a paid seat without a profile, free to assign
    status: str  # active, payment_failed, action_required, disputed or canceled
    renews_at: date | None  # the day the next year is charged (or the seat ends)
    # A seat bought in the beta window, before its first charge: when it is charged (the
    # same day as renews_at). None for any other seat, and once charged.
    trial_ends_at: datetime | None = None
    cancel_at_period_end: bool
    next_price: int | None  # cents, without VAT, of the next year; None if it ends
    payment_method: str  # card: the only one at the launch
    collection: str  # auto


class NewSeatPriceOut(BaseModel):
    pro: int
    pro_whatsapp: int


class BillingOut(BaseModel):
    profile_complete: bool
    blocked: BlockedOut | None
    seats: list[SeatOut]
    new_seat_price: NewSeatPriceOut  # cents, without VAT, a whole year, bought today


def _next_price(seat: SeatRow, seats: list[SeatRow], now: datetime) -> int | None:
    if seat.cancel_at_period_end or seat.location_id is None or seat.current_period_end is None:
        return None
    if seat.next_price_key is not None and seat.current_period_end - now <= _NOTICE:
        return price_cents(seat.next_price_key)  # frozen at the notice of T-30
    at = seat.current_period_end
    counting = seats_counting_at(seats_in_force(seats, now), at)
    return price_cents(seat_price(seat.plan, seat.id, counting))


def _shown(seat: SeatRow, now: datetime) -> bool:
    if seat.status is SeatStatus.PENDING:
        return False  # a Checkout not paid: no seat yet
    if seat.status is SeatStatus.CANCELED:
        return seat.current_period_end is not None and seat.current_period_end > now
    return True


async def billing_body(billing: BillingStore, account: Account, now: datetime) -> BillingOut:
    seats = await billing.seats(account.id)
    profile = await billing.profile(account.id)
    in_force = seats_in_force(seats, now)
    return BillingOut(
        profile_complete=profile is not None,
        blocked=(
            BlockedOut(since=account.blocked.since, reason=account.blocked.reason)
            if account.blocked is not None
            else None
        ),
        seats=[
            SeatOut(
                id=s.id,
                plan=s.plan.value,
                location_id=s.location_id,
                status=s.status.value,
                renews_at=s.current_period_end.date() if s.current_period_end else None,
                trial_ends_at=s.trial_ends_at if s.in_trial else None,
                cancel_at_period_end=s.cancel_at_period_end,
                next_price=_next_price(s, seats, now),
                payment_method="card",
                collection=s.collection,
            )
            for s in seats
            if _shown(s, now)
        ],
        new_seat_price=NewSeatPriceOut.model_validate(new_seat_prices(in_force)),
    )


# --- the 402 without a seat ---------------------------------------------------------


def euros(cents: int) -> str:
    return f"{cents // 100},{cents % 100:02d}"


def no_seat(plan: Plan, price: int, charging: bool) -> JSONResponse:
    """The 402 of an activation without room in the plan: the paywall. While nothing is
    charged (beta), the same code with `charging: false`: there is nothing to buy."""
    name = PLAN_NAMES[plan]
    if charging:
        detail = (
            f"Esta ficha necesita una plaza {name}: {euros(price)} € + IVA por un año. "
            "Se renueva sola salvo que la canceles. Pásate a Pro o a Pro+ para activarla."
        )
    else:
        detail = (
            f"Ya usas todas las fichas {name} de la beta. Desactiva otra ficha para activar "
            "esta, o actívala en el plan Gratis si lo tienes libre."
        )
    return JSONResponse(
        {
            "detail": detail,
            "code": "no_seat",
            "offer": {"plan": plan.value, "price": price},
            "charging": charging,
        },
        status_code=402,
    )


# --- the routes ---------------------------------------------------------------------


class AddressIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line1: str = Field(min_length=1, max_length=200)
    line2: str | None = Field(default=None, max_length=200)
    postal_code: str = Field(min_length=1, max_length=20)
    city: str = Field(min_length=1, max_length=100)
    province: str | None = Field(default=None, max_length=100)
    country: str = Field(pattern=r"^[A-Za-z]{2}$")  # ISO 3166-1 alpha-2


class ProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    legal_name: str = Field(min_length=1, max_length=200)
    tax_id: str = Field(min_length=4, max_length=30)
    address: AddressIn
    invoice_email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AddressOut(BaseModel):
    line1: str | None = None
    line2: str | None = None
    postal_code: str | None = None
    city: str | None = None
    province: str | None = None
    country: str | None = None


class ProfileOut(BaseModel):
    legal_name: str | None = None
    tax_id: str | None = None
    address: AddressOut = AddressOut()
    invoice_email: str | None = None
    # iva_21, no_sujeto (Canarias, Ceuta, Melilla) or isp_ue; None without a profile.
    tax_treatment: str | None = None
    complete: bool = False


class SeatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: Literal["pro", "pro_whatsapp"]
    location_id: str = Field(min_length=1, max_length=64)


class RequestCreated(BaseModel):
    request_id: str


class RequestOut(BaseModel):
    state: str  # pendiente, lista or fallida
    url: str | None = None  # lista (except cancel and resume): an https URL of Stripe
    code: str | None = None  # fallida
    detail: str | None = None  # fallida: ready to show


class InvoiceOut(BaseModel):
    id: str
    kind: str  # factura or rectificativa
    number: str | None
    amount_total: int | None  # cents, VAT included; negative for a rectificativa
    issued_at: datetime | None


class InvoicesOut(BaseModel):
    invoices: list[InvoiceOut]


def signed_total(kind: str, amount_total: int | None) -> int | None:
    """Cents with VAT; negative for a credit note (rectificativa)."""
    if amount_total is None:
        return None
    return -abs(amount_total) if kind == "rectificativa" else amount_total


def pdf_name(number: str | None) -> str:
    """The file name of a PDF: the number of Odoo with "/" as "_", and only
    [A-Za-z0-9_-] (FTR/2027/00001 -> FTR_2027_00001.pdf)."""
    clean = re.sub(r"[^A-Za-z0-9_-]", "", (number or "").replace("/", "_"))[:60]
    return f"{clean or 'factura'}.pdf"


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = " ".join(value.split())
    return text or None


def _profile_out(profile: FiscalProfile | None) -> ProfileOut:
    if profile is None:
        return ProfileOut()
    return ProfileOut(
        legal_name=profile.legal_name,
        tax_id=profile.tax_id,
        address=AddressOut(
            line1=profile.address_line1,
            line2=profile.address_line2,
            postal_code=profile.postal_code,
            city=profile.city,
            province=profile.province,
            country=profile.country,
        ),
        invoice_email=profile.invoice_email,
        tax_treatment=profile.tax_treatment.value,
        complete=True,
    )


def _error(status: int, code: str, detail: str, **extra) -> JSONResponse:
    return JSONResponse({"detail": detail, "code": code, **extra}, status_code=status)


def build_router(
    *,
    store,
    billing: BillingStore,
    quota: Quota,
    clock: Callable[[], float],
    cookie_secure: bool = True,
) -> APIRouter:
    router = APIRouter()
    current_account = account_guard(store, clock, cookie_secure)

    def now() -> datetime:
        return datetime.fromtimestamp(clock(), UTC)

    async def within_limit(account: Account) -> bool:
        try:
            return await quota.take_quota(
                f"billing_write:{account.id}", WRITE_LIMIT, WRITE_WINDOW_SECONDS, now()
            )
        except Exception as exc:  # no Stripe call without a working cap
            log.warning("tope de cobro no disponible: %s", type(exc).__name__)
            raise HTTPException(503, "Ahora no podemos atenderte. Prueba en un minuto.") from None

    writes = [Depends(require_csrf_header)]

    @router.get(f"{PREFIX}/profile", response_model=ProfileOut)
    async def get_profile(account: Account = Depends(current_account)) -> ProfileOut:  # noqa: B008
        return _profile_out(await billing.profile(account.id))

    @router.put(f"{PREFIX}/profile", response_model=ProfileOut, dependencies=writes)
    async def put_profile(
        body: ProfileIn,
        account: Account = Depends(current_account),  # noqa: B008
    ) -> ProfileOut | JSONResponse:
        if not await within_limit(account):
            return _error(429, "rate_limited", MSG_RATE)
        address = body.address
        country = address.country.upper()
        try:
            tax = tax_treatment(country, address.postal_code, body.tax_id)
        except BillingInputError as exc:
            return _error(422, exc.code, MSG_PROFILE_INVALID[exc.code])
        profile = FiscalProfile(
            legal_name=_clean(body.legal_name) or body.legal_name,
            tax_id=tax.tax_id,
            tax_id_type=tax.stripe_tax_id_type,
            tax_id_kind=tax.tax_id_kind.value,
            address_line1=_clean(address.line1) or address.line1,
            address_line2=_clean(address.line2),
            postal_code=address.postal_code.strip(),
            city=_clean(address.city) or address.city,
            province=_clean(address.province),
            country=country,
            territory=tax.territory,
            tax_treatment=tax.treatment,
            invoice_email=body.invoice_email.strip(),
        )
        await billing.save_profile(account.id, profile)
        return _profile_out(profile)

    @router.post(
        f"{PREFIX}/seats", status_code=202, response_model=RequestCreated, dependencies=writes
    )
    async def buy_seat(
        body: SeatIn,
        account: Account = Depends(current_account),  # noqa: B008
    ) -> RequestCreated | JSONResponse:
        if account.pending_deletion is not None:
            return _error(409, OffboardingPending.code, MSG_LEAVE_PENDING)
        if account.blocked is not None:
            return _error(409, CODE_ACCOUNT_BLOCKED, MSG_ACCOUNT_BLOCKED_BUY)
        if not plans.charging(now(), await billing.launch_at()):
            return _error(409, "beta_no_charge", MSG_BETA_NO_CHARGE)
        location_id = location_id_or_404(body.location_id)
        if await billing.profile(account.id) is None:
            return _error(409, "profile_incomplete", MSG_PROFILE_INCOMPLETE)
        if not await within_limit(account):
            return _error(429, "rate_limited", MSG_RATE)
        try:
            request_id = await billing.request_seat(account.id, location_id, Plan(body.plan))
        except LocationNotFound:
            raise HTTPException(404, MSG_LOCATION_NOT_FOUND) from None
        except AlreadyActive:
            return _error(409, "already_active", MSG_ALREADY_ACTIVE)
        except SeatPending as exc:
            return _error(409, "seat_pending", MSG_SEAT_PENDING, request_id=exc.request_id)
        return RequestCreated(request_id=request_id)

    async def seat_request(
        account: Account, seat_id: str, kind: str
    ) -> RequestCreated | JSONResponse:
        if not SEAT_ID.fullmatch(seat_id):
            raise HTTPException(404, MSG_SEAT_NOT_FOUND)
        if not await within_limit(account):
            return _error(429, "rate_limited", MSG_RATE)
        request_id = await billing.request_for_seat(account.id, kind, seat_id)
        if request_id is None:
            raise HTTPException(404, MSG_SEAT_NOT_FOUND)
        return RequestCreated(request_id=request_id)

    @router.post(
        PREFIX + "/seats/{seat_id}/cancel",
        status_code=202,
        response_model=RequestCreated,
        dependencies=writes,
    )
    async def cancel_seat(
        seat_id: str,
        account: Account = Depends(current_account),  # noqa: B008
    ) -> RequestCreated | JSONResponse:
        return await seat_request(account, seat_id, CANCEL)

    @router.post(
        PREFIX + "/seats/{seat_id}/resume",
        status_code=202,
        response_model=RequestCreated,
        dependencies=writes,
    )
    async def resume_seat(
        seat_id: str,
        account: Account = Depends(current_account),  # noqa: B008
    ) -> RequestCreated | JSONResponse:
        return await seat_request(account, seat_id, RESUME)

    async def simple_request(account: Account, kind: str) -> RequestCreated | JSONResponse:
        if not await within_limit(account):
            return _error(429, "rate_limited", MSG_RATE)
        return RequestCreated(request_id=await billing.request(account.id, kind))

    @router.post(
        f"{PREFIX}/pay", status_code=202, response_model=RequestCreated, dependencies=writes
    )
    async def pay(account: Account = Depends(current_account)) -> RequestCreated | JSONResponse:  # noqa: B008
        return await simple_request(account, PAY)

    @router.post(
        f"{PREFIX}/portal", status_code=202, response_model=RequestCreated, dependencies=writes
    )
    async def portal(account: Account = Depends(current_account)) -> RequestCreated | JSONResponse:  # noqa: B008
        return await simple_request(account, PORTAL)

    @router.get(PREFIX + "/requests/{request_id}", response_model=RequestOut)
    async def get_request(
        request_id: str,
        account: Account = Depends(current_account),  # noqa: B008
    ) -> RequestOut:
        found = await billing.get_request(account.id, request_id[:64])
        if found is None:
            raise HTTPException(404, MSG_REQUEST_NOT_FOUND)
        return RequestOut(state=found.state, url=found.url, code=found.code, detail=found.detail)

    @router.get(f"{PREFIX}/invoices", response_model=InvoicesOut)
    async def invoices(account: Account = Depends(current_account)) -> InvoicesOut:  # noqa: B008
        # The legal invoices and credit notes of Odoo, once issued (step 9). A credit
        # note's total is negative: it gives back.
        return InvoicesOut(
            invoices=[
                InvoiceOut(
                    id=d.id,
                    kind=d.kind,
                    number=d.number,
                    amount_total=signed_total(d.kind, d.amount_total),
                    issued_at=d.issued_at,
                )
                for d in await billing.invoices(account.id)
            ]
        )

    @router.get(PREFIX + "/invoices/{document_id}/pdf")
    async def invoice_pdf(
        document_id: str,
        account: Account = Depends(current_account),  # noqa: B008
    ) -> Response:
        pdf = await billing.invoice_pdf(account.id, document_id[:64])
        if pdf is None:
            raise HTTPException(404, "Esa factura no está en tu cuenta.")
        number = next(
            (d.number for d in await billing.invoices(account.id) if d.id == document_id), None
        )
        name = pdf_name(number)
        return Response(
            pdf,
            media_type="application/pdf",
            headers={
                "Content-Disposition": (
                    f"attachment; filename=\"{name}\"; filename*=UTF-8''{name}"
                ),
                "Cache-Control": "private, no-store",
            },
        )

    return router
