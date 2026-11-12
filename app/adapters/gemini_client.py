"""The one place that calls the Gemini API (REST, `generateContent`).

Shared by the screenshot reader, the writer of replies and the sentiment analysis: each
of them brings its prompt, its schema and the model of its answer; the HTTP call, the
structured output and the reading of the answer are here and only here.

`generateContent` is stateless: it keeps no conversation on Google's side. The key
travels in the `x-goog-api-key` header, never in the URL, so it cannot end up in an
access log. Nothing is written to disk or logged here.

To move to Vertex AI, this is the module to change: the base URL (which then carries
the project and the region), and the authentication (an OAuth token of a service
account instead of the API key header). See docs/sentimiento.md.
"""

import re
from dataclasses import dataclass
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# The tasks are transcription, classification and short texts: more reasoning only
# adds cost and latency.
THINKING_LEVEL = "LOW"
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,80}")

T = TypeVar("T", bound=BaseModel)


class GeminiUnavailable(Exception):
    """The call failed or its answer cannot be used. The message is a fixed string:
    the provider's own text may quote the request, and httpx errors may quote the URL."""


@dataclass(frozen=True)
class GeminiUsage:
    """Tokens of one call, from `usageMetadata`. None when the answer does not say."""

    input_tokens: int | None = None
    output_tokens: int | None = None  # the answer plus the reasoning


def model_path(model: str) -> str:
    """`gemini-3.6-flash` and `models/gemini-3.6-flash` are both accepted."""
    name = model.removeprefix("models/")
    if not _MODEL_NAME.fullmatch(name):
        raise ValueError("FICHAITOR_GEMINI_MODEL no es un nombre de modelo válido")
    return f"models/{name}"


class GeminiClient:
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

    async def close(self) -> None:
        await self._client.aclose()

    async def generate(
        self, system: str, parts: list[dict], schema: dict, max_tokens: int, output: type[T]
    ) -> tuple[T, GeminiUsage]:
        """One call with structured output. The answer is validated against `output`;
        a blocked, cut-off or malformed answer is a failure."""
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
        try:
            resp = await self._client.post(
                self._url, headers={"x-goog-api-key": self._api_key}, json=body
            )
        except httpx.TimeoutException:
            raise GeminiUnavailable("timeout") from None
        except httpx.HTTPError:
            raise GeminiUnavailable("connection error") from None
        if resp.status_code != 200:
            raise GeminiUnavailable(f"status {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError:
            raise GeminiUnavailable("body is not JSON") from None
        text = _answer_text(payload)
        try:
            return output.model_validate_json(text), _usage(payload)
        except ValidationError:
            # No cause attached: a validation error quotes the model's output.
            raise GeminiUnavailable("answer does not match the schema") from None


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _usage(payload: dict) -> GeminiUsage:
    meta = payload.get("usageMetadata")
    if not isinstance(meta, dict):
        return GeminiUsage()
    has_output = "candidatesTokenCount" in meta or "thoughtsTokenCount" in meta
    output = _count(meta.get("candidatesTokenCount")) + _count(meta.get("thoughtsTokenCount"))
    return GeminiUsage(
        input_tokens=_count(meta["promptTokenCount"]) if "promptTokenCount" in meta else None,
        output_tokens=output if has_output else None,
    )


def _answer_text(payload: object) -> str:
    """The JSON text of the only candidate, or a failure. A blocked, empty or cut-off
    answer is a failure: nothing can be trusted in half an answer."""
    if not isinstance(payload, dict):
        raise GeminiUnavailable("unexpected body")
    feedback = payload.get("promptFeedback")
    if isinstance(feedback, dict) and feedback.get("blockReason"):
        raise GeminiUnavailable("prompt blocked by the safety filters")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        raise GeminiUnavailable("no candidates")
    candidate = candidates[0]
    reason = candidate.get("finishReason")
    if reason != "STOP":
        # SAFETY, MAX_TOKENS, RECITATION, PROHIBITED_CONTENT... The value is the
        # provider's enum, not free text, but only its shape is trusted in a log.
        known = reason if isinstance(reason, str) and re.fullmatch(r"[A-Z_]{1,40}", reason) else "?"
        raise GeminiUnavailable(f"finish reason {known}")
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        raise GeminiUnavailable("empty answer")
    text = "".join(
        part["text"]
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str) and not part.get("thought")
    )
    if not text.strip():
        raise GeminiUnavailable("empty answer")
    return text
