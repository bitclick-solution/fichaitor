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

from app.api.billing import no_seat
from app.api.me import MeResponse, activation_refused, me_body
from app.api.session import (
    MSG_LEAVE_PENDING,
    MSG_LOCATION_NOT_FOUND,
    account_blocked_response,
    account_guard,
    location_id_or_404,
    require_csrf_header,
)
from app.application.profile_sync import ProfileSync
from app.domain import plans
from app.domain.billing import new_seat_prices
from app.domain.plans import Plan
from app.ports.billing_store import BillingStore, seats_in_force
from app.ports.store import (
    Account,
    ActivationRefused,
    LocationLimit,
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
    billing: BillingStore | None = None,
) -> APIRouter:
    router = APIRouter()
    current_account = account_guard(store, clock, cookie_secure)

    async def paywall(account_id: str, plan: Plan | None) -> JSONResponse:
        """No room in the plan: 402 `no_seat` with the seat to buy and its price today
        (docs/contrato-web-api.md, «Cobro y paywall»)."""
        wanted = plan if plan in plans.PAID_PLANS else Plan.PRO
        seats = await billing.seats(account_id) if billing is not None else []
        launch_at = await billing.launch_at() if billing is not None else plans.LAUNCH_AT
        price = new_seat_prices(seats_in_force(seats, now()))[wanted.value]
        return no_seat(wanted, price, plans.charging(now(), launch_at))

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
        # A payment not made: nothing is activated until it is paid (deactivating is).
        if account.blocked is not None:
            return account_blocked_response()
        location_id = location_id_or_404(location_id)
        plan = body.plan if body else None
        try:
            await store.activate(account.id, location_id, plan)
        except LocationNotFound:
            raise HTTPException(404, MSG_LOCATION_NOT_FOUND) from None
        except LocationLimit:
            return await paywall(account.id, plan)
        except ActivationRefused as exc:
            return activation_refused(exc)
        # So that the panel finds its figures ready. Never fails the activation itself.
        await sync.request_for_location(account.id, location_id)
        return await me_body(store, await current_account(request), now(), billing)

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
        return await me_body(store, await current_account(request), now(), billing)

    return router
