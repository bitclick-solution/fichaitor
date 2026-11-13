"""The communications written in the backoffice (docs/admin-facturacion.md, annex
«Comunicaciones por correo desde el backoffice»; template `comunicacion` of
brand/correo/LEEME.md, 12).

Two kinds, and only the footer changes:

- `servicio`: changes in the product or in the terms, incidents, end of the beta. To every
  account of the audience; no unsubscribe link, because it sells nothing.
- `comercial`: offers, news meant to sell, newsletter. Only to accounts that did not opt
  out; every email carries its one-click unsubscribe link and the List-Unsubscribe headers
  (RFC 8058).

What the person writes is data, never HTML: every value is escaped with html.escape, the
paragraphs too. Only ${contenido} and ${baja} go in as they are, because they are blocks
already filled and escaped. The limits are the ones of LEEME.md, checked here and again by
the database.
"""

import re
from dataclasses import dataclass
from html import escape
from urllib.parse import urlsplit

from app.domain.offboarding_mail import _PLAIN_FOOTER, _block
from app.mail_templates import BASE, BLOCKS
from app.ports.mailer import Email

TEMPLATE = "comunicacion"

SERVICE = "servicio"
COMMERCIAL = "comercial"
KINDS = (SERVICE, COMMERCIAL)

MOTIVE = {
    SERVICE: "Te escribimos porque tienes una cuenta en Fichaitor y esto afecta a tu servicio.",
    COMMERCIAL: "Te escribimos porque eres cliente de Fichaitor.",
}
UNSUBSCRIBE_LABEL = "Darme de baja de estas comunicaciones"

# Audiences: every active account, the beta only, one plan or one account (a support
# case). Never the waiting list, never addresses typed by hand.
AUDIENCE_ALL = "todas"
AUDIENCE_BETA = "beta"
AUDIENCE_PLAN = "plan"
AUDIENCE_ACCOUNT = "cuenta"
AUDIENCES = (AUDIENCE_ALL, AUDIENCE_BETA, AUDIENCE_PLAN, AUDIENCE_ACCOUNT)
PLANS = ("gratis", "pro", "pro_whatsapp")

# brand/correo/LEEME.md, 12: recommended limits, checked by the backoffice and here.
MAX_SUBJECT = 90
MAX_PREHEADER = 120
MAX_HEADLINE = 40
MAX_BUTTON_LABEL = 30
MAX_BUTTON_URL = 500
MAX_PARAGRAPHS = 8
MAX_PARAGRAPH = 1000

# The only place a button may point to (LEEME.md, 12): checked when the draft is saved.
BUTTON_HOST = "fichaitor.es"
_LINE_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PARAGRAPH_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")  # a line break is allowed
_URL_FORBIDDEN = re.compile(r"[\x00-\x20\x7f\"'<>\\`]")


