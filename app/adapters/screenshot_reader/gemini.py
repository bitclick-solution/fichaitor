"""Screenshot reader and reply writer on the Gemini API (REST, `generateContent`).

Two requests per capture at most. The first sends the image inline (base64) and gets
back the visible reviews as JSON. The second, only when there is a review to answer,
sends the stars and the text of that one review (no image, no names) and gets back a
draft reply. Counting, choosing the review and accepting or discarding the draft are
done by the domain.

`generateContent` is stateless: unlike the newer Interactions API, which stores every
interaction by default, it keeps no conversation on Google's side. The key travels in
the `x-goog-api-key` header, never in the URL, so it cannot end up in an access log.
Nothing is written to disk or logged here. See docs/pantallazo.md for the sources.
"""

import base64
import json
import re
from collections.abc import Mapping
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

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

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# Up to ten reviews with their text. Thinking tokens count against this limit too.
READ_MAX_OUTPUT_TOKENS = 4096
REPLY_MAX_OUTPUT_TOKENS = 1024
# Transcription and a four-sentence reply: more reasoning only adds cost and latency.
THINKING_LEVEL = "LOW"
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,80}")
_OWNER_REPLY = {"yes": True, "no": False, "unknown": None}

T = TypeVar("T", bound=BaseModel)


def model_path(model: str) -> str:
    """`gemini-3.6-flash` and `models/gemini-3.6-flash` are both accepted."""
    name = model.removeprefix("models/")
    if not _MODEL_NAME.fullmatch(name):
        raise ValueError("FICHAITOR_GEMINI_MODEL no es un nombre de modelo válido")
    return f"models/{name}"


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
        self._api_key = api_key
        self._url = f"{BASE_URL}/{model_path(model)}:generateContent"
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

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
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
                "maxOutputTokens": max_tokens,
                "thinkingConfig": {"thinkingLevel": THINKING_LEVEL},
            },
        }
        # The messages of these errors are fixed strings: the provider's own text may
        # quote the request (reviews, names), and httpx errors may quote the URL.
        try:
            resp = await self._client.post(
                self._url, headers={"x-goog-api-key": self._api_key}, json=body
            )
        except httpx.TimeoutException:
            raise ScreenshotReaderUnavailable("timeout") from None
        except httpx.HTTPError:
            raise ScreenshotReaderUnavailable("connection error") from None
        if resp.status_code != 200:
            raise ScreenshotReaderUnavailable(f"status {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError:
            raise ScreenshotReaderUnavailable("body is not JSON") from None
        text = _answer_text(payload)
        try:
            return output.model_validate_json(text)
        except ValidationError:
            # No cause attached: a validation error quotes the model's output.
            raise ScreenshotReaderUnavailable("answer does not match the schema") from None


def _answer_text(payload: object) -> str:
    """The JSON text of the only candidate, or a failure. A blocked, empty or cut-off
    answer is a failure: nothing can be trusted in half an answer."""
    if not isinstance(payload, dict):
        raise ScreenshotReaderUnavailable("unexpected body")
    feedback = payload.get("promptFeedback")
    if isinstance(feedback, dict) and feedback.get("blockReason"):
        raise ScreenshotReaderUnavailable("prompt blocked by the safety filters")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        raise ScreenshotReaderUnavailable("no candidates")
    candidate = candidates[0]
    reason = candidate.get("finishReason")
    if reason != "STOP":
        # SAFETY, MAX_TOKENS, RECITATION, PROHIBITED_CONTENT... The value is the
        # provider's enum, not free text, but only its shape is trusted in a log.
        known = reason if isinstance(reason, str) and re.fullmatch(r"[A-Z_]{1,40}", reason) else "?"
        raise ScreenshotReaderUnavailable(f"finish reason {known}")
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        raise ScreenshotReaderUnavailable("empty answer")
    text = "".join(
        part["text"]
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str) and not part.get("thought")
    )
    if not text.strip():
        raise ScreenshotReaderUnavailable("empty answer")
    return text
