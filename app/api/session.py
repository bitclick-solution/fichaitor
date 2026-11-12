"""Session cookie and the two guards of the routes behind a session."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from fastapi import HTTPException, Request, Response

from app.domain.session import SESSION_TTL_SECONDS, hash_token
from app.ports.store import Account, Store

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


def active_account_guard(
    store: Store, clock: Callable[[], float], cookie_secure: bool = True
) -> Callable[[Request], Awaitable[Account]]:
    """The account of the session, unless it has a scheduled leave: from the moment a
    leave is confirmed, nothing of the profile is read or shown until it is restored."""
    current_account = account_guard(store, clock, cookie_secure)

    async def active_account(request: Request) -> Account:
        account = await current_account(request)
        if account.pending_deletion is not None:
            raise HTTPException(409, MSG_LEAVE_PENDING)
        return account

    return active_account
