"""Sentiment of a business: how people see it, from the reviews of its profile. No I/O.

The model proposes and the code decides:

- A model classifies each review with text once: `sentiment` and up to three topics
  from a closed list, each with a polarity. Its answer is data and is accepted only if
  every value belongs to the closed sets (`parse_classification`). A review without
  text is classified by its stars with a fixed rule and never reaches the model.
- The score (0 to 100), the verdict, the counts per topic and the decision to
  recompute are worked out here, from the classifications. The model never counts.
- The summary is written by a model and accepted only if it passes `validate_summary`;
  otherwise the fixed text of `rule_summary` is used. Either way the code adds a
  sentence about the ratings of 1 or 2 stars that came without a comment.
- The suggestion is chosen by the code (`choose_suggestion`): unanswered reviews come
  first. The one a model wrote is used only where the rules leave room for it, and only
  if it passes `validate_suggestion`.

The text of a review is written by a third party: nothing in it is ever acted upon.
The formula and the lists are documented in docs/sentimiento.md.
"""

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from app.domain.review_reply import _CONTACT, _PROMISE, _SENTENCE_END, _has_emoji, clean_text

# 2 (2026-11-12): the summary names the low ratings without a comment and the
# suggestion is chosen by fixed rules, unanswered reviews first.
FORMULA_VERSION = 2

POSITIVE = "positive"
NEUTRAL = "neutral"
NEGATIVE = "negative"
SENTIMENTS = (POSITIVE, NEUTRAL, NEGATIVE)

PRAISE = "praise"
COMPLAINT = "complaint"
POLARITIES = (PRAISE, COMPLAINT)

MAX_TOPICS_PER_REVIEW = 3
MAX_LISTED_TOPICS = 5

# How a classification came to be.
METHOD_MODEL = "model"  # a model classified the text
METHOD_STARS = "stars"  # no text: the fixed rule on the stars
METHOD_FALLBACK = "stars_fallback"  # text the model could not classify, after every attempt
METHOD_PENDING = "pending"  # sent to the model and not classified yet: it is retried

# After this many failed attempts a review with text is classified by its stars for good:
# a review the model always refuses must not be paid for in every analysis.
MAX_CLASSIFY_ATTEMPTS = 3

STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insuficiente"
STATUS_ERROR = "error"
STATUS_PENDING = "pendiente"  # only in the API: nothing stored yet

OVERALL_POSITIVE = "positivo"
OVERALL_MIXED = "mixto"
OVERALL_NEGATIVE = "negativo"

SOURCE_MODEL = "model"
SOURCE_RULES = "rules"

# --- topics: a closed vocabulary per sector, so that counts can be compared ------------

OTHER_TOPIC = "Otro"
SECTOR_GENERAL = "general"
SECTOR_HOSPITALITY = "hosteleria"

TOPICS_BY_SECTOR: dict[str, tuple[str, ...]] = {
    SECTOR_GENERAL: (
        "Trato del personal",
        "Tiempo de espera",
        "Calidad",
        "Precio",
        "Limpieza",
        "Ambiente",
        "Rapidez",
        "Atención posventa",
        "Variedad",
        "Ubicación y acceso",
        OTHER_TOPIC,
    ),
    SECTOR_HOSPITALITY: (
        "Trato del personal",
        "Tiempo de espera",
        "Comida y bebida",
        "Precio",
        "Limpieza",
        "Ambiente",
        "Rapidez",
        "Variedad",
        "Ubicación y acceso",
        OTHER_TOPIC,
    ),
}
ALL_TOPICS = frozenset(topic for topics in TOPICS_BY_SECTOR.values() for topic in topics)

_HOSPITALITY = re.compile(
    r"\b(bar|bares|restaurante|cafeter[ií]a|caf[eé]|taberna|pizzer[ií]a|mes[oó]n|asador"
    r"|marisquer[ií]a|hamburgueser[ií]a|cervecer[ií]a|helader[ií]a|pasteler[ií]a|panader[ií]a"
    r"|bodega|chiringuito|pub|tapas|comida|restaurant)\b",
    re.IGNORECASE,
)