class CommunicationInvalid(ValueError):
    """A field breaks a rule. `field` names it; the message is for the screens and never
    repeats the value."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


@dataclass(frozen=True)
class Content:
    """What the person writes: the same in the preview, the test and every email."""

    kind: str
    subject: str
    preheader: str
    headline: str
    paragraphs: tuple[str, ...]
    button_label: str | None = None
    button_url: str | None = None

    @classmethod
    def from_row(cls, row: dict) -> "Content":
        """From the JSON the database gives (admin_communication_get and the worker)."""
        return cls(
            kind=row["kind"],
            subject=row["subject"],
            preheader=row["preheader"],
            headline=row["headline"],
            paragraphs=tuple(row["paragraphs"]),
            button_label=row.get("button_label"),
            button_url=row.get("button_url"),
        )


def check_button_url(url: str) -> str:
    """The link of the button: absolute, https and on fichaitor.es, nothing else. No user,
    no port, no other host (fichaitor.es.evil.com, evil.com/fichaitor.es), no spaces nor
    quotes that could break out of href."""
    if len(url) > MAX_BUTTON_URL or _URL_FORBIDDEN.search(url):
        raise CommunicationInvalid("button_url", "El enlace del botón no es válido.")
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc != BUTTON_HOST
        or not parts.path.startswith("/")
        or not url.startswith(f"https://{BUTTON_HOST}/")
    ):
        raise CommunicationInvalid(
            "button_url", "El enlace del botón solo puede ser https://fichaitor.es/…"
        )
    return url


def _line(field: str, value: str, limit: int, name: str) -> str:
    value = (value or "").strip()
    if not value:
        raise CommunicationInvalid(field, f"Falta {name}.")
    if len(value) > limit:
        raise CommunicationInvalid(field, f"{name.capitalize()}: {limit} caracteres como mucho.")
    if _LINE_CONTROL.search(value):
        raise CommunicationInvalid(field, f"{name.capitalize()}: una sola línea.")
    return value


def validate(content: Content) -> Content:
    """The content with its values trimmed, or CommunicationInvalid."""
    if content.kind not in KINDS:
        raise CommunicationInvalid("kind", "El tipo es servicio o comercial.")
    subject = _line("subject", content.subject, MAX_SUBJECT, "el asunto")
    preheader = _line("preheader", content.preheader, MAX_PREHEADER, "el preencabezado")
    headline = _line("headline", content.headline, MAX_HEADLINE, "el titular")
    if headline.endswith("."):
        raise CommunicationInvalid("headline", "El titular va sin punto final.")
    paragraphs = tuple((p or "").strip() for p in content.paragraphs)
    if not 1 <= len(paragraphs) <= MAX_PARAGRAPHS:
        raise CommunicationInvalid("paragraphs", "De 1 a 8 párrafos.")
    for text in paragraphs:
        if not text:
            raise CommunicationInvalid("paragraphs", "Hay un párrafo vacío.")
        if len(text) > MAX_PARAGRAPH:
            raise CommunicationInvalid("paragraphs", "Cada párrafo, 1000 caracteres como mucho.")
        if _PARAGRAPH_CONTROL.search(text):
            raise CommunicationInvalid("paragraphs", "Un párrafo lleva caracteres no válidos.")
    label = (content.button_label or "").strip() or None
    url = (content.button_url or "").strip() or None
    if (label is None) != (url is None):
        raise CommunicationInvalid("button_url", "El botón necesita la etiqueta y el enlace.")
    if label is not None and url is not None:
        label = _line("button_label", label, MAX_BUTTON_LABEL, "la etiqueta del botón")
        url = check_button_url(url)
    return Content(content.kind, subject, preheader, headline, paragraphs, label, url)


def communication_email(to: str, content: Content, unsubscribe_url: str | None = None) -> Email:
    """The email of a communication, HTML and plain text (LEEME.md, 12).

    `unsubscribe_url`: required for a commercial one (the ${enlace_baja} of the account,
    made from the configuration and a token); never for a service one. The headers
    List-Unsubscribe and List-Unsubscribe-Post are added by the job (unsubscribe.headers)."""
    commercial = content.kind == COMMERCIAL
    if commercial and not unsubscribe_url:
        raise ValueError("una comunicación comercial necesita su enlace de baja")
    if not commercial and unsubscribe_url:
        raise ValueError("una comunicación de servicio no lleva enlace de baja")
    first, *rest = content.paragraphs
    blocks = [_block("parrafo", texto=first)]
    plain = [first]
    if content.button_label and content.button_url:
        blocks.append(_block("boton", etiqueta=content.button_label, enlace=content.button_url))
        plain.append(f"{content.button_label}: {content.button_url}")
    blocks += [_block("parrafo", texto=p) for p in rest]
    plain += rest
    motive = MOTIVE[content.kind]
    baja = (
        BLOCKS["baja"].substitute(enlace_baja=escape(unsubscribe_url))
        if commercial and unsubscribe_url
        else ""
    )
    html = BASE.substitute(
        asunto=escape(content.subject),
        preencabezado=escape(content.preheader),
        titular=escape(content.headline),
        contenido="\n".join(blocks),  # already escaped, block by block
        motivo=escape(motive),
        baja=baja,
    )
    footer = motive
    if commercial:
        footer += f"\n{UNSUBSCRIBE_LABEL}: {unsubscribe_url}"
    text = "\n\n".join([content.headline.upper(), *plain, footer]) + "\n\n" + _PLAIN_FOOTER
    return Email(to, content.subject, text, html)
