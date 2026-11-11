import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.application.rate_limit import SlidingWindowLimiter
from app.domain.contact import InvalidContact, normalize_contact
from app.ports.store import Store, WaitlistSignup

log = logging.getLogger("fichaitor.waitlist")

SOURCE_PATTERN = r"^[A-Za-z0-9_\-.]{1,60}$"


class WaitlistRequest(BaseModel):
    channel: Literal["whatsapp", "email"]
    contact: str = Field(min_length=5, max_length=254)
    consent: bool
    consent_version: str = Field(min_length=1, max_length=40)
    place_id: str | None = Field(default=None, max_length=200)
    # What the owner typed in the search box. This is what gets stored as the name.
    business_query: str | None = Field(default=None, max_length=200)
    # Deprecated: the web filled it with the name returned by Google Places, which the
    # terms forbid storing. Still accepted so old clients do not get a 422; never stored.
    business_name: str | None = Field(default=None, max_length=200)
    score: int | None = Field(default=None, ge=0, le=100)
    source: str | None = Field(default=None, pattern=SOURCE_PATTERN)


def build_router(store: Store, limiter: SlidingWindowLimiter, client_ip) -> APIRouter:
    router = APIRouter()

    @router.post("/api/v1/public/waitlist", status_code=201)
    async def join_waitlist(body: WaitlistRequest, request: Request) -> dict[str, str]:
        if not body.consent:
            raise HTTPException(422, "Necesitamos tu consentimiento para avisarte.")
        try:
            contact = normalize_contact(body.channel, body.contact)
        except InvalidContact as exc:
            raise HTTPException(422, str(exc)) from exc
        if not limiter.allow(client_ip(request)):
            raise HTTPException(429, "Demasiados intentos. Prueba dentro de un rato.")
        typed_name = " ".join((body.business_query or "").split()) or None
        # The score is ours but derived from Google Maps content. Whether it may be stored
        # is an open question for Google (docs/fase0/places-coste.md, section 5). Kept as
        # it is, here and in the funnel event below, until they answer.
        try:
            await store.add_signup(
                WaitlistSignup(
                    channel=body.channel,
                    contact=contact,
                    consent_version=body.consent_version,
                    place_id=body.place_id,
                    business_name=typed_name,
                    score=body.score,
                    source=body.source,
                )
            )
        except Exception as exc:  # the contact must never end up in logs
            log.error("no se pudo guardar el alta en la lista de espera: %s", type(exc).__name__)
            raise HTTPException(503, "No hemos podido apuntarte. Prueba en un minuto.") from exc
        try:
            await store.record_event("signup", body.source, body.place_id, body.score)
        except Exception as exc:
            log.warning("evento de embudo no registrado: %s", type(exc).__name__)
        # Same answer for a new and a repeated contact: it does not reveal who is on the list.
        return {"status": "ok"}

    return router
