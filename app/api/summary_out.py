"""Shape of GET /api/v1/me/summary. Documented in docs/contrato-web-api.md."""

from datetime import UTC, date, datetime

from pydantic import BaseModel

from app.application.profile_sync import SummaryView
from app.domain.profile_summary import FIELD_LABELS, field_label

__all__ = [
    "FIELD_LABELS",
    "STATUS_PENDING",
    "SummaryResponse",
    "field_label",
    "seconds",
    "to_summary_response",
]

STATUS_PENDING = "pendiente"


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
    # The profile it is of (docs/multinegocio.md, 5.1): the web drops an answer that is
    # not of the profile it has selected.
    location_id: str


def seconds(moment: datetime | None) -> datetime | None:
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
            location_id=view.location_id,
        )
    fields = list(s.google_changed_fields)
    return SummaryResponse(
        status=s.status,
        synced_at=seconds(s.synced_at),
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
        location_id=view.location_id,
    )