def sector_of(category: str | None) -> str:
    """Sector of a profile from the name of its primary category in Google."""
    return SECTOR_HOSPITALITY if category and _HOSPITALITY.search(category) else SECTOR_GENERAL


def topics_for(sector: str) -> tuple[str, ...]:
    return TOPICS_BY_SECTOR.get(sector, TOPICS_BY_SECTOR[SECTOR_GENERAL])


# --- settings -------------------------------------------------------------------------


@dataclass(frozen=True)
class SentimentPolicy:
    """Every threshold of the feature. The values come from app/sentiment_config.py."""

    window_months: int = 12
    min_text_reviews: int = 5  # fewer reviews with text than this: provisional
    positive_min_score: int = 70
    mixed_min_score: int = 40
    new_reviews_trigger: int = 3
    monthly_days: int = 30
    min_hours_between: float = 24.0
    batch_size: int = 30  # reviews per call to the model
    max_calls: int = 6  # calls to the model per analysis, the summary included

    @property
    def window_days(self) -> int:
        return self.window_months * 365 // 12


# --- reviews and their classification -------------------------------------------------


@dataclass(frozen=True)
class TopicMention:
    topic: str
    polarity: str


@dataclass(frozen=True)
class Classification:
    sentiment: str
    topics: tuple[TopicMention, ...] = ()


@dataclass(frozen=True)
class StoredClassification:
    source_hash: str  # of the stars and the text it was made from
    sentiment: str | None  # None: sent to the model, not classified yet
    topics: tuple[TopicMention, ...] = ()
    method: str = METHOD_MODEL
    attempts: int = 0  # calls to the model that included this review


@dataclass(frozen=True)
class SentimentReview:
    """A stored review as the analysis sees it. Nothing about who wrote it exists."""

    id: str
    rating: int | None
    text: str
    created_at: datetime | None
    stored: StoredClassification | None = None
    replied: bool = False  # the owner has answered it


def has_text(review: SentimentReview) -> bool:
    return bool(review.text.strip())


def source_hash(rating: int | None, text: str) -> str:
    """What a classification was made from. It changes when the stars or the text do."""
    return hashlib.sha256(f"{rating}\n{text}".encode()).hexdigest()


def stars_sentiment(rating: int | None) -> str | None:
    """The fixed rule: 4 and 5 stars positive, 3 neutral, 1 and 2 negative."""
    if rating is None or not 1 <= rating <= 5:
        return None
    if rating >= 4:
        return POSITIVE
    return NEUTRAL if rating == 3 else NEGATIVE


def current(review: SentimentReview) -> StoredClassification | None:
    """The stored classification, if it was made from the review as it is now."""
    stored = review.stored
    if stored is None or stored.source_hash != source_hash(review.rating, review.text):
        return None
    return stored


def countable(review: SentimentReview) -> bool:
    """A review with neither stars nor text says nothing."""
    return has_text(review) or stars_sentiment(review.rating) is not None


def is_new(review: SentimentReview) -> bool:
    """Not seen by any analysis yet, or changed since."""
    return countable(review) and current(review) is None


def needs_model(review: SentimentReview) -> bool:
    """Reviews a model has to classify: new or changed text, and those still pending."""
    if not has_text(review):
        return False
    stored = current(review)
    if stored is None:
        return True
    return stored.sentiment is None and stored.attempts < MAX_CLASSIFY_ATTEMPTS


def effective(review: SentimentReview) -> Classification | None:
    """What a review counts as: its stored classification or, while it has none, the
    rule on its stars. None when it cannot be told (no usable classification, no stars)."""
    stored = current(review)
    if stored is not None and stored.sentiment in SENTIMENTS:
        return Classification(stored.sentiment, stored.topics)
    by_stars = stars_sentiment(review.rating)
    return Classification(by_stars) if by_stars is not None else None


def parse_topics(raw: object, allowed: frozenset[str] | Sequence[str]) -> tuple | None:
    """Topics of one review, or None if anything is outside the closed values."""
    if not isinstance(raw, list) or len(raw) > MAX_TOPICS_PER_REVIEW:
        return None
    topics: dict[str, TopicMention] = {}
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"topic", "polarity"}:
            return None
        topic, polarity = item["topic"], item["polarity"]
        if not isinstance(topic, str) or topic not in allowed or polarity not in POLARITIES:
            return None
        topics.setdefault(topic, TopicMention(topic, polarity))  # a topic counts once
    return tuple(topics.values())


