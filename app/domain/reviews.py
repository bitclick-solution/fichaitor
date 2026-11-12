"""Stored reviews: what can be kept of what Google returns. No I/O."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta

from app.domain.actions import GBP_REVIEW_ID
from app.domain.profile_summary import Review, ReviewSet

# For this long after Fichaitor sends a reply, a sync in which Google does not return
# it yet does not wipe it: Google reviews a reply before showing it.
REPLY_GRACE = timedelta(hours=24)
MAX_STORED_TEXT = 20000


def storable_text(text: str) -> str:
    """PostgreSQL text cannot hold NUL; anything absurdly long is cut."""
    return text.replace("\x00", "")[:MAX_STORED_TEXT]


def storable_reviews(reviews: Sequence[Review]) -> list[Review]:
    """The reviews that can be stored and addressed later: those with a well-formed
    Google id, each id once."""
    unique: dict[str, Review] = {}
    for review in reviews:
        if GBP_REVIEW_ID.fullmatch(review.review_id or ""):
            unique.setdefault(review.review_id, review)
    return list(unique.values())


def with_sent_replies(
    reviews: ReviewSet, sent: Mapping[str, tuple[str, datetime | None]]
) -> ReviewSet:
    """Google's reviews, with the replies Fichaitor sent a moment ago on top.

    `sent` maps Google's review id to the text and date of a reply sent within
    REPLY_GRACE. A review Google still lists as unanswered takes that reply; one Google
    already lists with a reply keeps Google's version, which is the newer truth.
    """
    if not sent:
        return reviews
    merged = tuple(
        replace(r, replied=True, reply_text=sent[r.review_id][0], reply_at=sent[r.review_id][1])
        if not r.replied and r.review_id in sent
        else r
        for r in reviews.reviews
    )
    return replace(reviews, reviews=merged)
