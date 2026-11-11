"""Summary of a Business Profile: what the owner should look at today. No I/O."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

STATUS_OK = "ok"
STATUS_NEEDS_RECONNECT = "needs_reconnect"
STATUS_ERROR = "error"

LOW_RATING_MAX = 2
TRANSLATED_MARK = "(Translated by Google)"
ORIGINAL_MARK = "(Original)"
_STARS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}


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
    special_dates: tuple[date, ...] = ()  # start date of every special-hours period


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
    )
