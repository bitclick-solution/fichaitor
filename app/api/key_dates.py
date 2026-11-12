"""Key dates of the connected profile in the panel: the days Fichaitor asks about (a
holiday, Nochebuena), answering one and dismissing one. Documented in
docs/contrato-web-api.md, "Fechas clave".

The list is of one active profile (`?location=`, or the preference of the panel); a
proposal id from the request is only ever looked up among the account's own, of any of
its active profiles (docs/multinegocio.md, 5.4).
Answering is the owner's confirmation: a pending action plus a job; the worker's
executor does the write to Google.
"""

import logging
import re
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.api.session import (
    MSG_NO_LOCATION,
    active_account_guard,
    location_param,
    require_csrf_header,
    resolve_location,
)
from app.api.summary_out import seconds
from app.application.key_dates import Answer, KeyDates
from app.domain.actions import status_label, status_message
from app.domain.calendar import format_date_es
from app.domain.key_dates import (
    REFUSED_PAST,
    REFUSED_PROFILE,
    PanelRefused,
    panel_status,
)
from app.domain.special_hours import (
    KIND_CLOSED,
    KIND_PERIODS,
    KIND_REGULAR,
    InvalidSpecialHours,
    checked,
    describe,
    parse_clock,
)
from app.ports.key_dates import KeyDateNotFound, KeyDateView, SpecialHoursInProgress
from app.ports.store import Account, Store

log = logging.getLogger("fichaitor.key_dates")

PROPOSAL_ID = re.compile(r"prop_[0-9a-f]{16}")
MAX_RANGES_ACCEPTED = 10  # bounds the request; the rule (one or two) answers in Spanish

MSG_NO_PROFILE = MSG_NO_LOCATION
MSG_NOT_FOUND = "Esa fecha no está en tu ficha."
MSG_CLOSED = "Esa fecha ya no está pendiente: ya se respondió o se descartó."
MSG_PAST = "Ese día ya ha pasado. No se ha cambiado nada."
MSG_PROFILE = "Esa fecha era de otra ficha. No se ha cambiado nada."
MSG_IN_PROGRESS = "Ya hay un cambio de horario enviándose para ese día. Espera a que termine."
MSG_NOT_SAVED = "No hemos podido guardar tu respuesta. Prueba otra vez."
_REFUSED = {REFUSED_PAST: MSG_PAST, REFUSED_PROFILE: MSG_PROFILE}
# The kinds of the contract with the web, and the domain's.
_KINDS = {"closed": KIND_CLOSED, "regular": KIND_REGULAR, "custom": KIND_PERIODS}
_KINDS_OUT = {v: k for k, v in _KINDS.items()}


class Range(BaseModel):
    open: str = Field(max_length=5)
    close: str = Field(max_length=5)


class AnswerRequest(BaseModel):
    kind: Literal["closed", "regular", "custom"]
    ranges: list[Range] = Field(default_factory=list, max_length=MAX_RANGES_ACCEPTED)


class AnswerOut(BaseModel):
    kind: str
    ranges: list[Range]
    text: str


class KeyDateActionOut(BaseModel):
    id: str
    status: str
    message: str
    error: str | None
    confirmed_at: datetime | None
    finished_at: datetime | None


class KeyDateOut(BaseModel):
    id: str
    date: str
    name: str
    regular_hours: list[Range] | None
    # Besides the contract of the card: useful for the panel's history and the result.
    date_text: str
    kind: str
    status: str
    answerable: bool
    expires_at: datetime | None
    answer: AnswerOut | None
    action: KeyDateActionOut | None
    location_id: str  # the profile it is of (docs/multinegocio.md, 5.1)


class KeyDatesResponse(BaseModel):
    dates: list[KeyDateOut]
    location_id: str  # the profile they are of


def _hhmm(value: str) -> str:
    minutes = parse_clock(value, closing=True)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _answer_out(view: KeyDateView) -> AnswerOut | None:
    confirmed = view.confirmed
    if not confirmed:
        return None
    try:
        change = checked(view.day, confirmed.get("kind", ""), confirmed.get("periods") or [])
    except InvalidSpecialHours:
        return None
    return AnswerOut(
        kind=_KINDS_OUT[change.kind],
        ranges=[Range(open=_hhmm(o), close=_hhmm(c)) for o, c in change.periods],
        text=describe(change),
    )


def _regular_hours(weekly, view: KeyDateView) -> list[Range] | None:
    """The usual hours of the weekday of that date, as Google had them at the last sync.
    None when the profile has none (or they were never read); [] when that weekday is
    normally closed."""
    if not weekly:
        return None
    day = view.day.weekday()
    return [Range(open=o, close=c) for d, o, c in weekly if d == day]


