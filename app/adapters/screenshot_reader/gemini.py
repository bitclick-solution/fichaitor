"""Screenshot reader and reply writer on the Gemini API (REST, `generateContent`).

Two requests per capture at most. The first sends the image inline (base64) and gets
back the visible reviews as JSON. The second, only when there is a review to answer,
sends the stars and the text of that one review (no image, no names) and gets back a
draft reply. Counting, choosing the review and accepting or discarding the draft are
done by the domain.

The HTTP call itself lives in `app.adapters.gemini_client`, shared with the sentiment
analysis: this module brings the prompts, the schemas and the reading of the answer.
Nothing is written to disk or logged here. See docs/pantallazo.md for the sources.
"""

import base64
import json
from collections.abc import Mapping
from typing import TypeVar

import httpx
from pydantic import BaseModel

from app.adapters.gemini_client import (
    BASE_URL,
    THINKING_LEVEL,
    GeminiClient,
    GeminiUnavailable,
    model_path,
)
from app.adapters.screenshot_reader.prompt import (
    READ_SCHEMA,
    READ_SYSTEM_PROMPT,
    READ_USER_PROMPT,
    REPLY_SCHEMA,
    REPLY_SYSTEM_PROMPT,
    ReplyDraft,
    ScreenshotExtraction,
)
from app.ports.screenshot_reader import ScreenshotReaderUnavailable

__all__ = ["BASE_URL", "THINKING_LEVEL", "GeminiScreenshotReader", "model_path", "to_facts"]

# Up to ten reviews with their text. Thinking tokens count against this limit too.
READ_MAX_OUTPUT_TOKENS = 4096
REPLY_MAX_OUTPUT_TOKENS = 1024
_OWNER_REPLY = {"yes": True, "no": False, "unknown": None}

T = TypeVar("T", bound=BaseModel)


def to_facts(extraction: ScreenshotExtraction) -> dict[str, object]:
    """Sentinels become None ("not visible") before the answer reaches the domain."""
    return {
        "screen": extraction.screen,
        "confidence": extraction.confidence,
        "business_name": extraction.business_name or None,
        "reviews": [
            {
                "stars": review.stars if 1 <= review.stars <= 5 else None,
                "text": review.text or None,
                "age": review.age or None,
                "has_owner_reply": _OWNER_REPLY[review.owner_reply],
            }
            for review in extraction.reviews
        ],
    }


class GeminiScreenshotReader:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._gemini = GeminiClient(api_key, model, timeout_seconds, client)
        self._url = self._gemini._url
        self._client = self._gemini._client

    async def read(self, image: bytes, media_type: str) -> Mapping[str, object]:
        parts = [
            {
                "inlineData": {
                    "mimeType": media_type,
                    "data": base64.standard_b64encode(image).decode("ascii"),
                }
            },
            {"text": READ_USER_PROMPT},
        ]
        extraction = await self._generate(
            READ_SYSTEM_PROMPT, parts, READ_SCHEMA, READ_MAX_OUTPUT_TOKENS, ScreenshotExtraction
        )
        return to_facts(extraction)

    async def write_reply(self, stars: int, review_text: str) -> str:
        # JSON keeps the review apart from the instructions whatever it contains.
        review = json.dumps({"estrellas": stars, "texto": review_text}, ensure_ascii=False)
        draft = await self._generate(
            REPLY_SYSTEM_PROMPT,
            [{"text": review}],
            REPLY_SCHEMA,
            REPLY_MAX_OUTPUT_TOKENS,
            ReplyDraft,
        )
        return draft.reply

    async def _generate(
        self, system: str, parts: list[dict], schema: dict, max_tokens: int, output: type[T]
    ) -> T:
        try:
            answer, _ = await self._gemini.generate(system, parts, schema, max_tokens, output)
        except GeminiUnavailable as exc:
            # One of the client's fixed strings: never the provider's own text.
            raise ScreenshotReaderUnavailable(str(exc)) from None
        return answer
