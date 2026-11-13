"""Defences of the backoffice at the HTTP level (docs/admin-facturacion.md, 6.1) and the
session of each request.

- Host: only 127.0.0.1:8090 or localhost:8090 (DNS rebinding). Anything else, 400.
- Headers: CSP without third parties (`default-src 'self'`, `frame-ancestors 'none'`),
  `Cache-Control: no-store` and the rest of the hardening, on every answer.
- No CORS at all. Every request that changes something carries
  `X-Requested-With: fichaitor-admin`, no foreign `Origin` nor `Sec-Fetch-Site`, and,
  once there is a session, `X-CSRF-Token` (derived from the cookie).
- Session cookie `__Host-fichaitor_admin`: Secure, HttpOnly, SameSite=Strict, Path=/,
  no Domain. Browsers treat http://localhost as a secure context and accept it.
"""

import hmac
import json
from dataclasses import dataclass
from datetime import datetime

from fastapi import Request, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.admin.auth import AuthKit, token_hash
from app.admin.store import AdminError, AdminStore

COOKIE = "__Host-fichaitor_admin"
COOKIE_MAX_AGE = 8 * 3600
CSRF_HEADER = "x-csrf-token"
REQUESTED_WITH = "fichaitor-admin"
SAFE_METHODS = frozenset({"GET", "HEAD"})

SECURITY_HEADERS = (
    (
        b"content-security-policy",
        b"default-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self';"
        b" connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none';"
        b" form-action 'self'; frame-ancestors 'none'",
    ),
    (b"cache-control", b"no-store"),
    (b"pragma", b"no-cache"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
)


def _error_body(code: str, message: str) -> bytes:
    return json.dumps({"code": code, "message": message}).encode()


class GuardMiddleware:
    """Pure ASGI: the Host check runs before any route (also before a 404) and the
    security headers go on every answer, errors included."""

    def __init__(self, app: ASGIApp, allowed_hosts: tuple[str, ...]) -> None:
        self.app = app
        self.allowed = frozenset(h.lower() for h in allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        hosts = [v for k, v in scope.get("headers", []) if k == b"host"]
        host = hosts[0].decode("latin-1").lower() if len(hosts) == 1 else ""

        async def send_secured(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in {name for name, _ in SECURITY_HEADERS}
                    and k.lower() != b"server"
                ]
                message["headers"] = headers + list(SECURITY_HEADERS)
            await send(message)

        if host not in self.allowed:
            if scope["type"] != "http":
                await send({"type": "websocket.close", "code": 1008})
                return
            body = _error_body("host_not_allowed", "Entra por el túnel: http://localhost:8090")
            await send_secured(
                {
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send_secured({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send_secured)


def allowed_origins(allowed_hosts: tuple[str, ...]) -> frozenset[str]:
    return frozenset(f"http://{h.lower()}" for h in allowed_hosts) | frozenset(
        f"https://{h.lower()}" for h in allowed_hosts
    )


def check_unsafe_request(request: Request, origins: frozenset[str]) -> None:
    """The CSRF defence that needs no session: every request that changes something says
    it comes from the backoffice's own page, and nothing says it comes from elsewhere."""
    if request.method in SAFE_METHODS:
        return
    if request.headers.get("x-requested-with") != REQUESTED_WITH:
        raise AdminError(403, "csrf_failed", "Falta X-Requested-With: fichaitor-admin.")
    origin = request.headers.get("origin")
    if origin is not None and origin.lower() not in origins:
        raise AdminError(403, "csrf_failed", "Origen no permitido.")
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site not in ("same-origin", "none"):
        raise AdminError(403, "csrf_failed", "Petición de otro sitio.")


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE,
        token,
        max_age=COOKIE_MAX_AGE,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")


@dataclass(frozen=True)
class RawSession:
    """The cookie of a request: its token and the hash the database knows."""

    token: str
    hash: str


@dataclass(frozen=True)
class Actor:
    """A person with a valid session (password and TOTP confirmed)."""

    token: str
    hash: str
    user_id: str
    username: str
    role: str
    totp_verified_at: datetime | None


def raw_session(request: Request, kit: AuthKit) -> RawSession:
    """The cookie, with the CSRF token checked on requests that change something. Any
    state of the session (waiting for the TOTP, re-enrolling the TOTP or valid)."""
    token = request.cookies.get(COOKIE)
    if not token or len(token) > 200:
        raise AdminError(401, "session_invalid", "Entra con tu usuario y contraseña.")
    if request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not hmac.compare_digest(sent.encode(), kit.csrf(token).encode()):
            raise AdminError(403, "csrf_failed", "Falta el token CSRF o no vale.")
    return RawSession(token, token_hash(token))


async def current_actor(request: Request, kit: AuthKit, store: AdminStore) -> Actor:
    """A valid session: the database checks it (admin_session_check) and notes the
    activity. 401 otherwise. Kept in request.state for the audit of a refusal."""
    raw = raw_session(request, kit)
    row = await store.session_check(raw.hash)
    if row is None:
        raise AdminError(401, "session_invalid", "La sesión no vale: vuelve a entrar.")
    actor = Actor(
        raw.token,
        raw.hash,
        row["admin_user_id"],
        row["username"],
        row["role"],
        row["totp_verified_at"],
    )
    request.state.actor = actor
    return actor
