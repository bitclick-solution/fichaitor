"""Opting out of the commercial communications, without a session (docs/contrato-web-api.md,
«Baja de las comunicaciones comerciales»).

Two ways in, both with the token signed for one account (app/domain/unsubscribe.py):

- POST /api/v1/public/unsubscribe {"token"}: the page fichaitor.es/baja-comunicaciones#<t>,
  which reads the token from the fragment and sends it in the body.
- POST /api/v1/public/unsubscribe/one-click?token=<t>: the URL of List-Unsubscribe. The
  mail client POSTs `List-Unsubscribe=One-Click` (RFC 8058) with no cookie and no header
  of ours, so neither a session nor the CSRF header is asked for. A GET does nothing
  (405): link scanners must not opt anyone out.

Both are idempotent and answer 204 whether the token is valid, already used, of an
account that left, or not a token at all: the answer says nothing about any account. A
limit per client address, larger for the one-click route (mail providers call it from a
few addresses). Nothing of the token is logged.
"""

import logging
from collections.abc import Callable
from typing import Protocol

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel

from app.application.rate_limit import SlidingWindowLimiter
from app.domain import unsubscribe

log = logging.getLogger("fichaitor.unsubscribe")

MSG_RATE_LIMITED = "Demasiados intentos. Prueba dentro de un rato."
MSG_UNAVAILABLE = "Ahora mismo no podemos hacerlo. Prueba otra vez en un minuto."
SOURCE_PAGE = "enlace"
SOURCE_ONE_CLICK = "un_clic"


class OptOutStore(Protocol):
    async def unsubscribe_communications(self, account_id: str, source: str) -> None:
        """Idempotent: the account stops getting commercial communications."""
        ...


class UnsubscribeRequest(BaseModel):
    # No bounds here: a token that is empty or too long gets the same 204 as any other
    # that is not valid (verify refuses it at once), never a different error. The size
    # of the request is bounded by nginx.
    token: str = ""


def build_router(
    *,
    store: OptOutStore,
    secret: str,
    page_limiter: SlidingWindowLimiter,
    one_click_limiter: SlidingWindowLimiter,
    client_ip: Callable[[Request], str],
) -> APIRouter:
    router = APIRouter()

    async def opt_out(token: str, source: str) -> Response:
        if not secret:
            # Without the secret no commercial email goes out, so no link can be valid.
            raise HTTPException(503, MSG_UNAVAILABLE)
        account = unsubscribe.verify(secret, token)
        if account is not None:
            try:
                await store.unsubscribe_communications(account, source)
            except Exception as exc:  # the token must never reach the logs
                log.error("baja de comunicaciones no guardada: %s", type(exc).__name__)
                raise HTTPException(503, MSG_UNAVAILABLE) from None
        return Response(status_code=204)

    @router.post("/api/v1/public/unsubscribe", status_code=204)
    async def page(body: UnsubscribeRequest, request: Request) -> Response:
        if not page_limiter.allow(client_ip(request)):
            raise HTTPException(429, MSG_RATE_LIMITED)
        return await opt_out(body.token, SOURCE_PAGE)

    @router.post("/api/v1/public/unsubscribe/one-click", status_code=204)
    async def one_click(
        request: Request,
        token: str = Query(default=""),
    ) -> Response:
        # The body (List-Unsubscribe=One-Click) is not needed: the token is in the URL.
        if not one_click_limiter.allow(client_ip(request)):
            raise HTTPException(429, MSG_RATE_LIMITED)
        return await opt_out(token, SOURCE_ONE_CLICK)

    return router