def parse_classification(raw: object, allowed_topics: Sequence[str]) -> Classification | None:
    """What a model said about one review, or None unless every value is one of the
    closed ones. Never raises: the answer of a model is data."""
    if not isinstance(raw, dict):
        return None
    sentiment = raw.get("sentiment")
    if not isinstance(sentiment, str) or sentiment not in SENTIMENTS:
        return None
    topics = parse_topics(raw.get("topics"), allowed_topics)
    if topics is None:
        return None
    return Classification(sentiment, topics)


def topics_to_json(topics: Sequence[TopicMention]) -> list[dict[str, str]]:
    return [{"topic": t.topic, "polarity": t.polarity} for t in topics]


def topics_from_json(raw: object) -> tuple[TopicMention, ...]:
    """Stored topics. Anything unexpected reads as no topics."""
    return parse_topics(raw, ALL_TOPICS) or ()


# --- when to analyse ------------------------------------------------------------------

TRIGGER_FIRST = "first"
TRIGGER_LOW_RATING = "low_rating"
TRIGGER_NEW_REVIEWS = "new_reviews"
TRIGGER_MONTHLY = "monthly"
TRIGGER_FORMULA = "formula_update"  # stored with an earlier FORMULA_VERSION
TRIGGERS = (
    TRIGGER_FIRST,
    TRIGGER_LOW_RATING,
    TRIGGER_NEW_REVIEWS,
    TRIGGER_MONTHLY,
    TRIGGER_FORMULA,
)

LOW_RATING_MAX = 2


@dataclass(frozen=True)
class LastAnalysis:
    status: str
    generated_at: datetime
    based_on: int
    formula_version: int = FORMULA_VERSION  # of the code that produced it


@dataclass(frozen=True)
class AnalysisState:
    last: LastAnalysis | None
    new_reviews: int  # not seen by any analysis, or changed since
    new_low_reviews: int  # of those, the ones of 1 or 2 stars


def analysis_state(reviews: Sequence[SentimentReview], last: LastAnalysis | None) -> AnalysisState:
    fresh = [r for r in reviews if is_new(r)]
    low = sum(1 for r in fresh if r.rating is not None and r.rating <= LOW_RATING_MAX)
    return AnalysisState(last, len(fresh), low)


def all_triggers(google_sub: str) -> bool:
    """Extension point for plans: whether the triggers by event (new reviews, a low
    rating) apply to that connection, or only the monthly one. There is no table of
    plans yet, so every trigger applies to everyone. See docs/sentimiento.md."""
    return True


def decide(
    state: AnalysisState,
    now: datetime,
    policy: SentimentPolicy,
    *,
    event_triggers: bool = True,
) -> str | None:
    """Why an analysis must run now, or None. Pure: same input, same answer.

    The first analysis runs at once, with no cap: there is none yet, the last one
    failed, or it found no reviews and now there are some. After that, at most one per
    `min_hours_between`, on whichever comes first: a new review of 1 or 2 stars,
    `new_reviews_trigger` new reviews, or `monthly_days` with at least one new review.

    A good analysis made by an earlier version of the formula is redone at once, with
    no cap: it happens once, since the new one is stored with the current version. It
    classifies nothing again: only reviews that are new or changed reach the model.
    """
    last = state.last
    if last is None or last.status == STATUS_ERROR:
        return TRIGGER_FIRST
    if last.based_on == 0 and state.new_reviews > 0:
        return TRIGGER_FIRST
    if last.status == STATUS_OK and last.formula_version < FORMULA_VERSION:
        return TRIGGER_FORMULA
    if state.new_reviews == 0:
        return None
    age = now - last.generated_at
    if age < timedelta(hours=policy.min_hours_between):
        return None
    if event_triggers:
        if state.new_low_reviews > 0:
            return TRIGGER_LOW_RATING
        if state.new_reviews >= policy.new_reviews_trigger:
            return TRIGGER_NEW_REVIEWS
    if age >= timedelta(days=policy.monthly_days):
        return TRIGGER_MONTHLY
    return None


