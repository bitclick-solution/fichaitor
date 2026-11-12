"""Shape of a diagnosis in the API: the public one and the one behind a session.

Documented in docs/contrato-web-api.md.
"""

from collections.abc import Iterable
from datetime import date, datetime

from pydantic import BaseModel

from app.api.summary_out import STATUS_PENDING, seconds
from app.application.profile_sync import SummaryView
from app.domain.calendar import Holiday
from app.domain.diagnosis import Diagnosis
from app.domain.profile_diagnosis import ConnectedProfile, diagnose_profile
from app.domain.profile_summary import STATUS_NEEDS_RECONNECT, STATUS_OK

# Shown by the web next to the data: Google requires its content to be attributed.
GOOGLE_MAPS_ATTRIBUTION = "Google Maps"
# The diagnosis behind a session comes from the owner's own profile, not from Google Maps.
PROFILE_ATTRIBUTION = "Datos de tu Perfil de Empresa de Google"

UNAVAILABLE_STATUS_CODE = 404
UNAVAILABLE_DETAIL = {
    STATUS_PENDING: "Estamos leyendo tu ficha. Vuelve a intentarlo en unos segundos.",
    STATUS_NEEDS_RECONNECT: (
        "Google nos ha retirado el permiso para leer tu ficha. Vuelve a conectar con Google."
    ),
}
UNAVAILABLE_DETAIL_ERROR = "No hemos podido leer tu ficha. Prueba más tarde."


class FindingOut(BaseModel):
    code: str
    title: str
    detail: str
    impact: str


class LockedCheckOut(BaseModel):
    code: str
    title: str


class DiagnosisResponse(BaseModel):
    place_id: str
    name: str
    address: str
    category: str
    score: int
    label: str
    findings: list[FindingOut]
    locked_checks: list[LockedCheckOut]
    maps_url: str
    attribution: str


class ProfileDiagnosisResponse(DiagnosisResponse):
    """GET /api/v1/me/diagnosis: the public shape plus where the data stands."""

    status: str  # ok, needs_reconnect or error: the state of the last sync
    synced_at: datetime | None  # the diagnosis is of the profile as it was then
    stale: bool
    location_id: str  # the profile it is of (docs/multinegocio.md, 5.1)


class DiagnosisUnavailable(BaseModel):
    """There is nothing to diagnose yet. `detail` is ready to be shown, as in any error."""

    status: str  # pendiente, needs_reconnect or error
    detail: str
    location_id: str  # the profile it is of (docs/multinegocio.md, 5.1)


def to_response(result: Diagnosis, attribution: str = GOOGLE_MAPS_ATTRIBUTION) -> DiagnosisResponse:
    return DiagnosisResponse(
        place_id=result.place_id,
        name=result.name,
        address=result.address,
        category=result.category,
        score=result.score,
        label=result.label,
        findings=[
            FindingOut(code=f.code, title=f.title, detail=f.detail, impact=f.impact.value)
            for f in result.findings
        ],
        locked_checks=[LockedCheckOut(code=c.code, title=c.title) for c in result.locked_checks],
        maps_url=result.maps_url,
        attribution=attribution,
    )


def to_profile_response(
    view: SummaryView, holidays: Iterable[Holiday], today: date
) -> ProfileDiagnosisResponse | DiagnosisUnavailable:
    """The diagnosis of the facts the worker stored, or why there is none yet.

    A failed sync keeps the facts of the last good one: the diagnosis is still served,
    with the status of that failure, so that the web can say the data is old.
    """
    s = view.summary
    if s is None or s.facts is None:
        # A summary without facts was written before they existed: a sync is on its way.
        status = STATUS_PENDING if s is None or s.status == STATUS_OK else s.status
        return DiagnosisUnavailable(
            status=status,
            detail=UNAVAILABLE_DETAIL.get(status, UNAVAILABLE_DETAIL_ERROR),
            location_id=view.location_id,
        )
    result = diagnose_profile(
        ConnectedProfile(
            name=view.title,
            facts=s.facts,
            review_count=s.review_count or 0,
            average_rating=s.average_rating,
            unanswered_count=s.unanswered_count or 0,
            unanswered_low_count=s.unanswered_low_count or 0,
            has_regular_hours=bool(s.has_regular_hours),
            google_changed_fields=s.google_changed_fields,
            place_id=view.place_id or "",
        ),
        holidays,
        today,
    )
    return ProfileDiagnosisResponse(
        **to_response(result, PROFILE_ATTRIBUTION).model_dump(),
        status=s.status,
        synced_at=seconds(s.synced_at),
        stale=view.stale,
        location_id=view.location_id,
    )
