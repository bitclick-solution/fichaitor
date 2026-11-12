"""Job analyze_sentiment: how people see the business, from the stored reviews.

Payload: {"google_sub": "...", "location": "locations/123"}. Enqueued by sync_profile
when a sync ends and an analysis is due (`SentimentTrigger`).

What it does, in order:

1. Decides again whether an analysis is due (`app.domain.sentiment.decide`). If not,
   it ends without doing anything: running the job twice costs nothing.
2. Classifies by its stars every new review without text. No model.
3. Sends to the model, in batches, the reviews with text that are new, changed or still
   pending: stars and text, nothing else. Each answer is accepted only if every value is
   one of the closed ones; otherwise that review stays pending for the next analysis.
   A failed call leaves its batch pending and never fails the job.
4. Computes the score, the verdict and the counts per topic in code.
5. Asks the model for the summary and a suggestion, only when there are enough
   reviews with text, and keeps the summary only if it passes the checks of the
   domain; otherwise the one built by fixed rules is used. The code adds the sentence
   about low ratings without a comment and chooses the suggestion by fixed rules,
   unanswered reviews first (`choose_suggestion`).
6. Stores the analysis, the score of the month and the usage (counters, no text).

A review is data written by a third party: nothing in it is acted upon. Neither the
reviews nor the summary appear in a log or in an error message.
"""

import dataclasses
import hashlib
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from app.domain.jobs import Job, PermanentJobError
from app.domain.review_reply import clean_text
from app.domain.sentiment import (
    CLASSIFY_TEXT_MAX_CHARS,
    MAX_CLASSIFY_ATTEMPTS,
    METHOD_FALLBACK,
    METHOD_MODEL,
    METHOD_PENDING,
    METHOD_STARS,
    SOURCE_MODEL,
    SOURCE_RULES,
    STATUS_OK,
    Analysis,
    Figures,
    SentimentPolicy,
    SentimentReview,
    StoredClassification,
    Usage,
    all_triggers,
    analysis_state,
    attention_of,
    choose_suggestion,
    complete_summary,
    compute,
    current,
    decide,
    has_text,
    needs_model,
    parse_classification,
    pick_excerpts,
    rule_summary,
    sector_of,
    source_hash,
    stars_sentiment,
    topics_for,
    validate_suggestion,
    validate_summary,
)
from app.ports.job_queue import JobEnqueuer
from app.ports.sentiment_model import (
    ModelUsage,
    ReviewToClassify,
    SentimentModel,
    SentimentModelUnavailable,
    SummaryRequest,
)
from app.ports.sentiment_store import SentimentStore
from app.worker import log

KIND = "analyze_sentiment"
MAX_ATTEMPTS = 3

PlanPolicy = Callable[[str], bool]


def _target(job: Job) -> tuple[str, str]:
    google_sub = job.payload.get("google_sub")
    location = job.payload.get("location")
    if not (isinstance(google_sub, str) and google_sub and isinstance(location, str) and location):
        raise PermanentJobError("payload no válido: faltan google_sub o location")
    return google_sub, location


class _Meter:
    """Counts the calls and adds up the tokens of one analysis."""

    def __init__(self) -> None:
        self.classify_calls = 0
        self.summary_calls = 0
        self.failed_calls = 0
        self.reviews_sent = 0
        self._input: int | None = None
        self._output: int | None = None

    @property
    def calls(self) -> int:
        return self.classify_calls + self.summary_calls

    def add(self, usage: ModelUsage) -> None:
        if usage.input_tokens is not None:
            self._input = (self._input or 0) + usage.input_tokens
        if usage.output_tokens is not None:
            self._output = (self._output or 0) + usage.output_tokens

    def usage(self) -> Usage:
        return Usage(
            classify_calls=self.classify_calls,
            summary_calls=self.summary_calls,
            failed_calls=self.failed_calls,
            reviews_sent=self.reviews_sent,
            input_tokens=self._input,
            output_tokens=self._output,
        )


