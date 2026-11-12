"""Shape of GET /api/v1/me/sentiment. Documented in docs/contrato-web-api.md.

Everything here was computed and stored by the worker: the API only reads it and adds
the comparison with the previous month.
"""

from datetime import datetime

from pydantic import BaseModel

from app.api.summary_out import seconds
from app.domain.sentiment import (
    STATUS_ERROR,
    STATUS_INSUFFICIENT,
    STATUS_PENDING,
    SentimentPolicy,
    month_of,
    next_update_text,
)
from app.ports.store import StoredSentiment

NEXT_UPDATE_NO_REVIEWS = "Se actualiza en cuanto tu ficha reciba su primera reseña."


class TopicOut(BaseModel):
    topic: str
    mentions: int


class MonthOut(BaseModel):
    month: str  # "2026-11"
    score: int


class SentimentResponse(BaseModel):
    status: str
    based_on: int | None
    with_text: int | None
    period: str | None
    overall: str | None
    score: int | None
    previous_score: int | None
    delta: int | None
    history: list[MonthOut]
    summary: str | None
    praised: list[TopicOut]
    complaints: list[TopicOut]
    suggestion: str | None
    generated_at: datetime | None
    next_update: str | None
    provisional: bool


def empty_response(status: str) -> SentimentResponse:
    """Nothing to show yet (pendiente) or nothing that could be computed (error)."""
    return SentimentResponse(
        status=status,
        based_on=None,
        with_text=None,
        period=None,
        overall=None,
        score=None,
        previous_score=None,
        delta=None,
        history=[],
        summary=None,
        praised=[],
        complaints=[],
        suggestion=None,
        generated_at=None,
        next_update=None,
        provisional=False,
    )


def to_sentiment_response(
    stored: StoredSentiment | None, policy: SentimentPolicy
) -> SentimentResponse:
    if stored is None:
        return empty_response(STATUS_PENDING)
    analysis = stored.analysis
    figures = analysis.figures
    if figures.status == STATUS_ERROR:
        return empty_response(STATUS_ERROR)
    if figures.status == STATUS_INSUFFICIENT:
        response = empty_response(STATUS_INSUFFICIENT)
        return response.model_copy(
            update={
                "based_on": 0,
                "with_text": 0,
                "summary": analysis.summary,
                "suggestion": analysis.suggestion,
                "generated_at": seconds(analysis.generated_at),
                "next_update": NEXT_UPDATE_NO_REVIEWS,
                "provisional": True,
            }
        )
    # The previous month is the latest one with a snapshot before the month of this
    # analysis. Without one there is nothing to compare with.
    this_month = month_of(analysis.generated_at)
    earlier = [score for month, score in stored.history if month < this_month]
    previous = earlier[-1] if earlier else None
    return SentimentResponse(
        status=figures.status,
        based_on=figures.based_on,
        with_text=figures.with_text,
        period=figures.period,
        overall=figures.overall,
        score=figures.score,
        previous_score=previous,
        delta=figures.score - previous if previous is not None else None,
        history=[
            MonthOut(month=f"{month.year:04d}-{month.month:02d}", score=score)
            for month, score in stored.history
        ],
        summary=analysis.summary,
        praised=[TopicOut(topic=t.topic, mentions=t.mentions) for t in figures.praised],
        complaints=[TopicOut(topic=t.topic, mentions=t.mentions) for t in figures.complaints],
        suggestion=analysis.suggestion,
        generated_at=seconds(analysis.generated_at),
        next_update=next_update_text(policy),
        provisional=figures.provisional,
    )
