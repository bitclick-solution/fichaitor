"""Summary of a Business Profile: what the owner should look at today. No I/O."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

STATUS_OK = "ok"
STATUS_NEEDS_RECONNECT = "needs_reconnect"
STATUS_ERROR = "error"

LOW_RATING_MAX = 2
TRANSLATED_MARK = "(Translated by Google)"
ORIGINAL_MARK = "(Original)"
_STARS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}

FACTS_VERSION = 1
MAX_STORED_SPECIAL_DATES = 400

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


def field_label(field: str) -> str:
    """Spanish label of a changed field. "phoneNumbers.primaryPhone" reads as its parent;
    an unknown field is returned as it came."""
    return FIELD_LABELS.get(field) or FIELD_LABELS.get(field.split(".", 1)[0]) or field


@dataclass(frozen=True)
class Review:
    rating: int | None  # 1 to 5; None when Google sends no stars
    created_at: datetime | None
    replied: bool
    text: str = ""  # original language, see original_text


@dataclass(frozen=True)
class ReviewSet:
    reviews: tuple[Review, ...]
    total: int | None = None  # Google's own count, when it sends one
    average: float | None = None


@dataclass(frozen=True)
class ProfileHours:
    has_regular_hours: bool
    special_dates: tuple[date, ...] = ()  # every day covered by a special-hours period


@dataclass(frozen=True)
class ProfileDetails:
    """One read of the location resource of Business Information."""

    hours: ProfileHours
    title: str = ""
    address: str = ""
    category: str = ""  # display name of the primary category
    additional_categories: int = 0
    description_length: int = 0  # of the owner's description; the text itself is not kept
    has_phone: bool = False
    has_website: bool = False
    service_count: int = 0
    is_open: bool = True  # False when the owner has marked the business as closed
    place_id: str = ""
    maps_url: str = ""


@dataclass(frozen=True)
class MediaSummary:
    count: int  # media uploaded by the owner
    latest_at: datetime | None = None  # None: no dates, or not every item was read


@dataclass(frozen=True)
class ProfileFacts:
    """What the diagnosis of a connected profile needs, besides the figures of the summary.

    Stored as it is (profile_summary.facts). Only facts about the business itself:
    no review texts, no authors and no description text.
    """

    title: str = ""
    address: str = ""
    category: str = ""
    additional_categories: int = 0
    description_length: int = 0
    has_phone: bool = False
    has_website: bool = False
    service_count: int = 0
    is_open: bool = True
    place_id: str = ""
    maps_url: str = ""
    special_dates: tuple[date, ...] = ()  # days with special hours, from the sync day on
    # None: Google did not let us read it, so the diagnosis says nothing about it.
    photo_count: int | None = None
    latest_photo_at: datetime | None = None
    attribute_count: int | None = None


@dataclass(frozen=True)
class ProfileSummary:
    review_count: int
    average_rating: float | None
    unanswered_count: int
    unanswered_low_count: int
    last_review_at: datetime | None
    has_regular_hours: bool
    upcoming_special_hours: int
    google_changed: bool
    google_changed_fields: tuple[str, ...]
    facts: ProfileFacts | None = None


def original_text(comment: str | None) -> str:
    """Google sends translated reviews as "(Translated by Google) … (Original) …"
    (or the original first and the translation after). Keep only the original."""
    text = comment or ""
    if ORIGINAL_MARK in text:
        text = text.rsplit(ORIGINAL_MARK, 1)[1]
    elif TRANSLATED_MARK in text:
        text = text.split(TRANSLATED_MARK, 1)[0]
    return text.strip()


def star_rating(value: str | None) -> int | None:
    return _STARS.get(value or "")


def parse_diff_mask(mask: str | None) -> tuple[str, ...]:
    """Field mask ("a,b") to a tuple of unique field names, order kept."""
    fields = (part.strip() for part in (mask or "").split(","))
    return tuple(dict.fromkeys(f for f in fields if f))


def summarize(
    reviews: ReviewSet,
    hours: ProfileHours,
    google_changed_fields: Sequence[str],
    today: date,
    facts: ProfileFacts | None = None,
) -> ProfileSummary:
    items = reviews.reviews
    unanswered = [r for r in items if not r.replied]
    ratings = [r.rating for r in items if r.rating is not None]
    dates = [r.created_at for r in items if r.created_at is not None]

    average = reviews.average
    if average is None and ratings:
        average = sum(ratings) / len(ratings)

    changed = tuple(google_changed_fields)
    return ProfileSummary(
        review_count=reviews.total if reviews.total is not None else len(items),
        average_rating=round(average, 2) if average is not None else None,
        unanswered_count=len(unanswered),
        unanswered_low_count=sum(
            1 for r in unanswered if r.rating is not None and r.rating <= LOW_RATING_MAX
        ),
        last_review_at=max(dates) if dates else None,
        has_regular_hours=hours.has_regular_hours,
        upcoming_special_hours=len({d for d in hours.special_dates if d >= today}),
        google_changed=bool(changed),
        google_changed_fields=changed,
        facts=facts,
    )


def collect_facts(
    details: ProfileDetails,
    media: MediaSummary | None,
    attribute_count: int | None,
    today: date,
) -> ProfileFacts:
    """The facts to store after a sync. Past special hours are dropped."""
    upcoming = sorted({d for d in details.hours.special_dates if d >= today})
    return ProfileFacts(
        title=details.title,
        address=details.address,
        category=details.category,
        additional_categories=details.additional_categories,
        description_length=details.description_length,
        has_phone=details.has_phone,
        has_website=details.has_website,
        service_count=details.service_count,
        is_open=details.is_open,
        place_id=details.place_id,
        maps_url=details.maps_url,
        special_dates=tuple(upcoming[:MAX_STORED_SPECIAL_DATES]),
        photo_count=media.count if media is not None else None,
        latest_photo_at=media.latest_at if media is not None else None,
        attribute_count=attribute_count,
    )


def facts_to_json(facts: ProfileFacts) -> dict[str, Any]:
    return {
        "v": FACTS_VERSION,
        "title": facts.title,
        "address": facts.address,
        "category": facts.category,
        "additional_categories": facts.additional_categories,
        "description_length": facts.description_length,
        "has_phone": facts.has_phone,
        "has_website": facts.has_website,
        "service_count": facts.service_count,
        "is_open": facts.is_open,
        "place_id": facts.place_id,
        "maps_url": facts.maps_url,
        "special_dates": [d.isoformat() for d in facts.special_dates],
        "photo_count": facts.photo_count,
        "latest_photo_at": facts.latest_photo_at.isoformat() if facts.latest_photo_at else None,
        "attribute_count": facts.attribute_count,
    }


def _typed(raw: dict, key: str, kind: type, *, optional: bool = False) -> Any:
    value = raw[key]
    if value is None and optional:
        return None
    # A bool is an int for Python: a count must not be read from true or false.
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise TypeError(key)
    if kind is int and value < 0:
        raise ValueError(key)
    return value


def facts_from_json(raw: object) -> ProfileFacts | None:
    """The stored facts. None when they are missing, of another version or malformed:
    the profile then counts as not read yet and the next sync writes them again."""
    if not isinstance(raw, dict) or raw.get("v") != FACTS_VERSION:
        return None
    try:
        latest = _typed(raw, "latest_photo_at", str, optional=True)
        return ProfileFacts(
            title=_typed(raw, "title", str),
            address=_typed(raw, "address", str),
            category=_typed(raw, "category", str),
            additional_categories=_typed(raw, "additional_categories", int),
            description_length=_typed(raw, "description_length", int),
            has_phone=_typed(raw, "has_phone", bool),
            has_website=_typed(raw, "has_website", bool),
            service_count=_typed(raw, "service_count", int),
            is_open=_typed(raw, "is_open", bool),
            place_id=_typed(raw, "place_id", str),
            maps_url=_typed(raw, "maps_url", str),
            special_dates=tuple(date.fromisoformat(d) for d in _typed(raw, "special_dates", list)),
            photo_count=_typed(raw, "photo_count", int, optional=True),
            latest_photo_at=datetime.fromisoformat(latest) if latest else None,
            attribute_count=_typed(raw, "attribute_count", int, optional=True),
        )
    except (KeyError, TypeError, ValueError):
        return None