class AnalyzeSentiment:
    def __init__(
        self,
        store: SentimentStore,
        model: SentimentModel | None,
        policy: SentimentPolicy,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        event_triggers: PlanPolicy = all_triggers,
    ) -> None:
        self._store = store
        # None: no model configured. Every review then counts by its stars and the
        # texts are the ones built by rules; nothing is marked as classified by a model.
        self._model = model
        self._policy = policy
        self._clock = clock
        self._event_triggers = event_triggers

    async def __call__(self, job: Job) -> None:
        google_sub, location = _target(job)
        if await self._store.is_suspended(google_sub):
            # A scheduled leave: no review of that account goes to the model.
            log.event("account_suspended", job_id=job.id, kind=job.kind)
            return
        now = self._clock()
        context = await self._store.sentiment_context(google_sub, location)
        reviews = await self._store.sentiment_reviews(google_sub, location)
        reason = decide(
            analysis_state(reviews, context.last),
            now,
            self._policy,
            event_triggers=self._event_triggers(google_sub),
        )
        if reason is None:
            log.event("sentiment_skipped", job_id=job.id)
            return

        meter = _Meter()
        topics = topics_for(sector_of(context.category))
        classified = self._by_stars(reviews)
        classified.update(await self._by_model(reviews, topics, meter))
        if classified:
            await self._store.save_classifications(classified, now)
            reviews = [
                dataclasses.replace(r, stored=classified.get(r.id, r.stored)) for r in reviews
            ]

        figures = compute(reviews, now, self._policy)
        summary, suggestion, source = await self._texts(figures, reviews, now, meter)
        analysis = Analysis(figures, summary, suggestion, source, reason, now)
        await self._store.save_analysis(google_sub, location, analysis, meter.usage())
        usage = meter.usage()
        log.event(
            "sentiment_analyzed",
            job_id=job.id,
            trigger=reason,
            status=figures.status,
            based_on=figures.based_on,
            with_text=figures.with_text,
            provisional=figures.provisional,
            score=figures.score,
            texts=source,
            classify_calls=usage.classify_calls,
            summary_calls=usage.summary_calls,
            failed_calls=usage.failed_calls,
            reviews_sent=usage.reviews_sent,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )

    @staticmethod
    def _by_stars(reviews: list[SentimentReview]) -> dict[str, StoredClassification]:
        """New reviews without text: the fixed rule, no model."""
        found: dict[str, StoredClassification] = {}
        for review in reviews:
            sentiment = stars_sentiment(review.rating)
            if not has_text(review) and sentiment is not None and current(review) is None:
                found[review.id] = StoredClassification(
                    source_hash(review.rating, review.text), sentiment, (), METHOD_STARS, 0
                )
        return found

    async def _by_model(
        self, reviews: list[SentimentReview], topics: tuple[str, ...], meter: _Meter
    ) -> dict[str, StoredClassification]:
        """Reviews with text that need a model, newest first, in batches. One call is
        kept back for the summary. What does not fit waits for the next analysis."""
        if self._model is None:
            return {}
        waiting = sorted(
            (r for r in reviews if needs_model(r)),
            key=lambda r: (r.created_at or datetime.min.replace(tzinfo=UTC), r.id),
            reverse=True,
        )
        found: dict[str, StoredClassification] = {}
        size = self._policy.batch_size
        budget = max(self._policy.max_calls - 1, 0)
        for start in range(0, len(waiting), size):
            if meter.classify_calls >= budget:
                break
            batch = waiting[start : start + size]
            request = [
                ReviewToClassify(number, r.rating or 0, clean_text(r.text, CLASSIFY_TEXT_MAX_CHARS))
                for number, r in enumerate(batch, start=1)
            ]
            meter.classify_calls += 1
            meter.reviews_sent += len(batch)
            answers: dict = {}
            try:
                raw, usage = await self._model.classify(request, topics)
                meter.add(usage)
                answers = dict(raw) if isinstance(raw, Mapping) else {}
            except SentimentModelUnavailable as exc:
                meter.failed_calls += 1
                # A fixed string of the adapter: never a review or the provider's text.
                log.event("sentiment_classify_failed", logging.WARNING, error=str(exc))
            for number, review in enumerate(batch, start=1):
                found[review.id] = _outcome(review, answers.get(number), topics)
        return found

    async def _texts(
        self, figures: Figures, reviews: list[SentimentReview], now: datetime, meter: _Meter
    ) -> tuple[str, str, str]:
        """Summary, suggestion and where the summary comes from.

        The summary is the model's if it passes every check, else the one built by
        rules; the code adds the sentence about low ratings without a comment. The
        suggestion is chosen by fixed rules: the model's is used only where they leave
        room for it. The model is not asked when the analysis is provisional."""
        attention = attention_of(reviews)
        summary = rule_summary(figures, attention)
        suggestion = choose_suggestion(figures, attention)
        ask = (
            self._model is not None
            and figures.status == STATUS_OK
            and not figures.provisional
            and meter.calls < self._policy.max_calls
        )
        if not ask:
            return summary, suggestion, SOURCE_RULES
        request = SummaryRequest(
            score=figures.score,
            overall=figures.overall,
            based_on=figures.based_on,
            praised=[(t.topic, t.mentions) for t in figures.praised],
            complaints=[(t.topic, t.mentions) for t in figures.complaints],
            excerpts=pick_excerpts(reviews, now, self._policy),
            low_without_text=attention.low_without_text,
            unanswered=attention.unanswered,
            unanswered_low=attention.unanswered_low,
        )
        meter.summary_calls += 1
        try:
            raw, usage = await self._model.summarize(request)
            meter.add(usage)
        except SentimentModelUnavailable as exc:
            meter.failed_calls += 1
            log.event("sentiment_summary_failed", logging.WARNING, error=str(exc))
            return summary, suggestion, SOURCE_RULES
        texts = raw if isinstance(raw, Mapping) else {}
        written = validate_summary(texts.get("summary"), figures, self._policy, attention)
        advised = validate_suggestion(texts.get("suggestion"), figures, self._policy, attention)
        # The suggestion of the model stands on its own checks, whatever the summary did.
        suggestion = choose_suggestion(figures, attention, advised)
        if written is None:
            log.event("sentiment_summary_discarded", logging.WARNING)
            return summary, suggestion, SOURCE_RULES
        return complete_summary(written, attention), suggestion, SOURCE_MODEL

    async def on_exhausted(self, job: Job) -> None:
        """Out of attempts: say so, unless there is an earlier analysis to keep showing."""
        try:
            google_sub, location = _target(job)
        except PermanentJobError:
            return
        await self._store.save_analysis_error(google_sub, location, self._clock())


