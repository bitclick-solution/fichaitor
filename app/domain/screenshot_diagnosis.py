"""What a screenshot of the reviews tab of a Google listing tells: pure rules, no I/O.

A reader (a vision model) reports the reviews visible in the image. Whatever it returns
is untrusted, because the text in the image is written by third parties: `parse_facts`
checks and bounds every value. The counts, the headline and the choice of the review to
answer come from fixed rules here. The only text a model writes is the suggested reply,
and `validate_reply` discards it unless it meets the rules.

The name of whoever wrote a review is never part of these facts.
"""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.domain.diagnosis import LOCKED_CHECKS, LockedCheck

# The rules of the suggested reply are shared with the replies to the reviews of a
# connected profile: they live in app.domain.review_reply and are re-exported here.
from app.domain.review_reply import (
    LOW_STARS,
    REPLY_MAX_LENGTH,
    REPLY_MAX_SENTENCES,
    REPLY_MIN_LENGTH,
    REPLY_MIN_SENTENCES,
    clean_text,
    validate_reply,
)

__all__ = [
    "LOW_STARS",
    "REPLY_MAX_LENGTH",
    "REPLY_MAX_SENTENCES",
    "REPLY_MIN_LENGTH",
    "REPLY_MIN_SENTENCES",
    "clean_text",
    "validate_reply",
]

MAX_REVIEWS = 10
MAX_REVIEW_TEXT_LENGTH = 600
MAX_AGE_LENGTH = 40
MAX_NAME_LENGTH = 120
EXCERPT_LENGTH = 200
DEFAULT_MIN_CONFIDENCE = 0.6
SCREENSHOT_ATTRIBUTION = "Según la captura que has enviado"
# "Cuántas reseñas tienes sin responder" is already the first of them: the capture only
# shows a handful, the total is only known by connecting the listing.
SCREENSHOT_LOCKED_CHECKS: tuple[LockedCheck, ...] = LOCKED_CHECKS


class Screen(StrEnum):
    REVIEWS = "reviews_tab"
    OVERVIEW = "profile_overview"
    OTHER = "other"


@dataclass(frozen=True)
class VisibleReview:
    """One review as seen in the capture. None means "not visible", never "no"."""

    stars: int | None = None
    text: str = ""  # many reviews are stars only
    age: str | None = None  # as shown: "hace 2 meses"
    has_owner_reply: bool | None = None


@dataclass(frozen=True)
class ScreenshotFacts:
    screen: Screen | None = None
    confidence: float | None = None
    business_name: str | None = None
    reviews: tuple[VisibleReview, ...] = ()


@dataclass(frozen=True)
class ReviewsSummary:
    business_name: str | None
    reviews_seen: int
    unanswered: int
    unanswered_low: int
    headline: str


class NotABusinessProfile(Exception):
    """The image is not a Google business listing, or the reader is not sure enough."""


class NotReviewsTab(Exception):
    """A Google listing, but not its reviews tab."""


class NoReviewsVisible(Exception):
    """The reviews tab, but no review can be made out."""


# --- bounding the reader's answer -------------------------------------------------------


