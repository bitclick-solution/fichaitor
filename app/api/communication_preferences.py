"""The commercial communications in Ajustes (LSSI art. 21.2; docs/contrato-web-api.md,
«Comunicaciones comerciales: en el alta y en Ajustes»).

- GET /api/v1/me/communications: whether the account gets them and its last change.
- PUT /api/v1/me/communications {"commercial": bool}: false opposes them (source
  `settings`), true withdraws the opposition. Idempotent.

The only source of truth is `communication_optout`, the same table the email link and
the one-click header write and the sending reads (db/schema_admin.sql). The service
emails are not affected.

Who: the account of the session. A web session is only ever opened by connecting with
the Google identity of the account (app/api/google_oauth.py): the managers of a business
only exist as chat channels (Telegram, WhatsApp) and never reach these routes. Answered
also during a scheduled leave and while the account is blocked for a payment: opposing
must be possible at any time.
"""

import logging
from collections.abc import Callable
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool

from app.api.session import account_guard, require_csrf_header
from app.api.summary_out import seconds
from app.ports.store import Account, Store

log = logging.getLogger("fichaitor.communications")

SOURCE_SETTINGS = "settings"
MSG_UNAVAILABLE = "Ahora mismo no podemos guardarlo. Prueba otra vez en un minuto."
MSG_READ_UNAVAILABLE = "Ahora mismo no podemos leerlo. Prueba otra vez en un minuto."


class CommunicationsOut(BaseModel):
    # True: it gets the commercial communications (offers, news meant to sell).
    commercial: bool
    # The last change; None if it never changed.
    changed_at: datetime | None
    # signup, settings, enlace or un_clic; None if it never changed.
    source: str | None


class CommunicationsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commercial: StrictBool


def build_router(*, store: Store, clock: Callable[[], float], cookie_secure: bool) -> APIRouter:
    router = APIRouter()
    current_account = account_guard(store, clock, cookie_secure)

    async def read(account: Account, message: str) -> CommunicationsOut:
        try:
            pref = await store.communication_preference(account.id)
        except Exception as exc:  # identifiers must never reach the logs
            log.error("preferencia de comunicaciones no leída: %s", type(exc).__name__)
            raise HTTPException(503, message) from None
        if pref is None:  # the account left a moment ago
            raise HTTPException(401, "No has iniciado sesión.")
        return CommunicationsOut(
            commercial=pref.commercial, changed_at=seconds(pref.changed_at), source=pref.source
        )

    @router.get("/api/v1/me/communications", response_model=CommunicationsOut)
    async def get_communications(
        account: Account = Depends(current_account),  # noqa: B008
    ) -> CommunicationsOut:
        return await read(account, MSG_READ_UNAVAILABLE)

    @router.put(
        "/api/v1/me/communications",
        response_model=CommunicationsOut,
        dependencies=[Depends(require_csrf_header)],
    )
    async def put_communications(
        body: CommunicationsIn,
        account: Account = Depends(current_account),  # noqa: B008
    ) -> CommunicationsOut:
        try:
            if body.commercial:
                await store.resubscribe_communications(account.id, SOURCE_SETTINGS)
            else:
                await store.unsubscribe_communications(account.id, SOURCE_SETTINGS)
        except Exception as exc:  # identifiers must never reach the logs
            log.error("preferencia de comunicaciones no guardada: %s", type(exc).__name__)
            raise HTTPException(503, MSG_UNAVAILABLE) from None
        return await read(account, MSG_UNAVAILABLE)

    return router
