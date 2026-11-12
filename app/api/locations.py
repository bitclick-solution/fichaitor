"""Activating and deactivating the profiles of an account (docs/multinegocio.md, 5.3).

Documented in docs/contrato-web-api.md, "Multinegocio". Both routes answer with the
body of GET /me. The database decides whether a profile fits (trigger
location_active_guard): two activations at once are counted one after the other, and
no SQL of the API can go past the limit. Nothing here charges.
"""

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from app.api.me import MeResponse, activation_refused, me_body
from app.api.session import (
    MSG_LEAVE_PENDING,
    MSG_LOCATION_NOT_FOUND,
    account_guard,
    location_id_or_404,
    require_csrf_header,
)
from app.application.profile_sync import ProfileSync
from app.domain.plans import Plan
from app.ports.store import (
    Account,
    ActivationRefused,
    LocationNotFound,
    OffboardingPending,
    Store,
)

ACTIVATION = "/api/v1/me/locations/{location_id}/activation"


class ActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Without a plan: an active profile keeps its own; an inactive one gets the free
    # plan while it is left and otherwise the first paid plan with room.
    plan: Plan | None = None


def _leave_pending() -> JSONResponse:
    # The answer of active_account_guard, with the code the web tells the 409s apart by.
    return JSONResponse(
        {"detail": MSG_LEAVE_PENDING, "code": OffboardingPending.code}, status_code=409
    )


def build_router(
    *,
    store: Store,
    sync: ProfileSync,
    clock: Callable[[], float],
    cookie_secure: bool = True,
) -> APIRouter:
    router = APIRouter()
    current_account = account_guard(store, clock, cookie_secure)

    def now() -> datetime:
        return datetime.fromtimestamp(clock(), UTC)

    @router.post(
        ACTIVATION,
        response_model=MeResponse,
        dependencies=[Depends(require_csrf_header)],
    )
    async def activate(
        location_id: str,
        request: Request,
        body: ActivationRequest | None = None,
        account: Account = Depends(current_account),  # noqa: B008
    ) -> MeResponse | JSONResponse:
        # Closed while a leave is scheduled, as every route behind active_account_guard;
        # the database refuses it as well.
        if account.pending_deletion is not None:
            return _leave_pending()
        location_id = location_id_or_404(location_id)
        try:
            await store.activate(account.id, location_id, body.plan if body else None)
        except LocationNotFound:
            raise HTTPException(404, MSG_LOCATION_NOT_FOUND) from None
        except ActivationRefused as exc:
            return activation_refused(exc)
        # So that the panel finds its figures ready. Never fails the activation itself.
        await sync.request_for_location(account.id, location_id)
        return await me_body(store, await current_account(request), now())

    @router.delete(
        ACTIVATION,
        response_model=MeResponse,
        dependencies=[Depends(require_csrf_header)],
    )
    async def deactivate(
        location_id: str,
        request: Request,
        account: Account = Depends(current_account),  # noqa: B008
    ) -> MeResponse | JSONResponse:
        # The profile stops being active at once: no sync, no proposal, its open ones
        # cancelled. Its data goes 30 days later unless it is activated again (the
        # worker's retention). Deactivating the last active profile is allowed.
        if account.pending_deletion is not None:
            return _leave_pending()
        if not await store.deactivate(account.id, location_id_or_404(location_id)):
            raise HTTPException(404, MSG_LOCATION_NOT_FOUND)
        # Read again: the preference may have moved to the oldest active profile.
        return await me_body(store, await current_account(request), now())

    return router
