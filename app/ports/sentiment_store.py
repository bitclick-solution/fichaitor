"""What the sentiment analysis reads and writes in the database (worker's side)."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.domain.sentiment import (
    Analysis,
    LastAnalysis,
    SentimentReview,
    StoredClassification,
    Usage,
)


@dataclass(frozen=True)
class SentimentContext:
    last: LastAnalysis | None  # None: that profile has never been analysed
    category: str = ""  # primary category of the profile, to choose the list of topics


class SentimentStore(Protocol):
    async def sentiment_context(self, google_sub: str, location: str) -> SentimentContext: ...

    async def sentiment_reviews(self, google_sub: str, location: str) -> list[SentimentReview]:
        """The live reviews of that profile, each with its stored classification."""
        ...

    async def save_classifications(
        self, classified: Mapping[str, StoredClassification], at: datetime
    ) -> None:
        """Insert or replace the classification of each review, by our review id."""
        ...

    async def save_analysis(
        self, google_sub: str, location: str, analysis: Analysis, usage: Usage
    ) -> None:
        """Replace the analysis of that profile, set the score of its month in the
        history and add a row of usage, all or nothing. No review text is stored."""
        ...

    async def save_analysis_error(self, google_sub: str, location: str, at: datetime) -> None:
        """Record that the analysis failed, unless that profile already has one: the
        last good analysis is never replaced by an error."""
        ...
