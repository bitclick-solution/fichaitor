"""Sentiment model without a model, for tests and local development.

With nothing injected it classifies by the stars and by a few keywords, and words a
fixed summary without figures: enough to walk the whole feature with the demo profile
and `FICHAITOR_FAKE_SCREENSHOT=true`. It obeys nothing a review says.
"""

import re
from collections.abc import Callable, Mapping, Sequence

from app.ports.sentiment_model import (
    ModelUsage,
    ReviewToClassify,
    SentimentModelUnavailable,
    SummaryRequest,
)

_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (r"trat|atend|amab|simp|disculp|personal", ("Trato del personal",)),
    (r"esper|tard|minutos|cola", ("Tiempo de espera",)),
    (r"tapas|arroz|comida|croqueta|caf[eé]|raci|desayun|plato", ("Comida y bebida", "Calidad")),
    (r"precio|caro|barat", ("Precio",)),
    (r"limpi|suci", ("Limpieza",)),
    (r"ruido|ambiente|terraza|m[uú]sica", ("Ambiente",)),
    (r"r[aá]pid", ("Rapidez",)),
)
FAKE_USAGE = ModelUsage(input_tokens=900, output_tokens=300)
SUMMARY_GOOD = (
    "La mayoría de tus clientes sale contenta y lo dice en sus reseñas. "
    "Las quejas son pocas y se concentran en los mismos puntos."
)
SUMMARY_BAD = (
    "Las reseñas recientes muestran más quejas que elogios. "
    "Los motivos se repiten, así que hay margen claro para mejorar."
)
SUGGESTION = "Responde esta semana a las reseñas negativas y revisa con tu equipo lo que se repite."


def demo_classification(review: ReviewToClassify, topics: Sequence[str]) -> dict:
    sentiment = (
        "positive" if review.stars >= 4 else "negative" if 1 <= review.stars <= 2 else "neutral"
    )
    polarity = "praise" if sentiment == "positive" else "complaint"
    found: list[dict] = []
    for pattern, labels in _KEYWORDS:
        label = next((name for name in labels if name in topics), None)
        if label and re.search(pattern, review.text, re.IGNORECASE):
            found.append({"topic": label, "polarity": polarity})
    return {"sentiment": sentiment, "topics": found[:3]}


class FakeSentimentModel:
    def __init__(
        self,
        classify: Callable[[ReviewToClassify, Sequence[str]], object] | None = None,
        texts: Mapping[str, object] | None = None,
        fail_classify: bool = False,
        fail_summary: bool = False,
    ) -> None:
        self._classify = classify or demo_classification
        self._texts = texts
        self.fail_classify = fail_classify
        self.fail_summary = fail_summary
        # What was asked: the batches of reviews and the summary requests.
        self.classify_calls: list[tuple[ReviewToClassify, ...]] = []
        self.summary_calls: list[SummaryRequest] = []

    async def classify(
        self, reviews: Sequence[ReviewToClassify], topics: Sequence[str]
    ) -> tuple[Mapping[int, object], ModelUsage]:
        self.classify_calls.append(tuple(reviews))
        if self.fail_classify:
            raise SentimentModelUnavailable("fallo simulado")
        answers = {r.number: self._classify(r, topics) for r in reviews}
        return {n: a for n, a in answers.items() if a is not None}, FAKE_USAGE

    async def summarize(self, request: SummaryRequest) -> tuple[Mapping[str, object], ModelUsage]:
        self.summary_calls.append(request)
        if self.fail_summary:
            raise SentimentModelUnavailable("fallo simulado")
        if self._texts is not None:
            return self._texts, FAKE_USAGE
        summary = SUMMARY_GOOD if request.overall == "positivo" else SUMMARY_BAD
        return {"summary": summary, "suggestion": SUGGESTION}, FAKE_USAGE
