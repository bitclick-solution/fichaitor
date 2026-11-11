"""Shape of GET /api/v1/me/summary. Documented in docs/contrato-web-api.md."""

from datetime import UTC, date, datetime

from pydantic import BaseModel

from app.application.profile_sync import SummaryView

STATUS_PENDING = "pendiente"

# Top-level fields of a Business Profile location, as they come in Google's diffMask.
FIELD_LABELS = {
    "title": "Nombre",
    "phoneNumbers": "Teléfono",
    "websiteUri": "Web",
    "categories": "Categorías",
    "regularHours": "Horario",
    "specialHours": "Horarios especiales",
    "moreHours": "Otros horarios",
    "storefrontAddress": "Dirección",
    "serviceArea": "Zona de servicio",
    "latlng": "Ubicación en el mapa",
    "openInfo": "Abierto o cerrado",
    "profile": "Descripción",
    "serviceItems": "Servicios",
    "labels": "Etiquetas",
}


class SummaryResponse(BaseModel):
    status: str
    synced_at: datetime | None
    review_count: int | None
    average_rating: float | None
    unanswered_count: int | None
    unanswered_low_count: int | None
    last_review_at: date | None
    has_regular_hours: bool | None
    upcoming_special_hours: int | None
    google_changed: bool | None
    google_changed_fields: list[str] | None
    google_changed_labels: list[str] | None
    stale: bool


def field_label(field: str) -> str:
    """Spanish label of a changed field. "phoneNumbers.primaryPhone" reads as its parent;
    an unknown field is returned as it came."""
    return FIELD_LABELS.get(field) or FIELD_LABELS.get(field.split(".", 1)[0]) or field


def _seconds(moment: datetime | None) -> datetime | None:
    return moment.astimezone(UTC).replace(microsecond=0) if moment else None


def to_summary_response(view: SummaryView) -> SummaryResponse:
    s = view.summary
    if s is None:
        return SummaryResponse(
            status=STATUS_PENDING,
            synced_at=None,
            review_count=None,
            average_rating=None,
            unanswered_count=None,
            unanswered_low_count=None,
            last_review_at=None,
            has_regular_hours=None,
            upcoming_special_hours=None,
            google_changed=None,
            google_changed_fields=None,
            google_changed_labels=None,
            stale=False,
        )
    fields = list(s.google_changed_fields)
    return SummaryResponse(
        status=s.status,
        synced_at=_seconds(s.synced_at),
        review_count=s.review_count,
        average_rating=s.average_rating,
        unanswered_count=s.unanswered_count,
        unanswered_low_count=s.unanswered_low_count,
        last_review_at=s.last_review_at.astimezone(UTC).date() if s.last_review_at else None,
        has_regular_hours=s.has_regular_hours,
        upcoming_special_hours=s.upcoming_special_hours,
        google_changed=s.google_changed,
        google_changed_fields=fields,
        google_changed_labels=[field_label(f) for f in fields],
        stale=view.stale,
    )
