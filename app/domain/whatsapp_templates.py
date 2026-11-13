"""The WhatsApp templates Fichaitor sends once the customer service window is closed, and
how a message of the conversation becomes one of them. No I/O.

Outside the 24 hours since the owner last wrote, WhatsApp only takes approved templates
(Meta, "Template fundamentals": templates are "the only type of message that can be sent
to WhatsApp users outside of a customer service window"). Each template here is one that
the owner submits for approval in WhatsApp Manager (docs/whatsapp-plantillas.md), with
the same name, language, category, body and buttons. Fichaitor never creates them.

Rules, from docs/fase1/guion-conversacional.md, 3.2, so that Meta keeps them as utility:
- every template names the owner's business, and speaks of a fact, not an opportunity;
- no sales words in the fixed text, no business advice;
- no template is only variables, and none starts or ends with one.

What is confirmed is what was shown: a template that carries the suggested reply shows it
whole (on one line: a value of a template has no line breaks). If it does not fit, the
excerpt of the review gives way; if even then it does not fit, the generic notice is sent
and its button shows the whole message inside the window its press opens.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.domain.channel_messages import (
    BUTTONS,
    REVIEW_EXCERPT_MIN,
    TemplateFallback,
    one_line,
    quoted,
)

LANGUAGE = "es"  # Spanish, as the owner asked (Meta also has es_ES, es_MX, es_AR)
UTILITY, MARKETING = "utility", "marketing"
MAX_BODY = 1024  # Meta, "Components": body of a template, with the values filled in
MAX_QUICK_REPLY_TITLE = 25  # Meta, "Components"; ours stay within 20, as in a session
FOOTER = ""  # none: every character spent here is taken from the review

_PLACEHOLDER = re.compile(r"\{\{(\d+)\}\}")


@dataclass(frozen=True)
class Template:
    name: str
    category: str
    body: str  # with {{1}}, {{2}}...
    example: tuple[str, ...]  # one value per placeholder: Meta requires them
    buttons: tuple[str, ...] = ()  # quick reply titles, in order
    use: str = ""

    @property
    def placeholders(self) -> int:
        return len(set(_PLACEHOLDER.findall(self.body)))

    @property
    def fixed_length(self) -> int:
        return len(_PLACEHOLDER.sub("", self.body))

    def render(self, params: Sequence[str]) -> str:
        return _PLACEHOLDER.sub(lambda m: params[int(m.group(1)) - 1], self.body)


_REVIEW_BUTTONS = (BUTTONS["publish"], BUTTONS["edit"], BUTTONS["no_reply"])
_HOURS_BUTTONS = (BUTTONS["open_normal"], BUTTONS["closed_day"], BUTTONS["other_hours"])
SHOW_PROPOSAL = "Ver propuesta"
SHOW_NOTICE = "Ver aviso"

REVIEW = Template(
    name="resena_nueva_v1",
    category=UTILITY,
    body=(
        "Nueva reseña en la ficha de {{1}}: {{2}}, «{{3}}». Respuesta preparada: «{{4}}». "
        "¿La envío a Google?"
    ),
    example=(
        "Bar La Esquina",
        "5 estrellas",
        "El arroz, espectacular. Volveremos.",
        "Muchas gracias por tu reseña. Nos alegra que disfrutaras el arroz.",
    ),
    buttons=_REVIEW_BUTTONS,
    use="Reseña nueva con texto y respuesta propuesta (review.new.positive y negative)",
)
REVIEW_NO_TEXT = Template(
    name="resena_sin_texto_v1",
    category=UTILITY,
    body=(
        "Nueva reseña en la ficha de {{1}}: {{2}}, sin texto. Respuesta preparada: «{{3}}». "
        "¿La envío a Google?"
    ),
    example=("Bar La Esquina", "4 estrellas", "Gracias por tu valoración. Te esperamos pronto."),
    buttons=_REVIEW_BUTTONS,
    use="Reseña nueva sin texto, con respuesta propuesta (review.new.no_text)",
)
HOLIDAY = Template(
    name="festivo_v1",
    category=UTILITY,
    body=(
        "El {{1}} es festivo ({{2}}). La ficha de {{3}} mostrará tu horario normal si no "
        "se cambia. ¿Abres ese día?"
    ),
    example=("martes 8 de diciembre", "Inmaculada Concepción", "Bar La Esquina"),
    buttons=_HOURS_BUTTONS,
    use="Festivo (holiday.ask)",
)
SPECIAL_DAY = Template(
    name="dia_especial_v1",
    category=UTILITY,
    body=(
        "El {{1}} es {{2}}. La ficha de {{3}} mostrará tu horario normal si no se cambia. "
        "¿Abres ese día?"
    ),
    example=("jueves 24 de diciembre", "Nochebuena", "Bar La Esquina"),
    buttons=_HOURS_BUTTONS,
    use="Nochebuena y Nochevieja (eve.ask)",
)
REMINDER = Template(
    name="recordatorio_v1",
    category=UTILITY,
    body=(
        "Sigue pendiente de tu respuesta en la ficha de {{1}}: {{2}}. Caduca el {{3}}. "
        "Pulsa «Ver propuesta» y te la enseño aquí."
    ),
    example=("Bar La Esquina", "la reseña de 5 estrellas", "lunes 16 de noviembre"),
    buttons=(SHOW_PROPOSAL,),
    use="Recordatorio de una propuesta (reminder.pending)",
)
RESULT = Template(
    name="resultado_cambio_v1",
    category=UTILITY,
    body=(
        "Sobre el cambio que confirmaste en la ficha de {{1}}: {{2}} Tienes el detalle en "
        "tu panel de Fichaitor."
    ),
    example=(
        "Bar La Esquina",
        "Respuesta enviada a Google. Suele verse en unos minutos.",
    ),
    use="Resultado de un cambio que el dueño confirmó (review.sent, holiday.sent_*, "
    "change.sent, google.failed...)",
)
NOTICE = Template(
    name="aviso_pendiente_v1",
    category=UTILITY,
    body=(
        "Hay un aviso de la ficha de {{1}} pendiente de tu respuesta. Pulsa «Ver aviso» y "
        "te lo enseño aquí."
    ),
    example=("Bar La Esquina",),
    buttons=(SHOW_NOTICE,),
    use="Cualquier otra propuesta, o una que no cabe en su plantilla",
)

TEMPLATES: tuple[Template, ...] = (
    REVIEW,
    REVIEW_NO_TEXT,
    HOLIDAY,
    SPECIAL_DAY,
    REMINDER,
    RESULT,
    NOTICE,
)

# Business and dates: bounded so that the review keeps its room.
_BUSINESS_MAX = 60
_SHORT_MAX = 80
_RESULT_MAX = 600


def param(value: object, max_length: int) -> str:
    """A value of a template: one line (no line breaks, tabs nor runs of spaces), bounded.
    Empty values are not accepted by Meta: a dash stands for nothing."""
    return one_line(value, max_length) or "-"


def fill(
    template: Template, params: Sequence[str], button_ids: Sequence[str] = ()
) -> TemplateFallback:
    """The fallback of a message, or ValueError if it does not fit the template."""
    if len(params) != template.placeholders:
        raise ValueError(f"{template.name}: {len(params)} valores para {template.placeholders}")
    if len(button_ids) != len(template.buttons):
        raise ValueError(f"{template.name}: {len(button_ids)} botones para {len(template.buttons)}")
    values = tuple(params)
    if any(not v.strip() or v != one_line(v, len(v)) for v in values):
        raise ValueError(f"{template.name}: valor vacío o con saltos de línea")
    if len(template.render(values)) > MAX_BODY:
        raise ValueError(f"{template.name}: no cabe")
    return TemplateFallback(template.name, values, tuple(button_ids), template.category)


def notice(business: str, show_id: str) -> TemplateFallback:
    return fill(NOTICE, (param(business, _BUSINESS_MAX),), (show_id,))


def _review(
    business: str, values: Mapping[str, str], button_ids: Sequence[str]
) -> TemplateFallback:
    name = param(business, _BUSINESS_MAX)
    stars = param(values["estrellas"], _SHORT_MAX)
    reply = one_line(values["respuesta"], 10_000)  # never cut: it is what gets confirmed
    if not reply:
        raise ValueError("reseña sin respuesta propuesta")
    review = quoted(values.get("resena", ""), 10_000)
    if not review:
        return fill(REVIEW_NO_TEXT, (name, stars, reply), button_ids)
    room = MAX_BODY - REVIEW.fixed_length - len(name) - len(stars) - len(reply)
    if room < min(REVIEW_EXCERPT_MIN, len(review)):
        raise ValueError("la respuesta no deja sitio para la reseña")
    return fill(REVIEW, (name, stars, quoted(review, room), reply), button_ids)


def for_proposal(
    key: str,
    business: str,
    values: Mapping[str, str],
    button_ids: Sequence[str],
    show_id: str,
) -> TemplateFallback:
    """The template of the opening message of a proposal (its catalog `key`), with the
    button ids of the message, or the generic notice when there is none for that key or
    the values do not fit. `values`: estrellas, resena, respuesta (reviews); fecha_larga,
    nombre (key dates)."""
    try:
        if key in ("review.new.positive", "review.new.negative", "review.new.no_text"):
            return _review(business, values, button_ids)
        if key in ("holiday.ask", "eve.ask"):
            template = HOLIDAY if key == "holiday.ask" else SPECIAL_DAY
            return fill(
                template,
                (
                    param(values["fecha_larga"], _SHORT_MAX),
                    param(values["nombre"], _SHORT_MAX),
                    param(business, _BUSINESS_MAX),
                ),
                button_ids,
            )
    except (KeyError, ValueError):
        pass
    return notice(business, show_id)


def reminder(business: str, summary: str, last_day: str, show_id: str) -> TemplateFallback:
    return fill(
        REMINDER,
        (
            param(business, _BUSINESS_MAX),
            param(summary, _SHORT_MAX + 40),
            param(last_day, _SHORT_MAX),
        ),
        (show_id,),
    )


def result(business: str, text: str) -> TemplateFallback:
    """The outcome of a change the owner confirmed, as the session message says it."""
    return fill(RESULT, (param(business, _BUSINESS_MAX), param(text, _RESULT_MAX)))
