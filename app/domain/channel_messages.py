"""Messages to and from a channel, independent of which channel it is. No I/O.

- `OutboundMessage` is the shape of `docs/03-lld.md` (text, buttons with `id` and
  `title`, fallback template). `validate` holds every message to the strictest
  limits of the channels, so that the same message works on Telegram today and on
  WhatsApp later.
- `InboundEvent` is what an adapter turns an update of its channel into.
- The catalog holds the texts and button titles of
  `docs/fase1/guion-conversacional.md`, by key. The service never writes a text of
  its own: it renders a key.
- A button id is opaque: proposal, step token and option. It says nothing about the
  business, the review or the change, and it is not enough to act: the service
  checks whom the proposal belongs to.
"""

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

# --- limits ------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelLimits:
    max_body: int  # characters of the text
    max_buttons: int
    max_button_title: int  # characters
    max_button_id_bytes: int


# WhatsApp reply buttons: at most 3, titles of 20 characters, body of 1,024
# (docs/fase0/whatsapp.md). Checked now although WhatsApp comes later.
WHATSAPP_LIMITS = ChannelLimits(
    max_body=1024, max_buttons=3, max_button_title=20, max_button_id_bytes=256
)
# Telegram Bot API 10.4: text of 1-4096 characters, callback_data of 1-64 bytes
# (https://core.telegram.org/bots/api#sendmessage and #inlinekeyboardbutton). The API
# documents no maximum of buttons nor of title length: 8 and 64 are our own caps.
TELEGRAM_LIMITS = ChannelLimits(
    max_body=4096, max_buttons=8, max_button_title=64, max_button_id_bytes=64
)
# What every message of the conversation must meet: the strictest of each.
CORE_LIMITS = ChannelLimits(
    max_body=min(WHATSAPP_LIMITS.max_body, TELEGRAM_LIMITS.max_body),
    max_buttons=min(WHATSAPP_LIMITS.max_buttons, TELEGRAM_LIMITS.max_buttons),
    max_button_title=min(WHATSAPP_LIMITS.max_button_title, TELEGRAM_LIMITS.max_button_title),
    max_button_id_bytes=min(
        WHATSAPP_LIMITS.max_button_id_bytes, TELEGRAM_LIMITS.max_button_id_bytes
    ),
)


# --- outbound ----------------------------------------------------------------------


class MessageKind(StrEnum):
    PROPOSAL = "proposal"  # the agent takes the initiative
    REPLY = "reply"  # answers something the owner just sent
    RESULT = "result"  # outcome of a change the owner confirmed
    SYSTEM = "system"  # linking, unknown chats


@dataclass(frozen=True)
class Button:
    id: str
    title: str


@dataclass(frozen=True)
class OutboundMessage:
    to: str  # channel_identity id; the adapter gets the address apart
    kind: MessageKind
    text: str
    buttons: tuple[Button, ...] = ()
    template_fallback: str | None = None  # WhatsApp outside the 24 hour window
    key: str = ""  # catalog key, for the record
    proposal_id: str | None = None


class InvalidMessage(ValueError):
    pass


_BUTTON_ID = re.compile(r"[A-Za-z0-9_.:\-]+")


def _has_control(text: str, allowed: str = "") -> bool:
    return any(unicodedata.category(ch)[0] == "C" and ch not in allowed for ch in text)


def validate(message: OutboundMessage, limits: ChannelLimits = CORE_LIMITS) -> OutboundMessage:
    """The same message, or InvalidMessage. Nothing is cut here: a message that does
    not fit is a bug of whoever built it, not something to fix silently."""
    if not message.to:
        raise InvalidMessage("mensaje sin destinatario")
    if not message.text.strip():
        raise InvalidMessage("mensaje sin texto")
    if len(message.text) > limits.max_body:
        raise InvalidMessage(f"texto de {len(message.text)} caracteres: máximo {limits.max_body}")
    if _has_control(message.text, allowed="\n"):
        raise InvalidMessage("el texto tiene caracteres de control")
    if len(message.buttons) > limits.max_buttons:
        raise InvalidMessage(f"{len(message.buttons)} botones: máximo {limits.max_buttons}")
    ids: set[str] = set()
    titles: set[str] = set()
    for button in message.buttons:
        title = button.title
        if not title.strip() or len(title) > limits.max_button_title or _has_control(title):
            raise InvalidMessage(f"título de botón no válido: {title!r}")
        if not _BUTTON_ID.fullmatch(button.id):
            raise InvalidMessage("identificador de botón no válido")
        if len(button.id.encode()) > limits.max_button_id_bytes:
            raise InvalidMessage("identificador de botón demasiado largo")
        if button.id in ids or title in titles:
            raise InvalidMessage("botones repetidos")
        ids.add(button.id)
        titles.add(title)
    return message


