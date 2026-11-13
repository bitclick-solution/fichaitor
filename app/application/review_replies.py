"""Replies to the reviews of the connected profile: the draft a model writes and the
confirmation of the owner.

Two separate things, on purpose:

- `draft` asks a model for a text. The model gets the stars and the text of the review
  and nothing else, and what it writes is only ever returned to the owner. Nothing in
  this path can create an action: asking for a draft publishes nothing, whatever the
  review says.
- `confirm` is the owner's decision. It records the exact text as a pending action;
  the worker's executor is what sends it to Google.

Neither the review nor the reply is logged.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.adapters.quota.memory import MemoryQuota
from app.domain.plans import FEATURE_AI_REPLIES, FEATURES, PlanRequired
from app.domain.quota import draft_scope
from app.domain.review_reply import (
    DRAFT_REVIEW_MAX_LENGTH,
    check_owner_reply,
    clean_text,
    draft_source_hash,
    validate_reply,
)
from app.ports.quota import Quota
from app.ports.reply_writer import ReplyWriter, ReplyWriterUnavailable
from app.ports.store import Store

log = logging.getLogger("fichaitor.reviews")


class ReviewNotFound(Exception):
    """Not a review of an active profile of that account."""


class DraftUnavailable(Exception):
    """No model is configured, or it did not answer. Worth trying later."""


class DraftRateLimited(Exception):
    pass


class DraftRejected(Exception):
    """There is no draft to offer: the owner writes the reply."""


@dataclass(frozen=True)
class Draft:
    review_id: str
    text: str
    cached: bool  # served from the stored draft: no model was called


class ReviewReplies:
    def __init__(
        self,
        store: Store,
        writer: ReplyWriter | None,
        *,
        draft_limit: int,
        draft_window_seconds: int,
        draft_timeout_seconds: float,
        clock: Callable[[], float],
        prompt_version: str = "",
        quota: Quota | None = None,
    ) -> None:
        self._store = store
        self._writer = writer
        # Per profile, counted in the quota store (PostgreSQL in production): a restart
        # or a second API process does not give a profile its drafts back.
        self._quota = quota if quota is not None else MemoryQuota()
        self._draft_limit = draft_limit
        self._draft_window = draft_window_seconds
        self._timeout = draft_timeout_seconds
        self._clock = clock
        self._prompt_version = prompt_version

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), UTC)

    async def draft(self, account_id: str, review_id: str) -> Draft:
        """Raises ReviewNotFound, DraftRejected, DraftUnavailable, DraftRateLimited or
        PlanRequired (the free plan has used its replies drafted by the model this
        month, app/domain/plans.py). A stored draft is served whatever the plan: the
        model is not asked again."""
        review = await self._store.get_review(account_id, review_id)
        if review is None:
            raise ReviewNotFound
        if review.rating is None:
            # Without stars there is no telling whether to thank or to apologise.
            raise DraftRejected
        # The review is data written by a third party. It reaches the model bounded
        # and on one line, with the stars and nothing else: no author, no business.
        text = clean_text(review.text, DRAFT_REVIEW_MAX_LENGTH)
        source = draft_source_hash(self._prompt_version, review.rating, text)

        stored = review.draft
        if stored is not None and stored.source_hash == source:
            # Asked twice with nothing changed: the model is not paid again.
            again = validate_reply(stored.text, review.rating)
            if again is not None:
                return Draft(review.id, again, cached=True)

        if self._writer is None:
            raise DraftUnavailable
        # Each business has its own cap (docs/multinegocio.md, 5.4). The store always
        # says which profile the review is of; the account is only a fallback.
        scope = draft_scope(review.location_id or account_id)
        try:
            taken = await self._quota.take_quota(
                scope, self._draft_limit, self._draft_window, self._now()
            )
        except Exception as exc:  # no paid call without a working cap
            log.warning("tope de borradores no disponible: %s", type(exc).__name__)
            raise DraftUnavailable from None
        if not taken:
            raise DraftRateLimited
        await self._take_allowance(account_id, review.location_id)
        try:
            async with asyncio.timeout(self._timeout):
                raw = await self._writer.write_reply(review.rating, text)
        except TimeoutError:
            log.warning("borrador de respuesta sin terminar a tiempo")
            raise DraftUnavailable from None
        except ReplyWriterUnavailable as exc:
            # The adapters' messages are fixed strings: no key, no review, no provider text.
            log.warning("borrador de respuesta no disponible: %s", exc)
            raise DraftUnavailable from None
        # Whatever the model wrote is untrusted too: it is shown only if it meets the rules.
        reply = validate_reply(raw, review.rating)
        if reply is None:
            log.warning("borrador de respuesta descartado: no cumple las reglas")
            raise DraftRejected
        try:
            await self._store.save_draft(account_id, review.id, source, reply, self._now())
        except Exception as exc:  # the draft is still good; it just will not be reused
            log.warning("borrador de respuesta no guardado: %s", type(exc).__name__)
        return Draft(review.id, reply, cached=False)

    async def _take_allowance(self, account_id: str, location_id: str | None) -> None:
        """Count one reply drafted by the model against the plan of that profile, read
        now. Only a plan with a cap counts (the free one: 25 per Google profile, once and
        for ever, app/domain/plans.py); Pro and Pro WhatsApp have none and spend nothing.
        Atomic in the store (free_allowance in PostgreSQL): two drafts at once never go
        over the cap."""
        if location_id is None:
            return
        try:
            plan = await self._store.location_plan(account_id, location_id)
        except Exception as exc:  # no paid call without knowing the plan
            log.warning("plan de la ficha no disponible: %s", type(exc).__name__)
            raise DraftUnavailable from None
        if plan is None:
            raise ReviewNotFound  # no longer an active profile of the account
        limit = FEATURES[plan].lifetime_ai_replies
        if limit is None:
            return
        try:
            taken = await self._store.take_ai_reply(account_id, location_id, limit)
        except Exception as exc:  # no paid call without a working cap
            log.warning("tope de respuestas con IA no disponible: %s", type(exc).__name__)
            raise DraftUnavailable from None
        if taken is None:
            raise ReviewNotFound
        if not taken:
            raise PlanRequired(FEATURE_AI_REPLIES, limit)

    async def confirm(self, account_id: str, review_id: str, text: object) -> str:
        """The owner confirms `text` as the reply to that review. Returns the id of the
        action. Raises InvalidReplyText, ReviewNotFound or ReplyInProgress."""
        confirmed = check_owner_reply(text)  # checked, never changed
        action_id = await self._store.create_reply_action(
            account_id, review_id, confirmed, self._now()
        )
        if action_id is None:
            raise ReviewNotFound
        return action_id
