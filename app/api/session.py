"""Session cookie and the two guards of the routes behind a session."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from fastapi import HTTPException, Request, Response

from app.domain.session import SESSION_TTL_SECONDS, hash_token
from app.ports.store import Account, Store

SESSION_COOKIE = "fichaitor_session"
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "fichaitor"


def utc(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, UTC)


def set_session_cookie(response: Response, token: str, secure: bool) -> None:
    # Lax: the cookie is set on the redirect that comes back from Google.
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, secure: bool) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=secure, samesite="lax")


async def require_csrf_header(request: Request) -> None:
    """A cross-site form cannot set a custom header, so its presence proves same origin."""
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(403, "Petición no permitida.")


def account_guard(
    store: Store, clock: Callable[[], float]
) -> Callable[[Request], Awaitable[Account]]:
    async def current_account(request: Request) -> Account:
        token = request.cookies.get(SESSION_COOKIE)
        account = None
        if token:
            account = await store.account_for_session(hash_token(token), utc(clock()))
        if account is None:
            raise HTTPException(401, "No has iniciado sesión.")
        return account

    return current_account
