"""Shape of a diagnosis in the API. Shared by the public route and the one behind a session."""

from pydantic import BaseModel

from app.domain.diagnosis import Diagnosis

# Shown by the web next to the data: Google requires its content to be attributed.
GOOGLE_MAPS_ATTRIBUTION = "Google Maps"


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


def to_response(result: Diagnosis) -> DiagnosisResponse:
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
        attribution=GOOGLE_MAPS_ATTRIBUTION,
    )
