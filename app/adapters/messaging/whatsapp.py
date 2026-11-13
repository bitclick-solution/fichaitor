"""WhatsApp Cloud API: the second channel. One number of Fichaitor, in Bitclick's
WhatsApp Business Account, for every customer (docs/canales.md, "WhatsApp").

Sends a message to `/<version>/<phone number id>/messages` (Graph API), and turns what
the webhook delivers into an `InboundEvent`. The API route checks the signature
(`X-Hub-Signature-256`) with `verify_signature` and answers the verification request
with `verify_subscription`; the conversation process sends.

Inside the customer service window (24 hours since the owner last wrote, reset by every
message or press of theirs) a message goes as it is: text, or text with up to 3 reply
buttons, or a list with more. Outside it, only an approved template can go: the one the
conversation attached to the message (`OutboundMessage.template_fallback`), with the
button ids as the payloads of its quick replies. A message without one is refused.

Limits (Meta, "Reply buttons", read on 2026-11-13): body of 1,024 characters, up to 3
buttons, titles of 20 characters (unique), ids of 256 characters. Texts are cut to them
here; the conversation already builds them within (channel_messages.CORE_LIMITS).

The access token goes in the Authorization header, never in a URL; no error raised here
carries a URL, a body, a phone number nor the text of an httpx exception: only what was
being done, the HTTP status and Meta's error code.

WhatsApp cannot edit a sent message nor show a notice for a press: `clear_buttons` does
nothing and `shows_notices` is False (the conversation sends the notice as a message).
"""

import hashlib
import hmac
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from app.domain import link_codes
from app.domain.channel_messages import (
    WHATSAPP_LIMITS,
    InboundEvent,
    InboundKind,
    OutboundMessage,
)
from app.domain.conversation import InvalidAddress, normalize_address
from app.domain.whatsapp_templates import LANGUAGE
from app.ports.messaging import MessagingRejected, MessagingUnavailable, SentMessage

log = logging.getLogger("fichaitor.whatsapp")

CHANNEL = "whatsapp"
API_BASE = "https://graph.facebook.com"
# Graph API v27.0, introduced on 2026-11-04 (Graph API changelog, read on 2026-11-13).
API_VERSION = "v27.0"
SIGNATURE_HEADER = "X-Hub-Signature-256"
# The window lasts 24 hours; a message is sent as a session message only with this
# margin left, so that it never reaches Meta a moment after the window closed.
WINDOW = timedelta(hours=24)
WINDOW_MARGIN = timedelta(minutes=10)
MAX_TEXT = 4096  # a text message (no buttons)
MAX_BODY = WHATSAPP_LIMITS.max_body
MAX_BUTTONS = WHATSAPP_LIMITS.max_buttons
MAX_TITLE = WHATSAPP_LIMITS.max_button_title
MAX_BUTTON_ID = WHATSAPP_LIMITS.max_button_id_bytes
MAX_LIST_ROWS = 10
MAX_ROW_TITLE = 24
LIST_BUTTON = "Ver opciones"
# What the link of the panel writes in the chat: the token of the link code (never the
# six digits), so that it works even while typed codes are paused (B3).
LINK_PREFIX = "Vincular Fichaitor"

# Meta error codes worth trying again later: rate limits and temporary failures.
# 130429 throughput, 131056 pair rate limit, 131000 something went wrong, 131016 service
# unavailable, 133004 server temporarily unavailable, 1/2 API unknown/service, 4/17/80007
# rate limits of the Graph API.
_TEMPORARY_CODES = frozenset({1, 2, 4, 17, 341, 80007, 130429, 131000, 131016, 131056, 133004})
_PHONE_NUMBER_ID = re.compile(r"\d{1,30}")
# The id Meta gives a message ("wamid.HBgL..."): what job_key_fits accepts after "whatsapp:".
_WAMID = re.compile(r"[A-Za-z0-9._=+/\-]{1,190}")
_CHALLENGE = re.compile(r"[A-Za-z0-9_\-.]{1,128}")
_LINK = re.compile(rf"(?:{LINK_PREFIX}:?\s+)?([A-Za-z0-9_\-]{{32,64}})", re.IGNORECASE)


# --- webhook -------------------------------------------------------------------------


