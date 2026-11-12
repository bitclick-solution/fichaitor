"""The model behind the sentiment analysis: it classifies reviews and words a summary."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol


class SentimentModelUnavailable(Exception):
    """The model failed, timed out or answered something unusable. The message is a
    fixed string: never a review, a summary or anything the provider said."""


@dataclass(frozen=True)
class ModelUsage:
    """Tokens of one call, when the provider reports them."""

    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class ReviewToClassify:
    """All a model gets of a review: a number that only means something inside the
    call, the stars and the text. No author, no business, no identifiers of ours."""

    number: int
    stars: int  # 0: unknown
    text: str


@dataclass(frozen=True)
class SummaryRequest:
    """What the summary is written from: figures the code computed and a few excerpts."""

    score: int
    overall: str
    based_on: int
    praised: Sequence[tuple[str, int]]  # topic, reviews that mention it
    complaints: Sequence[tuple[str, int]]
    excerpts: Sequence[tuple[int, str]]  # stars, text
    # What the topics do not show, counted by the code.
    low_without_text: int = 0  # ratings of 1 or 2 stars without a comment
    unanswered: int = 0  # reviews without a reply of the owner
    unanswered_low: int = 0  # of those, the ones of 1 or 2 stars


class SentimentModel(Protocol):
    """Both answers are data written from what third parties wrote: callers must pass
    them through `parse_classification`, `validate_summary` and `validate_suggestion`
    in `app.domain.sentiment`. A model decides nothing and counts nothing."""

    async def classify(
        self, reviews: Sequence[ReviewToClassify], topics: Sequence[str]
    ) -> tuple[Mapping[int, object], ModelUsage]:
        """Raw classification of each review, by its number. A review may be missing."""
        ...

    async def summarize(self, request: SummaryRequest) -> tuple[Mapping[str, object], ModelUsage]:
        """Raw texts: {"summary": ..., "suggestion": ...}. The suggestion is used only
        where the fixed rules of the domain leave room for it."""
        ...