def _flag(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _number(value: object) -> float | None:
    # bool is an int in Python: True must not become 1.0.
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _stars(value: object) -> int | None:
    number = _number(value)
    if number is None or number != int(number) or not 1 <= number <= 5:
        return None
    return int(number)


def _screen(value: object) -> Screen | None:
    try:
        return Screen(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _review(raw: object) -> VisibleReview | None:
    if not isinstance(raw, Mapping):
        return None
    review = VisibleReview(
        stars=_stars(raw.get("stars")),
        text=clean_text(raw.get("text"), MAX_REVIEW_TEXT_LENGTH),
        age=clean_text(raw.get("age"), MAX_AGE_LENGTH) or None,
        has_owner_reply=_flag(raw.get("has_owner_reply")),
    )
    # Neither stars nor text: there is nothing there to call a review.
    return review if review.stars is not None or review.text else None


def parse_facts(raw: object) -> ScreenshotFacts:
    """Turn the reader's output into facts. Never raises.

    Unknown keys are ignored (an author's name, a score, anything). A value of the wrong
    type or out of range becomes None: it is not clamped, because 100 stars say nothing
    about the real ones. Texts are cut and stripped of control characters, and at most
    MAX_REVIEWS reviews are kept.
    """
    if not isinstance(raw, Mapping):
        return ScreenshotFacts()
    listed = raw.get("reviews")
    reviews: list[VisibleReview] = []
    if isinstance(listed, list | tuple):
        for item in listed[: MAX_REVIEWS * 3]:  # bounded work even on a huge list
            review = _review(item)
            if review is not None:
                reviews.append(review)
            if len(reviews) == MAX_REVIEWS:
                break
    confidence = _number(raw.get("confidence"))
    return ScreenshotFacts(
        screen=_screen(raw.get("screen")),
        confidence=confidence if confidence is not None and 0 <= confidence <= 1 else None,
        business_name=clean_text(raw.get("business_name"), MAX_NAME_LENGTH) or None,
        reviews=tuple(reviews),
    )


# --- counts and headline ----------------------------------------------------------------


def _is_unanswered(review: VisibleReview) -> bool:
    # Only when the capture shows there is no reply. "Cannot tell" is not "unanswered".
    return review.has_owner_reply is False


def _headline(seen: int, unanswered: int, low: int, all_answered: bool) -> str:
    if unanswered == 0:
        if all_answered:
            if seen == 1:
                return "La reseña que veo está contestada."
            return "Todas las reseñas que veo están contestadas."
        if seen == 1:
            return "Veo 1 reseña y no distingo si está contestada."
        return f"Veo {seen} reseñas y no distingo ninguna sin contestar."
    if seen == 1:
        text = "Veo 1 reseña y está sin contestar."
    elif unanswered == seen:
        text = f"Veo {seen} reseñas y ninguna está contestada."
    elif unanswered == 1:
        text = f"Veo {seen} reseñas y 1 está sin contestar."
    else:
        text = f"Veo {seen} reseñas y {unanswered} están sin contestar."
    if low == 0:
        return text
    if unanswered == 1:
        return f"{text} Es de 1 o 2 estrellas."
    if low == 1:
        return f"{text} 1 de ellas es de 1 o 2 estrellas."
    return f"{text} {low} de ellas son de 1 o 2 estrellas."


def summarize(
    facts: ScreenshotFacts, min_confidence: float = DEFAULT_MIN_CONFIDENCE
) -> ReviewsSummary:
    if (
        facts.screen not in (Screen.REVIEWS, Screen.OVERVIEW)
        or facts.confidence is None
        or facts.confidence < min_confidence
    ):
        raise NotABusinessProfile
    if facts.screen is Screen.OVERVIEW:
        raise NotReviewsTab
    if not facts.reviews:
        raise NoReviewsVisible
    pending = [r for r in facts.reviews if _is_unanswered(r)]
    low = sum(1 for r in pending if r.stars is not None and r.stars <= LOW_STARS)
    seen = len(facts.reviews)
    all_answered = all(r.has_owner_reply is True for r in facts.reviews)
    return ReviewsSummary(
        business_name=facts.business_name,
        reviews_seen=seen,
        unanswered=len(pending),
        unanswered_low=low,
        headline=_headline(seen, len(pending), low, all_answered),
    )


# --- which review gets a suggested reply ------------------------------------------------

_UNIT_DAYS = {
    "minuto": 1 / 1440,
    "hora": 1 / 24,
    "día": 1,
    "dia": 1,
    "semana": 7,
    "mes": 30,
    "año": 365,
}
_AGE = re.compile(r"hace\s+(\d{1,3}|un|una)\s+(minuto|hora|día|dia|semana|mes|año)", re.IGNORECASE)


def age_in_days(age: str | None) -> float:
    """Days in "hace 2 meses". Infinite when it cannot be read: unknown is never "newest"."""
    if not age:
        return math.inf
    lowered = age.lower()
    if "ayer" in lowered:
        return 1.0
    match = _AGE.search(lowered)
    if match is None:
        return math.inf
    amount = 1 if match.group(1) in ("un", "una") else int(match.group(1))
    return amount * _UNIT_DAYS[match.group(2)]


def pick_review_to_answer(reviews: tuple[VisibleReview, ...]) -> VisibleReview | None:
    """The unanswered review with the fewest stars among those with text; if none has
    text, the newest. Reviews whose stars cannot be read are skipped: without them
    there is no telling whether to thank or to apologise. Ties go to the one shown first."""
    pending = [r for r in reviews if _is_unanswered(r) and r.stars is not None]
    with_text = [r for r in pending if r.text]
    if with_text:
        return min(with_text, key=lambda r: r.stars or 0)
    return min(pending, key=lambda r: age_in_days(r.age), default=None)


def excerpt(text: str, max_length: int = EXCERPT_LENGTH) -> str:
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "…"