# --- the score: computed by the code --------------------------------------------------

_VALUE = {POSITIVE: 2, NEUTRAL: 1, NEGATIVE: 0}  # halves: 1, 0.5 and 0
WEIGHT_NEWEST = 1000
WEIGHT_WINDOW_END = 500
WEIGHT_OLDER = 250


def weight(created_at: datetime | None, now: datetime, policy: SentimentPolicy) -> int:
    """Weight of a review, in thousandths: 1000 today, falling in a straight line to 500
    at the end of the window, and 250 for anything older or without a date."""
    if created_at is None:
        return WEIGHT_OLDER
    days = max((now - created_at).days, 0)
    window = policy.window_days
    if days > window:
        return WEIGHT_OLDER
    return WEIGHT_NEWEST - (WEIGHT_NEWEST - WEIGHT_WINDOW_END) * days // window


def score_of(
    items: Sequence[tuple[str, datetime | None]], now: datetime, policy: SentimentPolicy
) -> int | None:
    """Score from 0 to 100: the weighted share of positive reviews, a neutral one
    counting half. Integer arithmetic only: the same input always gives the same
    score. None without reviews."""
    total = 0
    earned = 0
    for sentiment, created_at in items:
        w = weight(created_at, now, policy)
        total += w
        earned += w * _VALUE[sentiment]
    if total == 0:
        return None
    return (100 * earned + total) // (2 * total)  # rounded half up


def overall_of(score: int, policy: SentimentPolicy) -> str:
    if score >= policy.positive_min_score:
        return OVERALL_POSITIVE
    return OVERALL_MIXED if score >= policy.mixed_min_score else OVERALL_NEGATIVE


@dataclass(frozen=True)
class TopicCount:
    topic: str
    mentions: int  # reviews that mention it, never more than the reviews analysed


@dataclass(frozen=True)
class Figures:
    """Everything the code works out from the classifications."""

    status: str
    based_on: int  # reviews that count
    with_text: int  # of those, the ones with text
    provisional: bool
    period: str | None
    score: int | None
    overall: str | None
    praised: tuple[TopicCount, ...] = ()
    complaints: tuple[TopicCount, ...] = ()


def period_label(policy: SentimentPolicy, *, whole_history: bool) -> str:
    if whole_history:
        return "todas las reseñas"
    return f"últimos {policy.window_months} meses"


def _in_window(review: SentimentReview, now: datetime, policy: SentimentPolicy) -> bool:
    return review.created_at is not None and (now - review.created_at).days <= policy.window_days


def _topic_scope(
    reviews: Sequence[SentimentReview], now: datetime, policy: SentimentPolicy
) -> tuple[list[SentimentReview], bool]:
    """The reviews whose topics are counted: those of the window when it holds enough
    reviews with text; otherwise all of them (and the period says so)."""
    recent = [r for r in reviews if _in_window(r, now, policy)]
    if sum(1 for r in recent if has_text(r)) >= policy.min_text_reviews:
        return recent, False
    return list(reviews), True


def count_topics(
    reviews: Sequence[SentimentReview],
) -> tuple[tuple[TopicCount, ...], tuple[TopicCount, ...]]:
    """Reviews per topic, praise and complaints apart, the most mentioned first.
    "Otro" is left out: it names nothing. At most MAX_LISTED_TOPICS of each."""
    counts: dict[str, dict[str, int]] = {PRAISE: {}, COMPLAINT: {}}
    for review in reviews:
        found = effective(review)
        for mention in found.topics if found is not None else ():
            if mention.topic != OTHER_TOPIC:
                bucket = counts[mention.polarity]
                bucket[mention.topic] = bucket.get(mention.topic, 0) + 1

    def top(bucket: dict[str, int]) -> tuple[TopicCount, ...]:
        ordered = sorted(bucket.items(), key=lambda item: (-item[1], item[0]))
        return tuple(TopicCount(topic, n) for topic, n in ordered[:MAX_LISTED_TOPICS])

    return top(counts[PRAISE]), top(counts[COMPLAINT])


