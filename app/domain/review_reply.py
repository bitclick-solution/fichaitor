"""Replies to reviews: pure rules, no I/O.

Two different texts go through here and they are held to different rules:

- A **draft** is written by a model from a review a third party wrote. It is untrusted:
  `validate_reply` discards it unless it meets the house rules (tone, length, no
  contact details, no offers, no promises).
- The **owner's reply** is what the owner confirms for publication, whether it started
  as a draft or not. It is theirs to word: `check_owner_reply` only checks what Google
  itself requires. It never changes the text: what is confirmed is what is sent.
"""

import hashlib
import re
import unicodedata

LOW_STARS = 2  # 1 and 2 stars: the reviews that hurt most when left unanswered

REPLY_MIN_LENGTH = 40
REPLY_MAX_LENGTH = 600
REPLY_MIN_SENTENCES = 2
REPLY_MAX_SENTENCES = 4

# ReviewReply.comment: "The maximum length is 4096 bytes."
# https://developers.google.com/my-business/reference/rest/v4/accounts.locations.reviews#ReviewReply
GOOGLE_REPLY_MAX_BYTES = 4096

# How much of a review is sent to the model that drafts the reply.
DRAFT_REVIEW_MAX_LENGTH = 2000

MSG_REPLY_EMPTY = "Escribe la respuesta antes de enviarla."
MSG_REPLY_TOO_LONG = (
    "La respuesta es demasiado larga. Google admite 4.096 bytes, unos 4.000 caracteres."
)
MSG_REPLY_BAD_CHARACTERS = "La respuesta tiene caracteres que Google no admite. Revísala."


def clean_text(value: object, max_length: int) -> str:
    """One line of printable text, cut to a maximum. Anything that is not text is empty."""
    if not isinstance(value, str):
        return ""
    # Control and format characters (line breaks, bidi overrides, zero width) go away.
    printable = "".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in value)
    return " ".join(printable.split())[:max_length].strip()


# --- the draft: written by a model, accepted only if it meets the rules ----------------

_SENTENCE_END = re.compile(r"[.!?…]+(?=\s|$)")
_CONTACT = re.compile(
    r"@|https?:|www\.|\.(?:com|es|net|org)\b|(?:\d[\s.\-]?){6,}|whatsapp|telegram", re.IGNORECASE
)
_OFFER = re.compile(
    r"descuento|gratis|gratuit|regal|invita(?:mos|ción|cion|remos)|reembols|devolveremos"
    r"|devolución|devolucion|compensa|cupón|cupon|oferta|promoci|vale\s+de|bono|[%€$]",
    re.IGNORECASE,
)
_PROMISE = re.compile(
    r"promet|garantiz|nos comprometemos|me comprometo|te aseguramos|le aseguramos"
    r"|os aseguramos|no volverá|no se repetirá",
    re.IGNORECASE,
)
_APOLOGY = re.compile(r"sentimos|siento|lamenta|disculp|perdón|perdon", re.IGNORECASE)
_THANKS = re.compile(r"gracias|agradec", re.IGNORECASE)
_PRIVATE = re.compile(r"privado|en persona|personalmente|directamente", re.IGNORECASE)


def _has_emoji(text: str) -> bool:
    return any(
        unicodedata.category(ch) in ("So", "Sk", "Cs", "Co")
        or 0x1F000 <= ord(ch) <= 0x1FAFF
        or 0xFE00 <= ord(ch) <= 0xFE0F
        for ch in text
    )


def validate_reply(raw: object, stars: int) -> str | None:
    """The reply ready to show, or None if it breaks any rule. Never raises.

    Rules: one paragraph of 2 to 4 sentences within the length limits; no emojis; no
    contact details, links or figures that look like a phone; no offers or
    compensations; no promises; an apology and an invitation to talk in private for 1
    and 2 stars; thanks for 4 and 5.
    """
    if not isinstance(raw, str) or len(raw) > REPLY_MAX_LENGTH * 4:
        return None
    reply = clean_text(raw, REPLY_MAX_LENGTH * 4)
    if not REPLY_MIN_LENGTH <= len(reply) <= REPLY_MAX_LENGTH:
        return None
    if reply[-1] not in ".!?…":
        return None
    if not REPLY_MIN_SENTENCES <= len(_SENTENCE_END.findall(reply)) <= REPLY_MAX_SENTENCES:
        return None
    if _has_emoji(reply) or any(ch in reply for ch in "<>{}[]"):
        return None
    if _CONTACT.search(reply) or _OFFER.search(reply) or _PROMISE.search(reply):
        return None
    if stars <= LOW_STARS and not (_APOLOGY.search(reply) and _PRIVATE.search(reply)):
        return None
    if stars >= 4 and not _THANKS.search(reply):
        return None
    if stars == 3 and not (_THANKS.search(reply) or _APOLOGY.search(reply)):
        return None
    return reply


def draft_source_hash(prompt_version: str, stars: int, review_text: str) -> str:
    """What a stored draft was written from. A different hash means the review or the
    prompt has changed and the draft must be written again."""
    source = f"{prompt_version}\n{stars}\n{review_text}"
    return hashlib.sha256(source.encode()).hexdigest()


# --- the owner's reply: theirs to word, checked only against what Google accepts -------

_ALLOWED_CONTROL = "\n\r\t"


class InvalidReplyText(ValueError):
    """The text cannot be sent to Google. The message is in Spanish, ready to show."""


def check_owner_reply(text: object) -> str:
    """Return the text exactly as it came, or raise InvalidReplyText.

    Nothing is trimmed, normalised or corrected here: the owner confirms an exact
    text and that exact text is what Google receives. A text that cannot be sent as
    it is gets rejected, so that the owner changes it and confirms again.
    """
    if not isinstance(text, str) or not text.strip():
        raise InvalidReplyText(MSG_REPLY_EMPTY)
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:  # a lone surrogate: not valid text
        raise InvalidReplyText(MSG_REPLY_BAD_CHARACTERS) from None
    if size > GOOGLE_REPLY_MAX_BYTES:
        raise InvalidReplyText(MSG_REPLY_TOO_LONG)
    if any(unicodedata.category(ch) == "Cc" and ch not in _ALLOWED_CONTROL for ch in text):
        raise InvalidReplyText(MSG_REPLY_BAD_CHARACTERS)
    return text