def to_out(view: KeyDateView, answerable: bool, weekly=()) -> KeyDateOut:
    action = view.action
    return KeyDateOut(
        id=view.id,
        date=view.day.isoformat(),
        name=view.name,
        regular_hours=_regular_hours(weekly, view),
        date_text=format_date_es(view.day),
        kind=view.kind,
        status=panel_status(view.state),
        answerable=answerable,
        expires_at=seconds(view.expires_at),
        answer=_answer_out(view),
        location_id=view.location_id,
        action=(
            KeyDateActionOut(
                id=action.id,
                status=status_label(action.state),
                message=status_message(action.state, action.error_code, action.type),
                error=action.error_code,
                confirmed_at=seconds(action.confirmed_at),
                finished_at=seconds(action.finished_at),
            )
            if action is not None
            else None
        ),
    )


def build_router(
    *,
    store: Store,
    key_dates: KeyDates,
    clock: Callable[[], float],
    cookie_secure: bool = True,
) -> APIRouter:
    router = APIRouter()
    # Closed while a leave is scheduled (409): nothing is listed, answered or dismissed.
    current_account = active_account_guard(store, clock, cookie_secure)

    def id_or_404(proposal_id: str) -> str:
        if not PROPOSAL_ID.fullmatch(proposal_id):
            raise HTTPException(404, MSG_NOT_FOUND)
        return proposal_id

    async def weekly_of(account: Account, location_id: str):
        """The usual hours of that active profile, as the last sync read them."""
        profile = await store.profile(account.id, location_id)
        if profile is None:
            return ()
        facts = profile.summary.facts if profile.summary is not None else None
        return facts.regular_hours if facts is not None else ()

    def out(view: KeyDateView, weekly=()) -> KeyDateOut:
        return to_out(view, key_dates.answerable(view), weekly)

    @router.get("/api/v1/me/key-dates", response_model=KeyDatesResponse)
    async def my_key_dates(
        filter: Literal["pending", "all"] = Query("pending"),  # noqa: A002
        account: Account = Depends(current_account),  # noqa: B008
        location: str | None = Depends(location_param),  # noqa: B008
    ) -> KeyDatesResponse:
        loc = await resolve_location(store, account, location)
        weekly = await weekly_of(account, loc)
        found = await key_dates.list(account.id, pending_only=filter == "pending", location_id=loc)
        return KeyDatesResponse(dates=[out(v, weekly) for v in found], location_id=loc)

    @router.post(
        "/api/v1/me/key-dates/{proposal_id}/answer",
        response_model=KeyDateOut,
        status_code=202,
        dependencies=[Depends(require_csrf_header)],
    )
    async def answer_key_date(
        proposal_id: str,
        body: AnswerRequest,
        account: Account = Depends(current_account),  # noqa: B008
    ) -> KeyDateOut:
        # This request IS the owner's confirmation ("Confirmar" in the panel): a session,
        # the protection header and the exact hours. What goes to Google is what is
        # checked here.
        # The proposal names its profile: it must be an active one of this account,
        # whatever the preference (docs/multinegocio.md, 5.4).
        proposal_id = id_or_404(proposal_id)
        ranges = tuple((r.open, r.close) for r in body.ranges)
        try:
            view = await key_dates.answer(
                account.id, proposal_id, Answer(_KINDS[body.kind], ranges)
            )
        except KeyDateNotFound:
            raise HTTPException(404, MSG_NOT_FOUND) from None
        except InvalidSpecialHours as exc:
            raise HTTPException(422, str(exc)) from None
        except PanelRefused as exc:
            raise HTTPException(409, _REFUSED.get(exc.code, MSG_CLOSED)) from None
        except SpecialHoursInProgress:
            raise HTTPException(409, MSG_IN_PROGRESS) from None
        except Exception as exc:
            log.error("respuesta a una fecha clave no registrada: %s", type(exc).__name__)
            raise HTTPException(503, MSG_NOT_SAVED) from None
        return out(view, await weekly_of(account, view.location_id))

    @router.post(
        "/api/v1/me/key-dates/{proposal_id}/dismiss",
        status_code=204,
        dependencies=[Depends(require_csrf_header)],
    )
    async def dismiss_key_date(
        proposal_id: str,
        account: Account = Depends(current_account),  # noqa: B008
    ) -> Response:
        proposal_id = id_or_404(proposal_id)
        try:
            await key_dates.dismiss(account.id, proposal_id)
        except KeyDateNotFound:
            raise HTTPException(404, MSG_NOT_FOUND) from None
        except PanelRefused:
            raise HTTPException(409, MSG_CLOSED) from None
        return Response(status_code=204)

    return router