def compute(reviews: Sequence[SentimentReview], now: datetime, policy: SentimentPolicy) -> Figures:
    """The figures of an analysis. With one review there is already a score; without
    any, the status is "insuficiente" and there is no score."""
    counted = [(r, c) for r in reviews if (c := effective(r)) is not None]
    with_text = sum(1 for r, _ in counted if has_text(r))
    if not counted:
        return Figures(STATUS_INSUFFICIENT, 0, 0, True, None, None, None)
    score = score_of([(c.sentiment, r.created_at) for r, c in counted], now, policy)
    scope, whole_history = _topic_scope([r for r, _ in counted], now, policy)
    praised, complaints = count_topics(scope)
    return Figures(
        status=STATUS_OK,
        based_on=len(counted),
        with_text=with_text,
        provisional=with_text < policy.min_text_reviews,
        period=period_label(policy, whole_history=whole_history),
        score=score,
        overall=overall_of(score, policy),
        praised=praised,
        complaints=complaints,
    )


# --- what needs the owner's attention: counted by the code ----------------------------


@dataclass(frozen=True)
class Attention:
    """What the classifications do not show: low ratings that came without a comment
    and reviews nobody has answered. Counted over the live reviews of the profile."""

    low_ratings: int = 0  # reviews of 1 or 2 stars, with or without text
    low_without_text: int = 0  # of those, the ones without a comment
    unanswered: int = 0  # reviews without a reply of the owner
    unanswered_low: int = 0  # of those, the ones of 1 or 2 stars


NO_ATTENTION = Attention()


def _is_low(review: SentimentReview) -> bool:
    return review.rating is not None and 1 <= review.rating <= LOW_RATING_MAX


def attention_of(reviews: Sequence[SentimentReview]) -> Attention:
    low = [r for r in reviews if _is_low(r)]
    pending = [r for r in reviews if not r.replied]
    return Attention(
        low_ratings=len(low),
        low_without_text=sum(1 for r in low if not has_text(r)),
        unanswered=len(pending),
        unanswered_low=sum(1 for r in pending if _is_low(r)),
    )


# --- what the model that writes the summary gets --------------------------------------

EXCERPT_MAX_CHARS = 280
EXCERPTS_PER_SIDE = 3
CLASSIFY_TEXT_MAX_CHARS = 1000


def pick_excerpts(
    reviews: Sequence[SentimentReview], now: datetime, policy: SentimentPolicy
) -> list[tuple[int, str]]:
    """A few recent reviews, stars and text only: the newest that are not positive and
    the newest positive ones. Always the same for the same reviews."""
    scope, _ = _topic_scope([r for r in reviews if effective(r) is not None], now, policy)
    with_text = sorted(
        (r for r in scope if has_text(r)),
        key=lambda r: (r.created_at or datetime.min.replace(tzinfo=UTC), r.id),
        reverse=True,
    )
    critical = [r for r in with_text if effective(r).sentiment != POSITIVE][:EXCERPTS_PER_SIDE]
    happy = [r for r in with_text if effective(r).sentiment == POSITIVE][:EXCERPTS_PER_SIDE]
    return [(r.rating or 0, clean_text(r.text, EXCERPT_MAX_CHARS)) for r in critical + happy]


# --- the texts: written by a model, accepted only if they agree with the figures ------

SUMMARY_MIN_LENGTH = 20
SUMMARY_MAX_LENGTH = 320
SUMMARY_MAX_SENTENCES = 2
SUGGESTION_MIN_LENGTH = 15
SUGGESTION_MAX_LENGTH = 220
SUGGESTION_MAX_SENTENCES = 2

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_WORD = re.compile(r"[a-záéíóúüñ]+")
_NUMBER_WORDS = {
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "cinco": 5,
    "seis": 6,
    "siete": 7,
    "ocho": 8,
    "nueve": 9,
    "diez": 10,
    "once": 11,
    "doce": 12,
    "trece": 13,
    "catorce": 14,
    "quince": 15,
    "dieciséis": 16,
    "dieciseis": 16,
    "diecisiete": 17,
    "dieciocho": 18,
    "diecinueve": 19,
    "veinte": 20,
}
_SPANISH = frozenset(
    "de la el los las que y en con por una un del se es más lo su sus para al".split()
)
_ENGLISH = frozenset(
    "the and is of with to are for this that your customers reviews most was were".split()
)


