"""Routes behind a session: the account, its profiles and the diagnosis of the chosen one.

Every read and write goes through the account of the session; a location id from
the request is only ever looked up among that account's profiles.

Neither the summary, the diagnosis nor the sentiment talks to Google or to a model:
they read what the worker stored.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime

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
    SESSION_COOKIE,
    account_guard,
    active_account_guard,
    clear_session_cookie,
    require_csrf_header,
    utc,
)
from app.api.summary_out import SummaryResponse, seconds, to_summary_response
from app.application.profile_sync import ProfileSync
from app.domain.calendar import Holiday
from app.domain.profile_summary import STATUS_OK
from app.domain.sentiment import STATUS_ERROR, SentimentPolicy
from app.domain.session import hash_token
from app.ports.store import Account, Store

log = logging.getLogger("fichaitor.me")


class LocationOut(BaseModel):
    id: str
    title: str
    place_id: str | None
    verified: bool


class PendingDeletionOut(BaseModel):
    scope: str  # ficha or cuenta
    delete_at: datetime


class MeResponse(BaseModel):
    email: str
    locations: list[LocationOut]
    selected_location_id: str | None
    # False after a leave of the profile, until the owner connects with Google again.
    connected: bool
    # A scheduled leave: the web shows it with the way to restore, and nothing else.
    pending_deletion: PendingDeletionOut | None


class SelectLocationRequest(BaseModel):
    location_id: str = Field(min_length=1, max_length=64)


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
    current_account = account_guard(store, clock)
    # Every route that reads the profile or works on it: closed while a leave is scheduled.
    active_account = active_account_guard(store, clock)
    sentiment_policy = sentiment_policy or SentimentPolicy()

    async def me_of(account: Account) -> MeResponse:
        locations = await store.list_locations(account.id)
        return MeResponse(
            email=account.email,
            locations=[
                LocationOut(
                    id=loc.id, title=loc.title, place_id=loc.place_id, verified=loc.verified
                )
                for loc in locations
            ],
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
        )

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
    ) -> MeResponse:
        # Same answer for a profile of another account and for one that does not exist.
        if not await store.select_location(account.id, body.location_id):
            raise HTTPException(404, "Esa ficha no está en tu cuenta.")
        # So that the panel finds its figures ready. Never fails the choice itself.
        await sync.request_for_account(account.id)
        return await me_of(replace(account, selected_location_id=body.location_id))

    @router.get(
        "/api/v1/me/diagnosis",
        response_model=ProfileDiagnosisResponse,
        responses={UNAVAILABLE_STATUS_CODE: {"model": DiagnosisUnavailable}},
    )
    async def my_diagnosis(
        account: Account = Depends(active_account),  # noqa: B008
    ) -> ProfileDiagnosisResponse | JSONResponse:
        # Computed here, from the facts the worker stored: no call to Google, no cost
        # and so no rate limit. Reading it asks for a sync under the same rules as the
        # summary (missing, old or failed some time ago).
        view = await sync.view(account.id)
        if view is None:
            raise HTTPException(409, "Primero elige con qué ficha quieres trabajar.")
        result = to_profile_response(view, holidays, utc(clock()).date())
        if isinstance(result, DiagnosisUnavailable):
            return JSONResponse(result.model_dump(), status_code=UNAVAILABLE_STATUS_CODE)
        return result

    @router.get("/api/v1/me/summary", response_model=SummaryResponse)
    async def my_summary(
        account: Account = Depends(active_account),  # noqa: B008
    ) -> SummaryResponse:
        # Looked up by the account of the session only: nothing in the request chooses it.
        view = await sync.view(account.id)
        if view is None:
            raise HTTPException(409, "Primero elige con qué ficha quieres trabajar.")
        return to_summary_response(view)

    @router.get("/api/v1/me/sentiment", response_model=SentimentResponse)
    async def my_sentiment(
        account: Account = Depends(active_account),  # noqa: B008
    ) -> SentimentResponse:
        # Read from what the worker stored, by the account of the session only. No
        # model is called here: no cost and so no rate limit.
        view = await sync.view(account.id)
        if view is None:
            raise HTTPException(409, "Primero elige con qué ficha quieres trabajar.")
        stored = await store.sentiment(account.id, utc(clock()))
        if stored is None or stored.analysis.figures.status == STATUS_ERROR:
            if view.summary is not None and view.summary.status != STATUS_OK:
                # The profile itself could not be read: there is nothing to analyse.
                return empty_response(STATUS_ERROR)
            # The analysis is chained to a sync: a profile synced before this existed,
            # or whose analysis failed, gets one asked for (once per window).
            await sync.request_for_account(account.id)
        return to_sentiment_response(stored, sentiment_policy)

    @router.post(
        "/api/v1/auth/logout", status_code=204, dependencies=[Depends(require_csrf_header)]
    )
    async def logout(request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE)
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
