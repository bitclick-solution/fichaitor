"""Sentiment model on the Gemini API, through the shared client.

Two kinds of request. One classifies a batch of reviews: it sends the allowed topics
and, of each review, a number, the stars and the text; never an author, a business or
an identifier of ours. The other words the summary from figures the code computed and
a few excerpts. Both answers go back raw: accepting them is the domain's job.

Nothing is written to disk or logged here.
"""

import json
from collections.abc import Mapping, Sequence

import httpx

from app.adapters.gemini_client import GeminiClient, GeminiUnavailable, GeminiUsage
from app.adapters.sentiment_model.prompt import (
    CLASSIFY_SYSTEM_PROMPT,
    SUMMARY_SCHEMA,
    SUMMARY_SYSTEM_PROMPT,
    ClassifiedBatch,
    SummaryTexts,
    classify_schema,
)
from app.ports.sentiment_model import (
    ModelUsage,
    ReviewToClassify,
    SentimentModelUnavailable,
    SummaryRequest,
)

# About 40 tokens per review, plus the reasoning, for up to 100 reviews per call.
CLASSIFY_MAX_OUTPUT_TOKENS = 8192
SUMMARY_MAX_OUTPUT_TOKENS = 1024


def _usage(usage: GeminiUsage) -> ModelUsage:
    return ModelUsage(usage.input_tokens, usage.output_tokens)


class GeminiSentimentModel:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._gemini = GeminiClient(api_key, model, timeout_seconds, client)

    async def close(self) -> None:
        await self._gemini.close()

    async def classify(
        self, reviews: Sequence[ReviewToClassify], topics: Sequence[str]
    ) -> tuple[Mapping[int, object], ModelUsage]:
        # JSON keeps the reviews apart from the instructions whatever they contain.
        request = json.dumps(
            {
                "temas": list(topics),
                "resenas": [
                    {"id": r.number, "estrellas": r.stars, "texto": r.text} for r in reviews
                ],
            },
            ensure_ascii=False,
        )
        try:
            batch, usage = await self._gemini.generate(
                CLASSIFY_SYSTEM_PROMPT,
                [{"text": request}],
                classify_schema(topics),
                CLASSIFY_MAX_OUTPUT_TOKENS,
                ClassifiedBatch,
            )
        except GeminiUnavailable as exc:
            raise SentimentModelUnavailable(str(exc)) from None
        answers: dict[int, object] = {}
        for item in batch.items:
            # The first answer for a number stands: a repeated one changes nothing.
            answers.setdefault(item.id, {"sentiment": item.sentiment, "topics": item.topics})
        return answers, _usage(usage)

    async def summarize(self, request: SummaryRequest) -> tuple[Mapping[str, object], ModelUsage]:
        facts = json.dumps(
            {
                "nota": request.score,
                "valoracion": request.overall,
                "resenas_analizadas": request.based_on,
                "elogios": [{"tema": t, "resenas": n} for t, n in request.praised],
                "quejas": [{"tema": t, "resenas": n} for t, n in request.complaints],
                "extractos": [{"estrellas": s, "texto": t} for s, t in request.excerpts],
            },
            ensure_ascii=False,
        )
        try:
            texts, usage = await self._gemini.generate(
                SUMMARY_SYSTEM_PROMPT,
                [{"text": facts}],
                SUMMARY_SCHEMA,
                SUMMARY_MAX_OUTPUT_TOKENS,
                SummaryTexts,
            )
        except GeminiUnavailable as exc:
            raise SentimentModelUnavailable(str(exc)) from None
        return {"summary": texts.summary, "suggestion": texts.suggestion}, _usage(usage)
