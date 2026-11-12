"""Leaving Fichaitor: disconnect the profile or delete the account.
Documented in docs/contrato-web-api.md, "Baja: desconectar y borrar la cuenta".

A leave is confirmed with a one-time code sent to the email of the account, stays
scheduled for 48 hours (it can be restored from the session or from the link of the
email) and only then becomes effective, in the worker. Nothing here talks to Google or
to a mail server.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api.session import account_guard, require_csrf_header
from app.api.summary_out import seconds
from app.application.offboarding import (
    CodeGone,
    CodeLocked,
    CodeRateLimited,
    CodeWrong,
    LeavePending,
    MailUnavailable,
    Offboarding,
)
from app.application.profile_sync import ProfileSync
from app.application.rate_limit import SlidingWindowLimiter
from app.domain.offboarding import MAIL_LABELS
from app.ports.store import Account, Store

log = logging.getLogger("fichaitor.offboarding")

MSG_MAIL_UNAVAILABLE = (
    "Ahora mismo no podemos enviarte el código por correo. Escríbenos a soporte@fichaitor.es."
)
MSG_CODE_RATE_LIMITED = "Has pedido demasiados códigos. Prueba dentro de un rato."
MSG_ALREADY_PENDING = "Ya tienes una baja programada. Restáurala si quieres cambiarla."
MSG_CODE_GONE = "El código ha caducado o ya se ha usado. Pide otro."
MSG_CODE_LOCKED = "Demasiados intentos. Ese código ya no vale: pide otro."
MSG_NOT_SAVED = "No hemos podido hacerlo. Prueba otra vez en un minuto."
MSG_NOTHING_TO_RESTORE = "No tienes ninguna baja programada."
MSG_RESTORE_GONE = "Este enlace ya no vale: ha caducado o ya se ha usado."
MSG_RESTORE_RATE_LIMITED = "Demasiados intentos. Prueba dentro de un rato."


def wrong_code_message(attempts_left: int) -> str:
    left = "Te queda 1 intento." if attempts_left == 1 else f"Te quedan {attempts_left} intentos."
    return f"El código no es correcto. {left}"


Scope = Literal["ficha", "cuenta"]


class CodeRequest(BaseModel):
    scope: Scope


class CodeResponse(BaseModel):
    sent_to: str
    expires_in: int


class CodeStatusResponse(BaseModel):
    status: str  # pendiente, enviado, fallido or ninguno


class ConfirmRequest(BaseModel):
    scope: Scope
    # Any text: a malformed code is a wrong code, with a message ready to show.
    code: str = Field(min_length=1, max_length=20)


class ConfirmResponse(BaseModel):
    scope: str
    delete_at: datetime


class PublicRestoreRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)


def build_router(
    *,
    store: Store,
    offboarding: Offboarding,
    sync: ProfileSync,
    restore_limiter: SlidingWindowLimiter,
    client_ip: Callable[[Request], str],
    clock: Callable[[], float],
    cookie_secure: bool = True,
) -> APIRouter:
    router = APIRouter()
    current_account = account_guard(store, clock, cookie_secure)

    @router.post(
        "/api/v1/me/offboarding/code",
        response_model=CodeResponse,
        status_code=202,
        dependencies=[Depends(require_csrf_header)],
    )
    async def request_code(
        body: CodeRequest,
        account: Account = Depends(current_account),  # noqa: B008
    ) -> CodeResponse:
        try:
            sent = await offboarding.request_code(account, body.scope)
        except MailUnavailable:
            raise HTTPException(503, MSG_MAIL_UNAVAILABLE) from None
        except LeavePending:
            raise HTTPException(409, MSG_ALREADY_PENDING) from None
        except CodeRateLimited:
            raise HTTPException(429, MSG_CODE_RATE_LIMITED) from None
        except Exception as exc:  # the code and the address must never reach the logs
            log.error("código de baja no creado: %s", type(exc).__name__)
            raise HTTPException(503, MSG_NOT_SAVED) from None
        return CodeResponse(sent_to=sent.sent_to, expires_in=sent.expires_in)

    @router.get("/api/v1/me/offboarding/code", response_model=CodeStatusResponse)
    async def code_status(
        account: Account = Depends(current_account),  # noqa: B008
    ) -> CodeStatusResponse:
        # The request for a code answers before the email is out: this is how the web
        # learns that it could not be sent.
        state = await offboarding.code_mail_state(account)
        return CodeStatusResponse(status=MAIL_LABELS.get(state or "", "ninguno"))

    @router.post(
        "/api/v1/me/offboarding/confirm",
        response_model=ConfirmResponse,
        dependencies=[Depends(require_csrf_header)],
    )
    async def confirm(
        body: ConfirmRequest,
        account: Account = Depends(current_account),  # noqa: B008
    ) -> ConfirmResponse:
        try:
            delete_at = await offboarding.confirm(account, body.scope, body.code.strip())
        except LeavePending:
            raise HTTPException(409, MSG_ALREADY_PENDING) from None
        except CodeWrong as exc:
            raise HTTPException(422, wrong_code_message(exc.attempts_left)) from None
        except CodeGone:
            raise HTTPException(410, MSG_CODE_GONE) from None
        except CodeLocked:
            raise HTTPException(429, MSG_CODE_LOCKED) from None
        except Exception as exc:  # the code must never reach the logs
            log.error("baja no programada: %s", type(exc).__name__)
            raise HTTPException(503, MSG_NOT_SAVED) from None
        return ConfirmResponse(scope=body.scope, delete_at=seconds(delete_at))

    async def after_restore(account_id: str) -> None:
        # Back to work: the figures are read again at once, whatever the sync window
        # says. Never fails the restore itself.
        await sync.request_for_account(account_id, reconnected=True)

    @router.post(
        "/api/v1/me/offboarding/restore",
        status_code=204,
        dependencies=[Depends(require_csrf_header)],
    )
    async def restore(account: Account = Depends(current_account)) -> Response:  # noqa: B008
        try:
            restored = await offboarding.restore(account)
        except Exception as exc:
            log.error("baja no restaurada: %s", type(exc).__name__)
            raise HTTPException(503, MSG_NOT_SAVED) from None
        if not restored:
            raise HTTPException(409, MSG_NOTHING_TO_RESTORE)
        await after_restore(account.id)
        return Response(status_code=204)

    @router.post("/api/v1/public/offboarding/restore", status_code=204)
    async def public_restore(body: PublicRestoreRequest, request: Request) -> Response:
        if not restore_limiter.allow(client_ip(request)):
            raise HTTPException(429, MSG_RESTORE_RATE_LIMITED)
        try:
            account_id = await offboarding.restore_by_token(body.token)
        except Exception as exc:  # the token must never reach the logs
            log.error("baja no restaurada por enlace: %s", type(exc).__name__)
            raise HTTPException(503, MSG_NOT_SAVED) from None
        if account_id is None:
            # Unknown, used and expired tokens get the very same answer.
            raise HTTPException(410, MSG_RESTORE_GONE)
        await after_restore(account_id)
        return Response(status_code=204)

    return router