# --- button ids --------------------------------------------------------------------

PROPOSAL_ID = re.compile(r"prop_[0-9a-f]{16}")
_OPAQUE_BUTTON = re.compile(r"(prop_[0-9a-f]{16})\.([0-9a-f]{6})\.([a-z_]{1,24})")


@dataclass(frozen=True)
class ButtonPress:
    proposal_id: str
    step_token: str
    option: str


def encode_button_id(proposal_id: str, step_token: str, option: str) -> str:
    value = f"{proposal_id}.{step_token}.{option}"
    if not _OPAQUE_BUTTON.fullmatch(value):
        raise InvalidMessage("identificador de botón no válido")
    return value


def decode_button_id(value: object) -> ButtonPress | None:
    """None for anything that is not exactly one of our ids. Never raises: the value
    comes from outside."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    match = _OPAQUE_BUTTON.fullmatch(value)
    return ButtonPress(*match.groups()) if match else None


# --- inbound -----------------------------------------------------------------------


class InboundKind(StrEnum):
    TEXT = "text"
    BUTTON = "button"
    START = "start"  # the channel's "open the bot" command, with or without a code
    COMMAND = "command"  # one of COMMANDS; `text` is its name


# The only commands besides "start". Anything else that looks like a command is not
# attended. A command is a fixed answer: it never reaches a proposal.
COMMAND_HELP = "ayuda"
COMMAND_PRIVACY = "privacidad"
COMMANDS = (COMMAND_HELP, COMMAND_PRIVACY)


@dataclass(frozen=True)
class InboundEvent:
    channel: str
    address: str  # the chat, as the channel names it
    event_id: str  # unique per channel: the same event twice is processed once
    kind: InboundKind
    text: str = ""  # TEXT: the message. START: the code, if any
    button_id: str = ""
    callback_id: str = ""  # what the channel wants acknowledged
    message_ref: str = ""  # BUTTON: the message the button was on
    sender_name: str = ""


# --- text written by third parties ---------------------------------------------------

REVIEW_EXCERPT_MAX = 280
REVIEW_EXCERPT_MIN = 40
# The suggested reply is never cut: the owner confirms exactly what is shown.
SHOWN_REPLY_MAX = 700
# Room kept in every proposal for the line that goes before it in a reminder or when
# it is taken up again ("Sigue pendiente: ...").
PREFIX_RESERVE = 120
NAME_MAX = 40


def one_line(value: object, max_length: int) -> str:
    """One line of printable text, cut to a maximum with an ellipsis."""
    if not isinstance(value, str):
        return ""
    printable = "".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in value)
    text = " ".join(printable.split())
    if len(text) > max_length:
        text = text[: max(max_length - 1, 0)].rstrip() + "…"
    return text


def quoted(value: object, max_length: int) -> str:
    """Third party text made safe to show between «»: one line, bounded, and unable
    to close the quotation and pass for words of the agent."""
    return one_line(value, max_length).replace("«", "“").replace("»", "”")


# --- catalog -----------------------------------------------------------------------
# Texts of docs/fase1/guion-conversacional.md, section 9. Differences, all explained
# in docs/canales.md: review messages do not name the author (it is not stored),
# "{canal}" and "{este_chat}" replace "WhatsApp" and "Este número", "link.moved"
# replaces "link.already_linked", and the keys marked NEW do not exist in the script.

TEXTS: dict[str, str] = {
    "change.confirm": "Esto es lo que envío a Google:\n{cambios}\n¿Lo envío?",
    "change.edit_ask": "Dime qué cambio y te lo vuelvo a enseñar.",
    "change.sent": (
        "Enviado a Google: {cambio}. Suele verse en unos minutos; a veces Google tarda "
        "más en revisarlo. {enlace_ficha}"
    ),
    "generic.cancelled": "Vale, no cambio nada.",
    "generic.panel_link": "Lo tienes aquí: {enlace_panel}",
    "generic.neutral": "Yo me ocupo de tu ficha de Google. Si necesitas algo de eso, dímelo.",
    "queue.resume": "Seguimos con lo de antes: {resumen_propuesta}.",
    "google.failed": (
        "No he podido enviar el cambio a Google. No se ha cambiado nada en tu ficha. "
        "Puedes hacerlo desde el panel: {enlace_panel}"
    ),
    "google.rejected": "Google no ha aceptado el cambio: {motivo}. Tu ficha sigue como estaba.",
    "welcome.linked": (
        "Listo{nombre}. Ya llevo la ficha de {negocio} desde aquí.\n"
        "Te aviso de las reseñas nuevas y de los festivos, y puedes pedirme cambios "
        "cuando quieras.\n"
        "No cambio nada sin que me lo confirmes."
    ),
    "link.code_invalid": (
        "Ese código no me consta o ha caducado. Genera uno nuevo en el panel y "
        "envíamelo: {enlace_panel}"
    ),
    # NEW (decision of the owner, 2026-11-12): an address belongs to one account
    # at a time; linking it to another unlinks it from the previous one, with notice.
    "link.moved": "{este_chat} estaba vinculado a otra cuenta. Lo he desvinculado de ella.",
    "link.blocked": (
        "Llevas varios códigos incorrectos. Espera una hora y genera uno nuevo en el "
        "panel: {enlace_panel}"
    ),
    "review.new.positive": (
        "Nueva reseña, {estrellas}: «{extracto}»\nRespuesta que propongo: «{respuesta}»\n¿La envío?"
    ),
    "review.new.negative": (
        "Reseña de {estrellas}: «{extracto}»\n"
        "Conviene contestar con calma y sin entrar al detalle. Propongo: «{respuesta}»\n"
        "¿La envío?"
    ),
    "review.new.no_text": (
        "Nueva reseña, {estrellas}, sin texto.\nRespuesta que propongo: «{respuesta}»\n¿La envío?"
    ),
    "review.sent": "Respuesta enviada a Google. Suele verse en unos minutos. {enlace_reseña}",
    "review.skipped": "Hecho. La dejo sin respuesta.",
    "review.edit_ask": "Escríbeme o díctame tu respuesta y la envío tal cual.",
    "review.edit_confirm": "He entendido esto: «{texto}»\n¿Lo envío como respuesta?",
    "review.clarify_text": "¿Quieres que envíe esto como respuesta a la reseña? «{texto}»",
    "review.clarify_options": "Dime qué hago con la reseña.",
    "review.gone": "Esa reseña ya no está en Google. No hay nada que responder.",
    "holiday.ask": (
        "El {fecha_larga} es festivo ({nombre_festivo}). ¿Abres?\n"
        "Si no me dices nada, tu ficha mostrará el horario normal."
    ),
    "holiday.sent_open": (
        "Enviado a Google: el {fecha_larga} abres con tu horario normal{horario}. {enlace_ficha}"
    ),
    "holiday.sent_closed": (
        "Enviado a Google: {fecha_larga}, cerrado. Suele verse en unos minutos. {enlace_ficha}"
    ),
    "hours.ask": "¿Qué horario haces el {fecha_larga}? Por ejemplo: de 10 a 14.",
    "hours.clarify_1": (
        "¿A qué hora abres y a qué hora cierras ese día? Dímelo con horas, por ejemplo: "
        "de 10:00 a 14:00."
    ),
    "hours.clarify_2": (
        "Sigo sin tenerlo claro. Dime la hora de apertura y la de cierre. Si partes el "
        "día: de 10 a 14 y de 17 a 20."
    ),
    "hours.options": "Elige una opción y lo dejamos resuelto.",
    "keydate.ask": (
        "Se acerca {evento} ({fecha_larga}). ¿Tienes una oferta o un horario distinto?\n"
        "Una oferta publicada con tiempo se ve más días."
    ),
    "keydate.declined": "Anotado. No te lo vuelvo a proponer este año.",
    "reminder.pending": "Sigue pendiente: {resumen_propuesta}. Caduca el {fecha_larga}.",
    "expired.past": (
        "Esa propuesta era para el {fecha_larga} y ya ha pasado. No he cambiado nada en tu ficha."
    ),
    "reconnect.needed": (
        "Antes tengo que recuperar el acceso a tu ficha. Tarda un minuto: {enlace_reconectar}"
    ),
    "fallback.first": (
        "No te he entendido. ¿Me lo dices de otra forma? Puedo responder reseñas, "
        "cambiar horarios y resolver dudas de tu ficha."
    ),
    "fallback.other": (
        "Escríbeme qué necesitas con una frase. Si prefieres hacerlo tú, el panel está "
        "aquí: {enlace_panel}"
    ),
    "unlinked.reply": (
        "Hola. Soy Fichaitor: llevo la ficha de Google de tu negocio desde {canal}.\n"
        "{este_chat} no está vinculado a ninguna cuenta. Si ya tienes una, envíame el "
        "código de 6 dígitos del panel. Si no, empieza aquí: {enlace_alta}"
    ),
    # NEW: a button that no longer does anything. Shown as the channel's notice.
    "stale.resolved": "Esa propuesta ya está resuelta. No he cambiado nada más.",
    "stale.inactive": "Ese botón ya no está activo. Usa los del último mensaje.",
    # NEW: a proposal that waits behind the one the owner has open.
    "queue.wait": (
        "Esa propuesta está en espera. Primero cerramos la última que te he enviado y "
        "después seguimos con esta."
    ),
    # NEW: a reply too long to be shown whole, and so impossible to confirm here.
    "review.edit_too_long": (
        "Tu respuesta es demasiado larga para enseñártela entera aquí. Acórtala o "
        "envíala desde el panel: {enlace_panel}"
    ),
    # NEW: what is left on the message once its buttons are removed.
    "fmt.choice": "Tu respuesta: {titulo}",
    # NEW (2026-11-12, B3): too many wrong codes from every chat together. Typed codes
    # wait; the link of the panel still works.
    "link.paused": (
        "Ahora mismo no puedo comprobar códigos escritos. Abre el enlace de vinculación "
        "del panel desde este móvil: {enlace_panel}"
    ),
    # NEW: the two commands besides /start.
    "command.help": (
        "Soy Fichaitor. Te aviso de las reseñas nuevas de tu ficha de Google y te "
        "propongo una respuesta. No publico nada sin que me lo confirmes.\n"
        "Para vincular {este_chat_minusculas}, abre el enlace del panel o envíame el "
        "código de 6 dígitos: {enlace_panel}\n"
        "Comandos: /ayuda y /privacidad."
    ),
    "command.privacy": (
        "Guardo {este_chat_minusculas} vinculado a tu cuenta y los mensajes que nos "
        "cruzamos, 90 días. Si lo desvinculas en el panel, dejo de escribirte al momento. "
        "Las reseñas son de quien las escribe: solo te las enseño.\n"
        "Política de privacidad: {enlace_privacidad}"
    ),
}

BUTTONS: dict[str, str] = {
    "publish": "Publicar",
    "edit": "Editar",
    "no_reply": "No responder",
    "yes_send": "Sí, enviar",
    "change": "Cambiar",
    "cancel": "Cancelar",
    "no": "No",
    "leave_it": "Dejarlo",
    "open_normal": "Abro normal",
    "closed_day": "Cierro",
    "other_hours": "Otro horario",
    "normal_hours": "Horario normal",
    "closed": "Cerrado",
    "in_panel": "Lo pongo en panel",
    "change_hours": "Cambio horario",
    "nothing_this_year": "Nada este año",
}

CHANNEL_WORDS: dict[str, tuple[str, str]] = {
    # channel: ("{canal}", "{este_chat}")
    "telegram": ("Telegram", "Este chat"),
    "whatsapp": ("WhatsApp", "Este número"),
}


def render(key: str, **values: str) -> str:
    """The text of a catalog key with its variables. A missing variable is a bug and
    raises. An empty trailing variable (a link there is none for) leaves no gap; the
    values themselves are never touched."""
    return TEXTS[key].format(**values).rstrip()


def button_title(option: str) -> str:
    return BUTTONS[option]


def stars_text(stars: int) -> str:
    """`fmt.stars` of the script: no emojis, the stars are written."""
    return "1 estrella" if stars == 1 else f"{stars} estrellas"