def allowed_numbers(
    figures: Figures, policy: SentimentPolicy, attention: Attention = NO_ATTENTION
) -> frozenset[int]:
    """The only figures a text about this analysis may quote."""
    numbers = {100, policy.window_months, figures.based_on, figures.with_text}
    if figures.score is not None:
        numbers.add(figures.score)
    numbers.update(t.mentions for t in figures.praised + figures.complaints)
    counts = (
        attention.low_ratings,
        attention.low_without_text,
        attention.unanswered,
        attention.unanswered_low,
    )
    numbers.update(n for n in counts if n > 0)
    return frozenset(numbers)


# "No se han registrado quejas", "sin quejas", "ninguna queja", "no hay críticas"...
_NO_COMPLAINTS = re.compile(
    r"\bno\s+(?:se\s+)?(?:han?|hay|existen?|constan?|aparecen?|tienes?|tiene|recibes?|recibe"
    r"|registran?|encuentran?|observan?|detectan?|aprecian?)\b[^.!?]{0,40}"
    r"\b(?:quejas?|cr[ií]ticas?|negativ[ao]s?)\b"
    r"|\bsin\s+(?:ninguna\s+)?(?:quejas?|cr[ií]ticas?)\b"
    r"|\bning[uú]n[ao]?\s+(?:queja|cr[ií]tica|valoraci[oó]n\s+negativa|rese[ñn]a\s+negativa)\b"
    r"|\bnadie\s+se\s+queja\b",
    re.IGNORECASE,
)


def denies_complaints(text: str) -> bool:
    """Whether a text says that nobody complains."""
    return _NO_COMPLAINTS.search(text) is not None


def _numbers_agree(text: str, allowed: frozenset[int]) -> bool:
    """No figure in the text contradicts the data: every number it quotes, in digits or
    in words, is one of the allowed ones. Decimals and percentages are never ours."""
    if "%" in text or "por ciento" in text.lower():
        return False
    for match in _NUMBER.findall(text):
        if not match.isdigit() or int(match) not in allowed:
            return False
    words = _WORD.findall(text.lower())
    return all(_NUMBER_WORDS[w] in allowed for w in words if w in _NUMBER_WORDS)


def _is_spanish(text: str) -> bool:
    words = _WORD.findall(text.lower())
    return sum(w in _SPANISH for w in words) >= 2 and not any(w in _ENGLISH for w in words)


def _valid_text(
    raw: object, allowed: frozenset[int], min_length: int, max_length: int, max_sentences: int
) -> str | None:
    if not isinstance(raw, str) or len(raw) > max_length * 4:
        return None
    text = clean_text(raw, max_length * 4)
    if not min_length <= len(text) <= max_length or text[-1] not in ".!?…":
        return None
    if not 1 <= len(_SENTENCE_END.findall(text)) <= max_sentences:
        return None
    if _has_emoji(text) or any(ch in text for ch in "<>{}[]"):
        return None
    if _CONTACT.search(text) or _PROMISE.search(text):
        return None
    if not _is_spanish(text) or not _numbers_agree(text, allowed):
        return None
    return text


def validate_summary(
    raw: object,
    figures: Figures,
    policy: SentimentPolicy,
    attention: Attention = NO_ATTENTION,
) -> str | None:
    """The summary a model wrote, ready to show, or None if it breaks any rule: two
    sentences at most, Spanish, no contact details, no promises and no figure other
    than those of the analysis. It may not say that there are no complaints when the
    profile has complaints or ratings of 1 or 2 stars. Never raises."""
    text = _valid_text(
        raw,
        allowed_numbers(figures, policy, attention),
        SUMMARY_MIN_LENGTH,
        SUMMARY_MAX_LENGTH,
        SUMMARY_MAX_SENTENCES,
    )
    if text is None:
        return None
    if (figures.complaints or attention.low_ratings > 0) and denies_complaints(text):
        return None
    return text


def validate_suggestion(
    raw: object,
    figures: Figures,
    policy: SentimentPolicy,
    attention: Attention = NO_ATTENTION,
) -> str | None:
    """Same rules as the summary, for the one action suggested to the owner."""
    return _valid_text(
        raw,
        allowed_numbers(figures, policy, attention),
        SUGGESTION_MIN_LENGTH,
        SUGGESTION_MAX_LENGTH,
        SUGGESTION_MAX_SENTENCES,
    )