def verify_signature(body: bytes, header: str | None, app_secret: str) -> bool:
    """Whether the request was signed by Meta with our app secret: HMAC-SHA256 of the raw
    body, sent as `sha256=<hex>`. Compared in constant time. Without a secret, nothing is
    accepted."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[len("sha256=") :].encode(), expected.encode())


def verify_subscription(
    mode: str | None, token: str | None, challenge: str | None, verify_token: str
) -> str | None:
    """The answer to Meta's verification request (`hub.mode=subscribe`, `hub.verify_token`,
    `hub.challenge`): the challenge if the token is ours, None otherwise. The challenge is
    echoed only if it is a short plain value."""
    if not verify_token or mode != "subscribe" or not token or not challenge:
        return None
    if not hmac.compare_digest(token.encode(), verify_token.encode()):
        return None
    return challenge if _CHALLENGE.fullmatch(challenge) else None


def extract_messages(payload: object, phone_number_id: str) -> list[dict[str, Any]]:
    """The messages of a webhook delivery addressed to our number, one by one, as the
    job `channel_inbound` carries them: {"phone_number_id", "message", "name"}. Statuses
    and anything for another number of the account are left out. Never raises."""
    found: list[dict[str, Any]] = []
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return found
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict) or change.get("field") != "messages":
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata")
            if not isinstance(metadata, dict) or metadata.get("phone_number_id") != phone_number_id:
                continue
            names = {}
            for contact in value.get("contacts") or []:
                if isinstance(contact, dict) and isinstance(contact.get("wa_id"), str):
                    profile = contact.get("profile")
                    name = profile.get("name") if isinstance(profile, dict) else ""
                    names[contact["wa_id"]] = name if isinstance(name, str) else ""
            for message in value.get("messages") or []:
                if isinstance(message, dict) and parse_message(message) is not None:
                    found.append(
                        {
                            "phone_number_id": phone_number_id,
                            "message": message,
                            "name": names.get(str(message.get("from")), "")[:100],
                        }
                    )
    return found


def statuses(payload: object, phone_number_id: str) -> list[tuple[str, int | None]]:
    """(status, error code) of each status of our number in a delivery: only to count
    them in the log. Never the message id nor the recipient."""
    found: list[tuple[str, int | None]] = []
    if not isinstance(payload, dict):
        return found
    for entry in payload.get("entry") or []:
        for change in (entry.get("changes") or []) if isinstance(entry, dict) else []:
            value = change.get("value") if isinstance(change, dict) else None
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata")
            if not isinstance(metadata, dict) or metadata.get("phone_number_id") != phone_number_id:
                continue
            for status in value.get("statuses") or []:
                if not isinstance(status, dict) or not isinstance(status.get("status"), str):
                    continue
                code = None
                errors = status.get("errors")
                if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                    code = errors[0].get("code") if isinstance(errors[0].get("code"), int) else None
                found.append((status["status"][:20], code))
    return found


def message_id(item: dict[str, Any]) -> str:
    """The wamid of one item of `extract_messages`: the key of its job."""
    return str(item["message"]["id"])


# --- inbound -------------------------------------------------------------------------


def parse_message(
    message: object, name: object = "", phone_number_id: str = ""
) -> InboundEvent | None:
    """A message of the webhook as an internal event, or None for anything the agent
    does not attend: images, audios, reactions, a sender without a phone. Never raises:
    it comes from outside.

    - text: a text, or the link of the panel ("Vincular Fichaitor <token>"), which is
      read as the "start" of Telegram with that token;
    - interactive `button_reply` or `list_reply`, and a template quick reply (`button`):
      a press, whose id is the opaque id we sent."""
    if not isinstance(message, dict):
        return None
    wamid, sender = message.get("id"), message.get("from")
    if not isinstance(wamid, str) or not _WAMID.fullmatch(wamid):
        return None
    if not isinstance(sender, str):
        return None  # a username without a phone number: it cannot be linked
    try:
        address = normalize_address(CHANNEL, "+" + sender)
    except InvalidAddress:
        return None
    sender_name = name if isinstance(name, str) else ""
    context = message.get("context")
    quoted = ""
    if isinstance(context, dict) and isinstance(context.get("id"), str):
        quoted = context["id"][:200]
    kind = message.get("type")
    if kind == "text":
        text_object = message.get("text")
        text = text_object.get("body") if isinstance(text_object, dict) else None
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            return None
        link = _LINK.fullmatch(text.strip())
        if link is not None and link_codes.is_token_shaped(link.group(1)):
            return InboundEvent(
                CHANNEL, address, wamid, InboundKind.START, link.group(1), sender_name=sender_name
            )
        return InboundEvent(
            CHANNEL,
            address,
            wamid,
            InboundKind.TEXT,
            text,
            sender_name=sender_name,
            reply_to=quoted,
        )
    button_id = None
    if kind == "interactive":
        interactive = message.get("interactive")
        if isinstance(interactive, dict):
            reply = interactive.get(interactive.get("type") or "")
            if interactive.get("type") in ("button_reply", "list_reply") and isinstance(
                reply, dict
            ):
                button_id = reply.get("id")
    elif kind == "button":
        button = message.get("button")
        if isinstance(button, dict):
            button_id = button.get("payload")
    if not isinstance(button_id, str) or not button_id or len(button_id) > MAX_BUTTON_ID:
        return None
    return InboundEvent(
        CHANNEL,
        address,
        wamid,
        InboundKind.BUTTON,
        button_id=button_id,
        message_ref=quoted,
        sender_name=sender_name,
    )


def parse_update(item: object, phone_number_id: str) -> InboundEvent | None:
    """One item of `extract_messages` (the payload of the job) as an event, only if it
    is for our number."""
    if not isinstance(item, dict) or item.get("phone_number_id") != phone_number_id:
        return None
    return parse_message(item.get("message"), item.get("name", ""), phone_number_id)


# --- the link ------------------------------------------------------------------------


def link_url(display_number: str, token: str) -> str:
    """`https://wa.me/<number>?text=Vincular%20Fichaitor%20<token>`: opens a chat with our
    number with the token already written. The number goes in full international form,
    digits only."""
    digits = re.sub(r"\D", "", display_number)
    return f"https://wa.me/{digits}?text={quote(f'{LINK_PREFIX} {token}', safe='')}"


# --- the channel ---------------------------------------------------------------------


def window_open(last_inbound_at: datetime | None, now: datetime) -> bool:
    """Whether a session message may go now: the owner wrote less than 24 hours ago (with
    a margin)."""
    return last_inbound_at is not None and now - last_inbound_at < WINDOW - WINDOW_MARGIN


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class WhatsAppChannel:
    name = CHANNEL
    shows_notices = False  # a press has no notice: the conversation writes it
    available = True

    def __init__(
        self,
        phone_number_id: str,
        access_token: str,
        client: httpx.AsyncClient,
        *,
        api_base: str = API_BASE,
        api_version: str = API_VERSION,
        timeout: float = 10.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not access_token:
            raise ValueError("falta el token de acceso")
        if not _PHONE_NUMBER_ID.fullmatch(phone_number_id or ""):
            raise ValueError("identificador del número no válido")
        self._url = f"{api_base}/{api_version}/{phone_number_id}/messages"
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._client = client
        self._timeout = timeout
        self._clock = clock

    def __repr__(self) -> str:  # never the token
        return "WhatsAppChannel()"

    async def _post(self, what: str, payload: dict[str, Any]) -> Any:
        try:
            response = await self._client.post(
                self._url, json=payload, headers=self._headers, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise MessagingUnavailable(
                f"whatsapp {what}: sin respuesta ({type(exc).__name__})"
            ) from None
        try:
            body = response.json()
        except ValueError:
            body = None
        status = response.status_code
        if 200 <= status < 300 and isinstance(body, dict):
            return body
        error = body.get("error") if isinstance(body, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        code = code if isinstance(code, int) else None
        detail = f"{status}" if code is None else f"{status}/{code}"
        if status == 429 or status >= 500 or code in _TEMPORARY_CODES:
            raise MessagingUnavailable(f"whatsapp {what}: no disponible ({detail})")
        raise MessagingRejected(f"whatsapp {what}: rechazado ({detail})")

    def session_payload(self, address: str, message: OutboundMessage) -> dict[str, Any]:
        """The message as it goes inside the window: text, reply buttons or a list."""
        base: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": address,
        }
        buttons = message.buttons
        if not buttons:
            return {
                **base,
                "type": "text",
                "text": {"body": _cut(message.text, MAX_TEXT), "preview_url": False},
            }
        body = {"text": _cut(message.text, MAX_BODY)}
        if len(buttons) <= MAX_BUTTONS:
            action: dict[str, Any] = {
                "buttons": [
                    {"type": "reply", "reply": {"id": b.id, "title": _cut(b.title, MAX_TITLE)}}
                    for b in buttons
                ]
            }
            kind = "button"
        else:
            # More than 3 options: a list (the conversation never needs one today).
            action = {
                "button": LIST_BUTTON,
                "sections": [
                    {
                        "rows": [
                            {"id": b.id, "title": _cut(b.title, MAX_ROW_TITLE)}
                            for b in buttons[:MAX_LIST_ROWS]
                        ]
                    }
                ],
            }
            kind = "list"
        return {
            **base,
            "type": "interactive",
            "interactive": {"type": kind, "body": body, "action": action},
        }

    @staticmethod
    def template_payload(address: str, message: OutboundMessage) -> dict[str, Any]:
        template = message.template_fallback
        assert template is not None
        components: list[dict[str, Any]] = []
        if template.params:
            components.append(
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in template.params],
                }
            )
        for index, button_id in enumerate(template.button_ids):
            components.append(
                {
                    "type": "button",
                    "sub_type": "quick_reply",
                    "index": str(index),
                    "parameters": [{"type": "payload", "payload": button_id}],
                }
            )
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": address,
            "type": "template",
            "template": {
                "name": template.name,
                "language": {"code": LANGUAGE},
                "components": components,
            },
        }

    async def send(self, address: str, message: OutboundMessage) -> SentMessage:
        if window_open(message.last_inbound_at, self._clock()):
            payload, what = self.session_payload(address, message), "mensaje"
        elif message.template_fallback is not None:
            payload, what = self.template_payload(address, message), "plantilla"
        else:
            raise MessagingRejected("whatsapp: ventana de 24 horas cerrada y sin plantilla")
        body = await self._post(what, payload)
        sent = body.get("messages")
        if (
            not isinstance(sent, list)
            or not sent
            or not isinstance(sent[0], dict)
            or not isinstance(sent[0].get("id"), str)
        ):
            raise MessagingUnavailable(f"whatsapp {what}: respuesta sin id")
        return SentMessage(sent[0]["id"][:120])

    async def acknowledge(self, callback_id: str, notice: str = "") -> None:
        return None  # nothing to acknowledge: the notice goes as a message

    async def clear_buttons(self, address: str, channel_message_id: str, text: str) -> None:
        return None  # a sent message cannot be edited
