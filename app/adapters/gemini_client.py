"""The one place that calls the Gemini API (REST, `generateContent`).

Shared by the screenshot reader, the writer of replies and the sentiment analysis: each
of them brings its prompt, its schema and the model of its answer; the HTTP call, the
structured output and the reading of the answer are here and only here.

`generateContent` is stateless: it keeps no conversation on Google's side. Nothing is
written to disk or logged here.

Two providers of the same models, chosen by FICHAITOR_GEMINI_PROVIDER (see
`app.adapters.gemini_provider` and docs/despliegue.md, «Gemini en Vertex AI»):

- AI Studio (Gemini Developer API): the key travels in the `x-goog-api-key` header,
  never in the URL, so it cannot end up in an access log.
- Vertex AI: the URL carries the project and the location; an OAuth access token of a
  service account goes in `Authorization: Bearer` (`app.adapters.gemini_auth`).

The body, the structured output (`responseJsonSchema`, accepted by both), the limits,
the reading of the answer, the errors and the usage of tokens are the same for both.
"""

import re
from dataclasses import dataclass
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.adapters.gemini_auth import TokenSource, VertexAuthError

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
VERTEX_API_VERSION = "v1"
# The ID of the project (my-project) or its number (544696777336): Vertex takes both.
_PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]|[0-9]{1,20}")
_LOCATION = re.compile(r"[a-z][a-z0-9-]{1,40}")
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


class GeminiBackend(Protocol):
    """Where the request goes and how it is authenticated: the only difference between
    the providers."""

    def url(self, model: str) -> str: ...

    async def headers(self, client: httpx.AsyncClient) -> dict[str, str]: ...

    def rejected(self) -> None:
        """The provider answered 401: the credential it got is no longer valid."""


class AIStudioBackend:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def __repr__(self) -> str:  # never the key
        return "AIStudioBackend()"

    def url(self, model: str) -> str:
        return f"{BASE_URL}/{model_path(model)}:generateContent"

    async def headers(self, client: httpx.AsyncClient) -> dict[str, str]:
        return {"x-goog-api-key": self._api_key}

    def rejected(self) -> None:
        pass


def vertex_host(location: str) -> str:
    """`global` and the multi-regions (`eu`, `us`) have their own hosts; a region is
    `{region}-aiplatform.googleapis.com`."""
    if location == "global":
        return "aiplatform.googleapis.com"
    if location in ("eu", "us"):
        return f"aiplatform.{location}.rep.googleapis.com"
    return f"{location}-aiplatform.googleapis.com"


class VertexBackend:
    def __init__(self, project: str, location: str, tokens: TokenSource) -> None:
        if not _PROJECT_ID.fullmatch(project):
            raise ValueError("FICHAITOR_VERTEX_PROJECT no es un ID de proyecto válido")
        if not _LOCATION.fullmatch(location):
            raise ValueError("FICHAITOR_VERTEX_LOCATION no es una ubicación válida")
        self.project = project
        self.location = location
        self._tokens = tokens

    def __repr__(self) -> str:  # never the token
        return f"VertexBackend(project={self.project!r}, location={self.location!r})"

    def url(self, model: str) -> str:
        name = model_path(model).removeprefix("models/")
        return (
            f"https://{vertex_host(self.location)}/{VERTEX_API_VERSION}/projects/{self.project}"
            f"/locations/{self.location}/publishers/google/models/{name}:generateContent"
        )

    async def headers(self, client: httpx.AsyncClient) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {await self._tokens.token(client)}"}
        if self._tokens.quota_project:  # only for the credentials of a person (gcloud)
            headers["x-goog-user-project"] = self._tokens.quota_project
        return headers

    def rejected(self) -> None:
        self._tokens.invalidate()


class GeminiClient:
    def __init__(
        self,
        api_key: str | GeminiBackend,
        model: str,
        timeout_seconds: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """`api_key` is the key of AI Studio, as before, or a backend (Vertex AI)."""
        self._backend = AIStudioBackend(api_key) if isinstance(api_key, str) else api_key
        self._url = self._backend.url(model)
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
            headers = await self._backend.headers(self._client)
        except VertexAuthError as exc:
            # One of the fixed strings of gemini_auth: never a key nor a token.
            raise GeminiUnavailable(f"credentials: {exc}") from None
        try:
            resp = await self._client.post(self._url, headers=headers, json=body)
        except httpx.TimeoutException:
            raise GeminiUnavailable("timeout") from None
        except httpx.HTTPError:
            raise GeminiUnavailable("connection error") from None
        if resp.status_code == 401:
            self._backend.rejected()
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