# --- the texts by fixed rules: always available ---------------------------------------

_OVERALL_WORD = {
    OVERALL_POSITIVE: "positiva",
    OVERALL_MIXED: "mixta",
    OVERALL_NEGATIVE: "negativa",
}
SUMMARY_NO_REVIEWS = "Tu ficha todavía no tiene reseñas, así que aún no hay nada que analizar."
SUGGESTION_ASK_FIRST = (
    "Pide a tus clientes una reseña con el enlace de tu ficha: con las primeras opiniones "
    "verás aquí cómo te ven."
)
SUGGESTION_ASK_MORE = (
    "Pide a tus clientes una reseña con el enlace de tu ficha: con más opiniones con texto "
    "verás de qué habla la gente."
)
SUGGESTION_KEEP_GOING = (
    "Sigue pidiendo reseñas a los clientes satisfechos y responde a las que recibes."
)
SUGGESTION_DEFAULT = (
    "Responde a las reseñas negativas recientes y revisa lo que se repite en ellas."
)
_ADVICE = {
    "Trato del personal": "Comenta con tu equipo las quejas sobre el trato y acordad cómo "
    "atender en los momentos de más trabajo.",
    "Tiempo de espera": "Revisa las horas con más espera y avisa al cliente del tiempo "
    "previsto en cuanto llega.",
    "Calidad": "Revisa qué producto o servicio se repite en las quejas de calidad y corrígelo "
    "antes que nada.",
    "Comida y bebida": "Revisa qué platos o bebidas se repiten en las quejas y corrígelos "
    "antes que nada.",
    "Precio": "Explica mejor qué incluye cada precio en la ficha y en el local para que no "
    "haya sorpresas.",
    "Limpieza": "Refuerza la limpieza en las horas de más afluencia y revisa las zonas que "
    "mencionan las quejas.",
    "Ambiente": "Revisa el ruido, la temperatura y la comodidad del local en las horas que "
    "mencionan las quejas.",
    "Rapidez": "Revisa qué paso del servicio se retrasa más y simplifícalo.",
    "Atención posventa": "Fija un plazo de respuesta para las incidencias posteriores a la "
    "venta y cúmplelo.",
    "Variedad": "Valora ampliar la oferta en lo que más echan en falta las reseñas.",
    "Ubicación y acceso": "Explica en la ficha cómo llegar, dónde aparcar y cómo es el acceso.",
}


def low_ratings_note(attention: Attention) -> str | None:
    """The sentence about the ratings of 1 or 2 stars that came without a comment: no
    topic can be read from them, so the counts per topic do not show them."""
    count = attention.low_without_text
    if count <= 0:
        return None
    if count == 1:
        return "Hay 1 valoración de 1 o 2 estrellas sin comentario."
    return f"Hay {count} valoraciones de 1 o 2 estrellas sin comentario."


def complete_summary(summary: str, attention: Attention) -> str:
    """A summary, of a model or of the rules, with the sentence about the low ratings
    without a comment added when there are any. Written by the code, never by a model."""
    note = low_ratings_note(attention)
    return f"{summary} {note}" if note else summary


def rule_summary(figures: Figures, attention: Attention = NO_ATTENTION) -> str:
    """The summary built from the figures alone. Honest about how little it rests on."""
    return complete_summary(_rule_summary(figures), attention)


def _rule_summary(figures: Figures) -> str:
    if figures.status != STATUS_OK or figures.score is None:
        return SUMMARY_NO_REVIEWS
    if figures.provisional:
        basis = "1 valoración" if figures.based_on == 1 else f"{figures.based_on} valoraciones"
        return f"Basado en {basis}; aún hay pocas con texto para ver de qué habla la gente."
    reviews = "1 reseña" if figures.based_on == 1 else f"{figures.based_on} reseñas"
    first = (
        f"La valoración general es {_OVERALL_WORD[figures.overall]}: {figures.score} sobre 100 "
        f"a partir de {reviews}."
    )
    praised = figures.praised[0].topic if figures.praised else None
    complaint = figures.complaints[0].topic if figures.complaints else None
    if praised and complaint:
        return f"{first} Lo más elogiado es «{praised}» y la queja más repetida, «{complaint}»."
    if praised:
        return f"{first} Lo más elogiado es «{praised}»."
    if complaint:
        return f"{first} La queja más repetida es «{complaint}»."
    return first


