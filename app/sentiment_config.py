"""Settings of the sentiment analysis. Read by the worker (which runs it) and by the
API (which only words when the next one is due)."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.sentiment import SentimentPolicy


class SentimentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FICHAITOR_SENTIMENT_", env_file=".env", extra="ignore"
    )

    # Main window of the score: older reviews weigh less.
    window_months: int = Field(default=12, ge=1, le=120)
    # Fewer reviews with text than this: the analysis is provisional and no model
    # writes its summary.
    min_text_reviews: int = Field(default=5, ge=1)
    # Score from which the verdict is "positivo" and "mixto"; below, "negativo".
    positive_min_score: int = Field(default=70, ge=0, le=100)
    mixed_min_score: int = Field(default=40, ge=0, le=100)
    # Recomputed with this many new reviews, with one of 1 or 2 stars, or after this
    # many days if there is at least one new review. Never more often than every
    # min_hours_between, except the first analysis.
    new_reviews_trigger: int = Field(default=3, ge=1)
    monthly_days: int = Field(default=30, ge=1)
    min_hours_between: float = Field(default=24.0, ge=0)
    # Cost: reviews per call to the model and calls per analysis (the summary is one).
    batch_size: int = Field(default=30, ge=1, le=100)
    max_calls: int = Field(default=6, ge=1, le=50)
    # Per call. max_calls x timeout must stay under the worker's job timeout.
    timeout_seconds: float = Field(default=15.0, gt=0)

    def policy(self) -> SentimentPolicy:
        return SentimentPolicy(
            window_months=self.window_months,
            min_text_reviews=self.min_text_reviews,
            positive_min_score=self.positive_min_score,
            mixed_min_score=self.mixed_min_score,
            new_reviews_trigger=self.new_reviews_trigger,
            monthly_days=self.monthly_days,
            min_hours_between=self.min_hours_between,
            batch_size=self.batch_size,
            max_calls=self.max_calls,
        )