def _outcome(
    review: SentimentReview, answer: object, topics: tuple[str, ...]
) -> StoredClassification:
    """What is stored for a review that was sent to the model."""
    digest = source_hash(review.rating, review.text)
    earlier = current(review)
    attempts = (earlier.attempts if earlier is not None else 0) + 1
    parsed = parse_classification(answer, topics)
    if parsed is not None:
        return StoredClassification(digest, parsed.sentiment, parsed.topics, METHOD_MODEL, attempts)
    by_stars = stars_sentiment(review.rating)
    if attempts >= MAX_CLASSIFY_ATTEMPTS and by_stars is not None:
        # Refused or unusable every time: its stars decide, and it is not sent again.
        return StoredClassification(digest, by_stars, (), METHOD_FALLBACK, attempts)
    return StoredClassification(digest, None, (), METHOD_PENDING, attempts)


def _profile_hash(google_sub: str, location: str) -> str:
    return hashlib.sha256(f"{google_sub}\n{location}".encode()).hexdigest()[:32]


class SentimentTrigger:
    """Run by sync_profile once the reviews are stored: enqueues the analysis when one
    is due. The first analysis is enqueued at once, so that it is ready seconds after
    the first sync. Never raises: a sync must not fail because of this."""

    def __init__(
        self,
        store: SentimentStore,
        jobs: JobEnqueuer,
        policy: SentimentPolicy,
        *,
        kind: str = KIND,
        event_triggers: PlanPolicy = all_triggers,
    ) -> None:
        self._store = store
        self._jobs = jobs
        self._policy = policy
        self._kind = kind
        self._event_triggers = event_triggers

    async def after_sync(self, google_sub: str, location: str, now: datetime) -> str | None:
        """The reason an analysis was enqueued for, or None."""
        try:
            context = await self._store.sentiment_context(google_sub, location)
            reviews = await self._store.sentiment_reviews(google_sub, location)
            reason = decide(
                analysis_state(reviews, context.last),
                now,
                self._policy,
                event_triggers=self._event_triggers(google_sub),
            )
            if reason is None:
                return None
            # One job per profile and minute: an idempotency key is unique for ever, and
            # the job decides again when it runs, so a second one does nothing.
            bucket = int(now.timestamp() // 60)
            await self._jobs.enqueue(
                self._kind,
                {"google_sub": google_sub, "location": location},
                idempotency_key=f"{self._kind}:{_profile_hash(google_sub, location)}:{bucket}",
                max_attempts=MAX_ATTEMPTS,
            )
            return reason
        except Exception as exc:  # identifiers and texts stay out of the logs
            log.event("sentiment_not_enqueued", logging.ERROR, error=type(exc).__name__)
            return None
