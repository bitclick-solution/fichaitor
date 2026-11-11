"""Routes behind a session: the account, its profiles and the diagnosis of the chosen one.

Every read and write goes through the account of the session; a location id from
the request is only ever looked up among that account's profiles.
"""

import logging
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api.diagnosis_out import DiagnosisResponse, to_response
from app.api.session import (
    SESSION_COOKIE,
    account_guard,
    clear_session_cookie,
    require_csrf_header,
    utc,
)
from app.application.diagnosis_service import DiagnosisService, RateLimited
from app.domain.session import hash_token
from app.ports.places import PlacesUnavailable
from app.ports.store import Account, Store

log = logging.getLogger("fichaitor.me")


class LocationOut(BaseModel):
    id: str
    title: str
    place_id: str | None
    verified: bool


class MeResponse(BaseModel):
    email: str
    locations: list[LocationOut]
    selected_location_id: str | None


class SelectLocationRequest(BaseModel):
    location_id: str = Field(min_length=1, max_length=64)


def build_router(
    *,
    store: Store,
    diagnosis: DiagnosisService,
    cookie_secure: bool,
    clock: Callable[[], float],
) -> APIRouter:
    router = APIRouter()
    current_account = account_guard(store, clock)

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
        account: Account = Depends(current_account),  # noqa: B008
    ) -> MeResponse:
        # Same answer for a profile of another account and for one that does not exist.
        if not await store.select_location(account.id, body.location_id):
            raise HTTPException(404, "Esa ficha no está en tu cuenta.")
        return await me_of(
            Account(account.id, account.email, selected_location_id=body.location_id)
        )

    @router.get("/api/v1/me/diagnosis", response_model=DiagnosisResponse)
    async def my_diagnosis(
        account: Account = Depends(current_account),  # noqa: B008
    ) -> DiagnosisResponse:
        if account.selected_location_id is None:
            raise HTTPException(409, "Primero elige con qué ficha quieres trabajar.")
        locations = await store.list_locations(account.id)
        location = next((loc for loc in locations if loc.id == account.selected_location_id), None)
        if location is None:
            raise HTTPException(409, "Primero elige con qué ficha quieres trabajar.")
        try:
            result = await diagnosis.run_for_profile(
                location.place_id, location.title, f"account:{account.id}"
            )
        except RateLimited as exc:
            raise HTTPException(
                429, "Has hecho muchas consultas. Prueba dentro de un rato."
            ) from exc
        except PlacesUnavailable as exc:
            raise HTTPException(
                503, "Google no responde ahora mismo. Prueba en un minuto."
            ) from exc
        if result is None:
            raise HTTPException(
                404,
                "No encontramos tu ficha en Google Maps. Puede que aún no sea pública "
                "o que esté pendiente de verificar.",
            )
        return to_response(result)

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
