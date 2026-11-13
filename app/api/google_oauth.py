import logging
import time
from collections.abc import Callable
from dataclasses import asdict
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.adapters.crypto.sealed import seal
from app.api.session import session_cookie_name, set_session_cookie, utc
from app.application.profile_sync import ProfileSync
from app.application.rate_limit import SlidingWindowLimiter
from app.domain.oauth_state import InvalidState, make_state, verify_state
from app.domain.session import SESSION_TTL_SECONDS, hash_token, new_token
from app.ports.google_oauth import BUSINESS_SCOPE, GoogleOAuth, GoogleOAuthError
from app.ports.store import GoogleConnection, Store

log = logging.getLogger("fichaitor.google_oauth")

# The name without its prefix: the one used over plain http in local development.
STATE_COOKIE = "fichaitor_oauth_state"
COOKIE_PATH = "/api/v1/google/oauth"
STATE_MAX_AGE = 600


def state_cookie_name(secure: bool) -> str:
    """With Secure, the name carries __Secure-: a site under the same domain without
    https cannot set it. Not __Host-: that prefix needs Path=/, and this cookie only
    travels to the OAuth routes. Browsers refuse the prefix without Secure."""
    return f"__Secure-{STATE_COOKIE}" if secure else STATE_COOKIE


def build_router(
    *,
    google: GoogleOAuth,
    store: Store,
    state_secret: str,
    token_public_key: str,
    token_key_version: int,
    post_connect_url: str,
    cookie_secure: bool,
    limiter: SlidingWindowLimiter,
    client_ip: Callable[[Request], str],
    clock: Callable[[], float] = time.time,
    sync: ProfileSync | None = None,
) -> APIRouter:
    router = APIRouter()
    state_cookie = state_cookie_name(cookie_secure)
    session_cookie = session_cookie_name(cookie_secure)

    def back(result: str, **extra: str | int) -> RedirectResponse:
        sep = "&" if "?" in post_connect_url else "?"
        url = f"{post_connect_url}{sep}{urlencode({'google': result, **extra})}"
        resp = RedirectResponse(url, status_code=303)
        resp.delete_cookie(state_cookie, path=COOKIE_PATH, secure=cookie_secure)
        return resp

    @router.get("/api/v1/google/oauth/start")
    async def start(request: Request) -> RedirectResponse:
        if not limiter.allow(client_ip(request)):
            raise HTTPException(429, "Demasiados intentos. Prueba dentro de un rato.")
        state = make_state(state_secret, clock())
        resp = RedirectResponse(google.authorization_url(state), status_code=302)
        # Lax: the cookie must travel on Google's top-level redirect back to us.
        resp.set_cookie(
            state_cookie,
            state,
            max_age=STATE_MAX_AGE,
            httponly=True,
            secure=cookie_secure,
            samesite="lax",
            path=COOKIE_PATH,
        )
        return resp

    @router.get("/api/v1/google/oauth/callback")
    async def callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> RedirectResponse:
        try:
            verify_state(state_secret, state or "", request.cookies.get(state_cookie), clock())
        except InvalidState as exc:
            raise HTTPException(400, "La conexión ha caducado. Vuelve a empezar.") from exc
        if error or not code:
            return back("cancelado")
        try:
            tokens = await google.exchange(code)
        except GoogleOAuthError as exc:
            log.warning("intercambio de código fallido: %s", exc)
            return back("error")
        if BUSINESS_SCOPE not in tokens.scopes:
            return back("sin_permiso")
        if not tokens.refresh_token:
            log.warning("Google no devolvió token de renovación")
            return back("error")
        try:
            identity = await google.identity(tokens.access_token)
        except GoogleOAuthError as exc:
            log.warning("identidad no disponible: %s", exc)
            return back("error")
        try:
            locations = await google.locations(tokens.access_token)
        except GoogleOAuthError as exc:
            # The connection is still valid; the worker can list the profiles later.
            log.warning("fichas no disponibles al conectar: %s", exc)
            locations = None
        try:
            await store.add_google_connection(
                GoogleConnection(
                    google_sub=identity.sub,
                    email=identity.email,
                    refresh_token_enc=seal(token_public_key, tokens.refresh_token),
                    key_version=token_key_version,
                    scopes=tokens.scopes,
                    locations=tuple(asdict(loc) for loc in locations or []),
                )
            )
        except Exception as exc:  # tokens and emails must never reach the logs
            log.error("no se pudo guardar la conexión: %s", type(exc).__name__)
            return back("error")
        # The account is the Google identity: connecting is also signing in.
        token = new_token()
        try:
            now = clock()
            # The funnel counts the first connection of an identity, not every sign-in.
            first_time = not await store.account_exists(identity.sub)
            account_id = await store.upsert_account(identity.sub, identity.email)
            if locations is not None:  # not listed: keep whatever the account already had
                await store.sync_locations(account_id, locations, utc(now))
            previous = request.cookies.get(session_cookie)
            if previous:
                await store.revoke_session(hash_token(previous), utc(now))
            await store.create_session(
                account_id, hash_token(token), utc(now), utc(now + SESSION_TTL_SECONDS)
            )
        except Exception as exc:  # tokens and emails must never reach the logs
            log.error("no se pudo abrir la sesión: %s", type(exc).__name__)
            return back("error")
        # A reconnection with a profile already chosen: its figures are read again now,
        # without waiting for the sync window. Never fails the connection. Skipped when
        # the profiles could not be listed: that connection has none the worker can read.
        # (A scheduled leave has no selected profile to read: nothing is enqueued.)
        if sync is not None and locations is not None:
            try:
                await sync.request_for_account(account_id, reconnected=True)
            except Exception as exc:  # identifiers must never reach the logs
                log.warning("sincronización tras conectar no encolada: %s", type(exc).__name__)
        if first_time:
            try:
                await store.record_event("connected")
            except Exception as exc:
                log.warning("evento de embudo no registrado: %s", type(exc).__name__)
        # "pendiente": connected, but Google did not let us list the profiles.
        # It does not mean the owner has none.
        resp = back("ok", fichas="pendiente" if locations is None else len(locations))
        set_session_cookie(resp, token, cookie_secure)
        return resp

    return router
