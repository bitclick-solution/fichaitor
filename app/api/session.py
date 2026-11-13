"""Session cookie, the two guards of the routes behind a session and the profile a
request works on (`?location=`, docs/multinegocio.md, 5.1)."""

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from fastapi import HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from app.domain.plans import FEATURE_AI_REPLIES, FEATURE_KEY_DATES, PlanRequired
from app.domain.session import SESSION_TTL_SECONDS, hash_token
from app.ports.store import (
    Account,
    LocationInactive,
    LocationNotFound,
    NoLocationChosen,
    Store,
)

# The name without its prefix: the one used over plain http in local development.
SESSION_COOKIE = "fichaitor_session"
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "fichaitor"


def session_cookie_name(secure: bool) -> str:
    """With Secure, the name carries __Host-: the browser only accepts it from this very
    host, over https, with Path=/ and without Domain, so no other site under the same
    domain can set it. Browsers refuse the prefix without Secure, so plain http (local
    development, FICHAITOR_COOKIE_SECURE=false) uses the bare name."""
    return f"__Host-{SESSION_COOKIE}" if secure else SESSION_COOKIE


def utc(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, UTC)


def set_session_cookie(response: Response, token: str, secure: bool) -> None:
    # Lax: the cookie is set on the redirect that comes back from Google.
    response.set_cookie(
        session_cookie_name(secure),
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, secure: bool) -> None:
    response.delete_cookie(
        session_cookie_name(secure), path="/", httponly=True, secure=secure, samesite="lax"
    )


async def require_csrf_header(request: Request) -> None:
    """A cross-site form cannot set a custom header, so its presence proves same origin."""
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(403, "Petición no permitida.")


def account_guard(
    store: Store, clock: Callable[[], float], cookie_secure: bool = True
) -> Callable[[Request], Awaitable[Account]]:
    # Only the cookie of the configured name: with Secure, one without the prefix
    # (which another site under the domain could have set) is not even read.
    cookie_name = session_cookie_name(cookie_secure)

    async def current_account(request: Request) -> Account:
        token = request.cookies.get(cookie_name)
        account = None
        if token:
            account = await store.account_for_session(hash_token(token), utc(clock()))
        if account is None:
            raise HTTPException(401, "No has iniciado sesión.")
        return account

    return current_account


MSG_LEAVE_PENDING = "Tienes una baja programada. Restáurala para seguir usando Fichaitor."

# docs/admin-facturacion.md, 3.6: a payment not made blocks the whole account, with no
# time limit. Every data route answers 402 with this code; GET /me, the billing routes,
# leaving, logout, unlinking chats and deactivating profiles still answer.
CODE_ACCOUNT_BLOCKED = "account_blocked"
MSG_ACCOUNT_BLOCKED = (
    "Tu cuenta está en pausa porque no hemos podido cobrar. Paga la factura pendiente o cambia la "
    "tarjeta en Ajustes > Facturación para seguir usando Fichaitor."
)


class AccountBlocked(Exception):
    """Raised by active_account_guard; create_app answers it with 402 account_blocked."""


def account_blocked_response() -> JSONResponse:
    return JSONResponse(
        {"detail": MSG_ACCOUNT_BLOCKED, "code": CODE_ACCOUNT_BLOCKED}, status_code=402
    )


def active_account_guard(
    store: Store, clock: Callable[[], float], cookie_secure: bool = True
) -> Callable[[Request], Awaitable[Account]]:
    """The account of the session, unless it has a scheduled leave: from the moment a
    leave is confirmed, nothing of the profile is read or shown until it is restored.
    Nor while it is blocked for a payment not made (AccountBlocked: 402)."""
    current_account = account_guard(store, clock, cookie_secure)

    async def active_account(request: Request) -> Account:
        account = await current_account(request)
        if account.pending_deletion is not None:
            raise HTTPException(409, MSG_LEAVE_PENDING)
        if account.blocked is not None:
            raise AccountBlocked
        return account

    return active_account


# --- which profile: ?location= (docs/multinegocio.md, 5.1) ------------------------

# Same shape as the check of location.id in db/schema.sql.
LOCATION_ID = re.compile(r"loc_[0-9a-z]{4,32}")
MSG_NO_LOCATION = "Primero elige con qué ficha quieres trabajar."
MSG_LOCATION_NOT_FOUND = "Esa ficha no está en tu cuenta."
MSG_LOCATION_INACTIVE = "Esa ficha no está activa."


def location_id_or_404(location_id: str) -> str:
    """A profile id from the request. One of another shape is not even looked up: it
    gets the same 404 as one of another account."""
    if not LOCATION_ID.fullmatch(location_id):
        raise HTTPException(404, MSG_LOCATION_NOT_FOUND)
    return location_id


async def location_param(
    location: str | None = Query(None, max_length=64),  # noqa: B008
) -> str | None:
    """`?location=loc_...`: the profile a read works on. None: the preference of the
    panel (selected_location_id), so that a web that does not send it keeps working."""
    return None if location is None else location_id_or_404(location)


async def resolve_location(store: Store, account: Account, location_id: str | None) -> str:
    """The active profile of that account the request works on, looked up with the
    account of the session. Answers 409 without a profile and without a preference, the
    same 404 for a profile of another account and for one that does not exist, and 409
    for an inactive one."""
    try:
        return await store.resolve_location(account.id, location_id)
    except NoLocationChosen:
        raise HTTPException(409, MSG_NO_LOCATION) from None
    except LocationNotFound:
        raise HTTPException(404, MSG_LOCATION_NOT_FOUND) from None
    except LocationInactive:
        raise HTTPException(409, MSG_LOCATION_INACTIVE) from None


# --- what the plan of a profile does not include (app/domain/plans.py) --------------

CODE_PLAN_REQUIRED = "plan_required"
MSG_KEY_DATES_PRO = (
    "Los festivos y las fechas clave están en Pro. Pásate a Pro para que te preguntemos "
    "por ellos y cambiemos el horario en Google."
)


def plan_required_detail(exc: PlanRequired) -> str:
    if exc.feature == FEATURE_AI_REPLIES:
        return (
            f"Has usado tus {exc.limit} respuestas con IA gratuitas. Pásate a Pro para "
            "tenerlas ilimitadas. Puedes seguir escribiendo tus respuestas."
        )
    if exc.feature == FEATURE_KEY_DATES:
        return MSG_KEY_DATES_PRO
    return "Tu plan no incluye esta función. Pásate a Pro o a Pro+."


def plan_required(exc: PlanRequired) -> JSONResponse:
    """402 with `code: plan_required` and the `feature` the plan lacks, and a `detail`
    ready to show (docs/contrato-web-api.md, «Límites del plan»)."""
    return JSONResponse(
        {
            "detail": plan_required_detail(exc),
            "code": CODE_PLAN_REQUIRED,
            "feature": exc.feature,
        },
        status_code=402,
    )