def reply_low_suggestion(attention: Attention) -> str:
    """Unanswered reviews of 1 or 2 stars: those first."""
    low, pending = attention.unanswered_low, attention.unanswered
    if low == 1:
        first = "Responde primero a la reseña de 1 o 2 estrellas que sigue sin respuesta."
    else:
        first = f"Responde primero a las {low} reseñas de 1 o 2 estrellas que siguen sin respuesta."
    if pending > low:
        return f"{first} En total tienes {pending} reseñas sin responder."
    return first


def reply_suggestion(attention: Attention) -> str:
    """Unanswered reviews, none of them of 1 or 2 stars."""
    if attention.unanswered == 1:
        return "Responde a la reseña que sigue sin respuesta: quien la lea verá que la atiendes."
    return (
        f"Responde a las {attention.unanswered} reseñas que siguen sin respuesta: quien las "
        "lea verá que las atiendes."
    )


def choose_suggestion(
    figures: Figures, attention: Attention = NO_ATTENTION, advised: str | None = None
) -> str:
    """The one action suggested to the owner, chosen by fixed rules in this order:

    1. Unanswered reviews of 1 or 2 stars: answer those first.
    2. Unanswered reviews: answer them.
    3. A repeated complaint: act on that topic.
    4. Otherwise, the general advice.

    `advised` is the suggestion a model wrote, already validated, or None. It is used
    only in 3 and 4: a model never decides what comes first.
    """
    if figures.status != STATUS_OK:
        return SUGGESTION_ASK_FIRST
    if attention.unanswered_low > 0:
        return reply_low_suggestion(attention)
    if attention.unanswered > 0:
        return reply_suggestion(attention)
    if figures.provisional:
        return SUGGESTION_ASK_MORE
    if figures.complaints:
        return advised or _ADVICE.get(figures.complaints[0].topic, SUGGESTION_DEFAULT)
    if advised:
        return advised
    return SUGGESTION_KEEP_GOING if figures.overall == OVERALL_POSITIVE else SUGGESTION_DEFAULT


def rule_suggestion(figures: Figures, attention: Attention = NO_ATTENTION) -> str:
    """The suggestion without a model: the same rules, with the fixed advice."""
    return choose_suggestion(figures, attention, None)


def next_update_text(policy: SentimentPolicy) -> str:
    """When the analysis is computed again, ready to show."""
    reviews = (
        "1 reseña nueva"
        if policy.new_reviews_trigger == 1
        else f"{policy.new_reviews_trigger} reseñas nuevas"
    )
    return (
        f"Se actualiza con {reviews}, con una de 1 o 2 estrellas o a los "
        f"{policy.monthly_days} días."
    )


# --- history --------------------------------------------------------------------------

HISTORY_MONTHS = 12


def month_of(moment: datetime) -> date:
    """First day (UTC) of the month a snapshot belongs to."""
    return moment.astimezone(UTC).date().replace(day=1)


def first_history_month(now: datetime, months: int = HISTORY_MONTHS) -> date:
    """First month of a trend of that many months ending in the current one."""
    current_month = month_of(now)
    index = current_month.year * 12 + current_month.month - 1 - (months - 1)
    return date(index // 12, index % 12 + 1, 1)


@dataclass(frozen=True)
class MonthScore:
    month: date
    score: int


@dataclass(frozen=True)
class Usage:
    """What an analysis cost. Counters only: no text ever."""

    classify_calls: int = 0
    summary_calls: int = 0
    failed_calls: int = 0
    reviews_sent: int = 0
    input_tokens: int | None = None  # None: the provider did not say
    output_tokens: int | None = None


@dataclass(frozen=True)
class Analysis:
    """What is stored of an analysis and what the panel shows."""

    figures: Figures
    summary: str | None
    suggestion: str | None
    summary_source: str  # model or rules
    trigger: str
    generated_at: datetime
    formula_version: int = FORMULA_VERSION
