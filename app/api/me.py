"""Routes behind a session: the account, its profiles, and the diagnosis, summary and
sentiment of one of its active profiles.

Every read and write goes through the account of the session; a location id from the
request (`?location=`, docs/multinegocio.md, 5.1) is only ever looked up among that
account's profiles.

Neither the summary, the diagnosis nor the sentiment talks to Google or to a model:
they read what the worker stored.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.diagnosis_out import (
    UNAVAILABLE_STATUS_CODE,
    DiagnosisUnavailable,
    ProfileDiagnosisResponse,
    to_profile_response,
)
from app.api.sentiment_out import SentimentResponse, empty_response, to_sentiment_response
from app.api.session import (
    MSG_LEAVE_PENDING,
    MSG_NO_LOCATION,
    account_guard,
    active_account_guard,
    clear_session_cookie,
    location_param,
    require_csrf_header,
    resolve_location,
    session_cookie_name,
    utc,
)
from app.api.summary_out import STATUS_PENDING, SummaryResponse, seconds, to_summary_response
from app.application.profile_sync import ProfileSync
from app.domain import plans
from app.domain.calendar import Holiday
from app.domain.conversation import DEFAULT_TIMEZONE, timezone_for
from app.domain.profile_summary import STATUS_OK
from app.domain.sentiment import STATUS_ERROR, SentimentPolicy
from app.domain.session import hash_token
from app.ports.store import (
    Account,
    ActivationRefused,
    FreeTaken,
    LocationLimit,
    LocationOverview,
    LocationRemoved,
    OffboardingPending,
    Store,
)

log = logging.getLogger("fichaitor.me")

# docs/multinegocio.md, «Aclaraciones para la web». No charging yet (beta).
MSG_NO_ROOM = "No te quedan fichas libres en tu plan. Escríbenos para activar más fichas."
MSG_FREE_TAKEN = (
    "Esta ficha ya está activa en el plan Gratis en otra cuenta. Actívala con un plan de "
    "pago o pide a esa cuenta que la desactive."
)
MSG_LOCATION_REMOVED = (
    "Google ya no muestra esa ficha en tu cuenta. Vuelve a conectar con Google para verla."
)
MSG_NOT_ACTIVATED = "Ahora no se puede activar esa ficha."


def activation_refused(exc: ActivationRefused) -> JSONResponse:
    """What an activation the database refused answers (docs/multinegocio.md, 5.3 and
    «Aclaraciones para la web», 5): 402 without room in the plan; otherwise 409 with
    `code`, so that the web can tell the cases apart."""
    if isinstance(exc, LocationLimit):
        return JSONResponse({"detail": MSG_NO_ROOM}, status_code=402)
    detail = {
        FreeTaken.code: MSG_FREE_TAKEN,
        OffboardingPending.code: MSG_LEAVE_PENDING,
        LocationRemoved.code: MSG_LOCATION_REMOVED,
    }.get(exc.code, MSG_NOT_ACTIVATED)
    return JSONResponse({"detail": detail, "code": exc.code}, status_code=409)


class PendingOut(BaseModel):
    reviews: int  # unanswered reviews, as the last sync counted them
    key_dates: int  # key dates still to answer, today or later


class LocationOut(BaseModel):
    id: str
    title: str
    place_id: str | None
    verified: bool
    # Multinegocio (docs/multinegocio.md, 5.2 and 12).
    active: bool
    plan: str | None  # gratis, pro or pro_whatsapp; None if inactive
    active_since: datetime | None
    # The status of its summary (ok, pendiente, error or needs_reconnect); None if inactive.
    status: str | None
    pending: PendingOut | None  # None if inactive


class PlanRoomOut(BaseModel):
    pro: int
    pro_whatsapp: int


class PlansOut(BaseModel):
    """What the account has active and the room it has left (app/domain/plans.py)."""

    active: int
    free_available: bool
    available: PlanRoomOut


class PlanPriceOut(BaseModel):
    first: int  # cents a year, the first paid profile of the account
    additional: int  # cents a year, from the second one


class PlanPricesOut(BaseModel):
    pro: PlanPriceOut
    pro_whatsapp: PlanPriceOut


class PricingOut(BaseModel):
    """docs/multinegocio.md, 12, «Precios en la respuesta». Cents; the web stores none."""

    currency: str
    period: str
    charging: bool  # False during the beta: the prices are for reference only
    plans: PlanPricesOut
    total: int
    if_activate: dict[str, int]  # by plan: the total after activating one more
    if_deactivate: dict[str, int]  # by active profile: the total after deactivating it


class PendingDeletionOut(BaseModel):
    scope: str  # ficha or cuenta
    delete_at: datetime


class MeResponse(BaseModel):
    email: str
    locations: list[LocationOut]
    # The profile the panel shows when a read names none: a preference (3.5).
    selected_location_id: str | None
    # False after a leave of the profile, until the owner connects with Google again.
    connected: bool
    # A scheduled leave: the web shows it with the way to restore, and nothing else.
    pending_deletion: PendingDeletionOut | None
    plans: PlansOut
    pricing: PricingOut


class SelectLocationRequest(BaseModel):
    location_id: str = Field(min_length=1, max_length=64)


def _location_out(location: LocationOverview, leaving: bool) -> LocationOut:
    # During a scheduled leave nothing of a profile is read: no status, no counters.
    shown = location.active and not leaving
    return LocationOut(
        id=location.id,
        title=location.title,
        place_id=location.place_id,
        verified=location.verified,
        active=location.active,
        plan=location.plan.value if location.plan is not None else None,
        active_since=seconds(location.active_since),
        status=(location.summary_status or STATUS_PENDING) if shown else None,
        pending=(
            PendingOut(
                reviews=location.pending_reviews or 0,
                key_dates=location.pending_key_dates or 0,
            )
            if shown
            else None
        ),
    )


async def me_body(store: Store, account: Account, now: datetime) -> MeResponse:
    """The body of GET /me, which is also the answer of choosing, activating and
    deactivating a profile."""
    # Key dates count from today in the zone they are asked in (app/application/key_dates.py).
    today = now.astimezone(timezone_for(DEFAULT_TIMEZONE)).date()
    overview = await store.locations_overview(account.id, today)
    leaving = account.pending_deletion is not None
    return MeResponse(
        email=account.email,
        locations=[_location_out(loc, leaving) for loc in overview.locations],
        selected_location_id=account.selected_location_id,
        connected=account.connected,
        pending_deletion=(
            PendingDeletionOut(
                scope=account.pending_deletion.scope,
                delete_at=seconds(account.pending_deletion.delete_at),
            )
            if account.pending_deletion is not None
            else None
        ),
        plans=PlansOut.model_validate(
            plans.usage(overview.active_by_plan(), overview.entitlements, now)
        ),
        pricing=PricingOut.model_validate(plans.pricing(overview.active(), now)),
    )


def build_router(
    *,
    store: Store,
    sync: ProfileSync,
    holidays: Sequence[Holiday],
    cookie_secure: bool,
    clock: Callable[[], float],
    sentiment_policy: SentimentPolicy | None = None,
) -> APIRouter:
    router = APIRouter()
    current_account = account_guard(store, clock, cookie_secure)
    # Every route that reads the profile or works on it: closed while a leave is scheduled.
    active_account = active_account_guard(store, clock, cookie_secure)
    sentiment_policy = sentiment_policy or SentimentPolicy()

    async def me_of(account: Account) -> MeResponse:
        return await me_body(store, account, datetime.fromtimestamp(clock(), UTC))

    @router.get("/api/v1/me", response_model=MeResponse)
    async def me(account: Account = Depends(current_account)) -> MeResponse:  # noqa: B008
        return await me_of(account)

    @router.post(
        "/api/v1/me/location",
        response_model=MeResponse,
        dependencies=[Depends(require_csrf_header)],
    )
    async def select_location(
        body: SelectLocationRequest,
        account: Account = Depends(active_account),  # noqa: B008
    ) -> MeResponse | JSONResponse:
        # Same answer for a profile of another account and for one that does not exist.
        # Until step 9 of docs/multinegocio.md (5.5), choosing an inactive profile
        # activates it with the default plan; that may not fit.
        try:
            chosen = await store.select_location(account.id, body.location_id)
        except ActivationRefused as exc:
            return activation_refused(exc)
        if not chosen:
            raise HTTPException(404, "Esa ficha no está en tu cuenta.")
        # So that the panel finds its figures ready. Never fails the choice itself.
        await sync.request_for_location(account.id, body.location_id)
        return await me_of(replace(account, selected_location_id=body.location_id))

    @router.get(
        "/api/v1/me/diagnosis",
        response_model=ProfileDiagnosisResponse,
        responses={UNAVAILABLE_STATUS_CODE: {"model": DiagnosisUnavailable}},
    )
    async def my_diagnosis(
        account: Account = Depends(active_account),  # noqa: B008
        location: str | None = Depends(location_param),  # noqa: B008
    ) -> ProfileDiagnosisResponse | JSONResponse:
        # Computed here, from the facts the worker stored: no call to Google, no cost
        # and so no rate limit. Reading it asks for a sync under the same rules as the
        # summary (missing, old or failed some time ago).
        view = await sync.view(account.id, await resolve_location(store, account, location))
        if view is None:  # deactivated a moment ago
            raise HTTPException(409, MSG_NO_LOCATION)
        result = to_profile_response(view, holidays, utc(clock()).date())
        if isinstance(result, DiagnosisUnavailable):
            return JSONResponse(result.model_dump(), status_code=UNAVAILABLE_STATUS_CODE)
        return result

    @router.get("/api/v1/me/summary", response_model=SummaryResponse)
    async def my_summary(
        account: Account = Depends(active_account),  # noqa: B008
        location: str | None = Depends(location_param),  # noqa: B008
    ) -> SummaryResponse:
        # Looked up with the account of the session: ?location= only picks among its own
        # active profiles.
        view = await sync.view(account.id, await resolve_location(store, account, location))
        if view is None:
            raise HTTPException(409, MSG_NO_LOCATION)
        return to_summary_response(view)

    @router.get("/api/v1/me/sentiment", response_model=SentimentResponse)
    async def my_sentiment(
        account: Account = Depends(active_account),  # noqa: B008
        location: str | None = Depends(location_param),  # noqa: B008
    ) -> SentimentResponse:
        # Read from what the worker stored, by the account of the session only. No
        # model is called here: no cost and so no rate limit.
        view = await sync.view(account.id, await resolve_location(store, account, location))
        if view is None:
            raise HTTPException(409, MSG_NO_LOCATION)
        stored = await store.sentiment(account.id, utc(clock()), view.location_id)
        if stored is None or stored.analysis.figures.status == STATUS_ERROR:
            if view.summary is not None and view.summary.status != STATUS_OK:
                # The profile itself could not be read: there is nothing to analyse.
                response = empty_response(STATUS_ERROR)
                return response.model_copy(update={"location_id": view.location_id})
            # The analysis is chained to a sync: a profile synced before this existed,
            # or whose analysis failed, gets one asked for (once per window).
            await sync.request_for_location(account.id, view.location_id)
        response = to_sentiment_response(stored, sentiment_policy)
        return response.model_copy(update={"location_id": view.location_id})

    @router.post(
        "/api/v1/auth/logout", status_code=204, dependencies=[Depends(require_csrf_header)]
    )
    async def logout(request: Request) -> Response:
        token = request.cookies.get(session_cookie_name(cookie_secure))
        if token:
            try:
                await store.revoke_session(hash_token(token), utc(clock()))
            except Exception as exc:  # the token must never reach the logs
                log.error("no se pudo revocar la sesión: %s", type(exc).__name__)
                raise HTTPException(
                    503, "No hemos podido cerrar la sesión. Prueba otra vez."
                ) from exc
        response = Response(status_code=204)
        clear_session_cookie(response, cookie_secure)
        return response

    return router
