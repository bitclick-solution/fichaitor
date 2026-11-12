"""Proposing new reviews by the messaging channels (docs/canales.md, step 5).

Two halves, in two processes:

- `ReviewProposals`, in the worker: after a sync, one `propose_review` job per review
  stored for the first time without a reply and written in the last 7 days. It never
  raises: a review that is not proposed stays in the panel.
- `ProposeReview`, the job, in the conversation process: for every active profile that
  has that review (`ReviewTarget.targets`: of an account without a scheduled leave, whose
  last sync is "ok" and that had an address that sees that profile linked before the
  review was written), it asks for the draft exactly as the panel does
  (`ReviewReplies.draft`: the stored draft, the cap per profile, `validate_reply`) and
  hands it to the conversation (`submit_review`, with the profile), which proposes it
  once per account and review, in the lane of that profile.

The review is data written by a third party: it is shown to the owner and passed to
the model as data, never read to decide anything. Without a valid draft the review is
not proposed by the channel; it stays in the panel.
"""

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from app.application.conversation_service import ConversationService, ReviewInput
from app.application.review_replies import (
    DraftRateLimited,
    DraftRejected,
    DraftUnavailable,
    ReviewNotFound,
    ReviewReplies,
)
from app.domain.channel_jobs import (
    PROPOSE_MAX_ATTEMPTS,
    PROPOSE_REVIEW,
    PROPOSE_REVIEWS_NEWER_THAN,
    REVIEW_ID,
    propose_key,
)
from app.domain.jobs import Job, JobError, PermanentJobError
from app.ports.conversation_store import ReviewTargets
from app.ports.job_queue import JobEnqueuer
from app.ports.worker_store import NewReview
from app.worker import log

KIND = PROPOSE_REVIEW


class ReviewProposals:
    """Worker side: enqueue the proposals of the new reviews of a sync."""

    def __init__(self, jobs: JobEnqueuer) -> None:
        self._jobs = jobs

    async def after_sync(self, new_reviews: Sequence[NewReview], now: datetime) -> None:
        for review in new_reviews:
            # The history is not proposed: only what was written in the last days.
            if review.created_at is None or review.created_at < now - PROPOSE_REVIEWS_NEWER_THAN:
                continue
            try:
                await self._jobs.enqueue(
                    KIND,
                    {"review_id": review.id},
                    idempotency_key=propose_key(review.id),
                    max_attempts=PROPOSE_MAX_ATTEMPTS,
                )
            except Exception as exc:
                log.event("propose_review_not_enqueued", logging.WARNING, error=type(exc).__name__)


class ProposeReview:
    """Conversation side: the job."""

    def __init__(
        self,
        targets: ReviewTargets,
        replies: ReviewReplies,
        service: ConversationService,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._targets = targets
        self._replies = replies
        self._service = service
        self._clock = clock

    async def __call__(self, job: Job) -> None:
        review_id = job.payload.get("review_id")
        if not isinstance(review_id, str) or not REVIEW_ID.fullmatch(review_id):
            raise PermanentJobError("payload no válido: falta review_id")
        target = await self._targets.review_targets(review_id)
        now = self._clock()
        if (
            target is None
            or target.stars is None
            or not target.unanswered
            or not target.live
            or target.created_at is None
            or target.created_at < now - PROPOSE_REVIEWS_NEWER_THAN
        ):
            log.event("review_not_proposed", job_id=job.id, reason="not_eligible")
            return
        unavailable = False
        proposed = 0
        for account_id, location_id in target.targets:
            try:
                draft = await self._replies.draft(account_id, review_id)
            except DraftUnavailable:
                unavailable = True  # the model did not answer: tried again later
                continue
            except (ReviewNotFound, DraftRejected, DraftRateLimited) as exc:
                # No valid draft for this account: the review stays in its panel.
                log.event("review_not_proposed", job_id=job.id, reason=type(exc).__name__)
                continue
            try:
                created = await self._service.submit_review(
                    account_id,
                    ReviewInput(review_id, target.stars, target.text, draft.text, location_id),
                )
            except ValueError:
                # The reply cannot be shown whole next to the review: panel only.
                log.event("review_not_proposed", job_id=job.id, reason="does_not_fit")
                continue
            proposed += created is not None
        log.event("review_proposed", job_id=job.id, accounts=proposed)
        if unavailable:
            raise JobError("no se pudo redactar la respuesta propuesta: se reintenta")
