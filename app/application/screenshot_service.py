"""Use case: show the owner what a screenshot of their reviews tab says, with one
suggested reply.

The image lives only in memory for the length of the request: it is not stored, cached
or logged, and neither is anything read from it (reviews, names, the business).
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.adapters.quota.memory import MemoryQuota
from app.application.rate_limit import SlidingWindowLimiter
from app.domain.quota import DAY_SECONDS, SCREENSHOT_DAY
from app.domain.screenshot_diagnosis import (
    DEFAULT_MIN_CONFIDENCE,
    ReviewsSummary,
    excerpt,
    parse_facts,
    pick_review_to_answer,
    summarize,
    validate_reply,
)
from app.ports.quota import Quota
from app.ports.screenshot_reader import ScreenshotReader, ScreenshotReaderUnavailable

log = logging.getLogger("fichaitor.screenshot")

# Below this much time left, the suggested reply is not even asked for.
_MIN_SECONDS_FOR_REPLY = 1.0


class ImageTooLarge(Exception):
    pass


class UnsupportedImage(Exception):
    """Not a PNG, JPEG or WebP, judging by its first bytes."""


class RateLimited(Exception):
    pass


class DailyLimitReached(Exception):
    """The cap on paid readings per day, for all clients together, has been hit."""


@dataclass(frozen=True)
class SuggestedReply:
    stars: int
    review_excerpt: str
    reply: str


@dataclass(frozen=True)
class ScreenshotResult:
    summary: ReviewsSummary
    suggested_reply: SuggestedReply | None


def sniff_image_type(data: bytes) -> str | None:
    """Media type by signature. The file name and the client's Content-Type are not trusted."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class ScreenshotService:
    def __init__(
        self,
        reader: ScreenshotReader,
        *,
        max_bytes: int = 6_000_000,
        rate_limit: int = 3,
        rate_window_seconds: int = 3600,
        daily_limit: int = 300,
        timeout_seconds: float = 30.0,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        clock: Callable[[], float] = time.time,
        quota: Quota | None = None,
    ) -> None:
        self._reader = reader
        self.max_bytes = max_bytes
        # Per client: in memory. For all clients together, per day: in the quota store
        # (PostgreSQL in production), so that a restart does not start it from zero.
        self._per_client = SlidingWindowLimiter(rate_limit, rate_window_seconds, clock)
        self._quota = quota if quota is not None else MemoryQuota()
        self._daily_limit = daily_limit
        self._clock = clock
        self._timeout = timeout_seconds
        self._min_confidence = min_confidence

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), UTC)

    async def precheck(self, client_key: str) -> None:
        """Whether this client may send a capture now, asked before its upload is read.
        Counts nothing. Raises RateLimited, DailyLimitReached or
        ScreenshotReaderUnavailable (the counter could not be read)."""
        if not self._per_client.peek(client_key):
            raise RateLimited
        try:
            left = await self._quota.quota_left(
                SCREENSHOT_DAY, self._daily_limit, DAY_SECONDS, self._now()
            )
        except Exception as exc:  # no paid call without a working cap
            log.warning("tope diario de capturas no disponible: %s", type(exc).__name__)
            raise ScreenshotReaderUnavailable("quota") from None
        if not left:
            raise DailyLimitReached

    async def run(self, image: bytes, client_key: str) -> ScreenshotResult:
        """Raises ImageTooLarge, UnsupportedImage, RateLimited, DailyLimitReached,
        ScreenshotReaderUnavailable, NotABusinessProfile, NotReviewsTab or
        NoReviewsVisible."""
        # Free checks first: a file we would reject anyway must not use up the limit.
        if len(image) > self.max_bytes:
            raise ImageTooLarge
        media_type = sniff_image_type(image)
        if media_type is None:
            raise UnsupportedImage
        # From here on every request is a paid call to the model.
        if not self._per_client.allow(client_key):
            raise RateLimited
        try:
            taken = await self._quota.take_quota(
                SCREENSHOT_DAY, self._daily_limit, DAY_SECONDS, self._now()
            )
        except Exception as exc:  # no paid call without a working cap
            log.warning("tope diario de capturas no disponible: %s", type(exc).__name__)
            raise ScreenshotReaderUnavailable("quota") from None
        if not taken:
            log.warning("límite diario de capturas alcanzado")
            raise DailyLimitReached
        deadline = time.monotonic() + self._timeout
        try:
            async with asyncio.timeout(self._timeout):
                raw = await self._reader.read(image, media_type)
        except TimeoutError as exc:
            log.warning("lectura de captura sin respuesta en %ss", self._timeout)
            raise ScreenshotReaderUnavailable("timeout") from exc
        except ScreenshotReaderUnavailable as exc:
            # The adapter's messages are fixed strings: no key, no image, no provider text.
            log.warning("lectura de captura fallida: %s", exc)
            raise
        # Whatever the reader says is data written by third parties: bounded here, and
        # the counts and the headline come from fixed rules.
        facts = parse_facts(raw)
        summary = summarize(facts, min_confidence=self._min_confidence)
        suggested = await self._suggest_reply(facts.reviews, deadline - time.monotonic())
        return ScreenshotResult(summary, suggested)

    async def _suggest_reply(self, reviews, seconds_left: float) -> SuggestedReply | None:
        """A reply for one unanswered review, or None. Never fails the request: the
        counts are worth showing even when the draft is missing or unusable."""
        target = pick_review_to_answer(reviews)
        if target is None or target.stars is None or seconds_left < _MIN_SECONDS_FOR_REPLY:
            return None
        try:
            async with asyncio.timeout(seconds_left):
                draft = await self._reader.write_reply(target.stars, target.text)
        except TimeoutError:
            log.warning("respuesta propuesta sin terminar a tiempo")
            return None
        except ScreenshotReaderUnavailable as exc:
            log.warning("respuesta propuesta no disponible: %s", exc)
            return None
        reply = validate_reply(draft, target.stars)
        if reply is None:
            log.warning("respuesta propuesta descartada: no cumple las reglas")
            return None
        return SuggestedReply(target.stars, excerpt(target.text), reply)
