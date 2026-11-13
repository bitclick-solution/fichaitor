"""Telegram Bot API: the first channel.

Sends messages with inline buttons, answers button presses, edits a message to
remove its buttons, and turns an update into an `InboundEvent`. Two ways of
receiving: a webhook (the API route checks `X-Telegram-Bot-Api-Secret-Token` with
`verify_webhook_secret`) and long polling with `getUpdates` for local development.

The bot token travels in the path of every request (`/bot<token>/<method>`), so:
- no error raised here carries a URL, a response body or the text of an httpx
  exception: only the method and a status code;
- `redact_bot_token_in_logs()` rewrites what httpx itself logs.

Limits (Bot API 10.4, https://core.telegram.org/bots/api, read on 2026-11-12):
text of 1-4096 characters; `callback_data` of 1-64 bytes; notice of
`answerCallbackQuery` of 0-200 characters; `getUpdates` does not work while a
webhook is set. `docs/canales.md` quotes them.

With several businesses, several proposals may go to the same chat at once, and
Telegram takes about one message per second per chat: `send` keeps one second between
two messages to the same chat, and waits once for a short `retry_after` of a 429
(docs/multinegocio.md, 7).
"""

import asyncio
import hmac
import logging
import re
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.domain.channel_messages import (
    COMMANDS,
    TELEGRAM_LIMITS,
    InboundEvent,
    InboundKind,
    OutboundMessage,
    validate,
)
from app.ports.messaging import (
    MessagingRejected,
    MessagingUnavailable,
    SentMessage,
)

log = logging.getLogger("fichaitor.telegram")

CHANNEL = "telegram"
API_BASE = "https://api.telegram.org"
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
MAX_TEXT = TELEGRAM_LIMITS.max_body
MAX_NOTICE = 200
MAX_START_PAYLOAD = 64  # deep link parameter: up to 64 of A-Z, a-z, 0-9, _ and -
ALLOWED_UPDATES = ["message", "callback_query"]
# One message per second and chat (docs/canales.md, "Límites verificados").
SEND_INTERVAL_SECONDS = 1.0
# A 429 that asks to wait this long or less is waited for once; a longer one is left to
# the next round of the conversation (the proposal is sent again later).
MAX_RETRY_AFTER_SECONDS = 5.0

_BOT_PATH = re.compile(r"/bot\d+:[A-Za-z0-9_\-]+")
_START = re.compile(r"/start(?:@\w+)?(?:\s+(\S+))?\s*")
_START_PAYLOAD = re.compile(r"[A-Za-z0-9_\-]{1,64}")
# A command: "/name" or "/name@bot", alone or followed by words that are ignored.
_COMMAND = re.compile(r"/([A-Za-z0-9_]{1,32})(?:@\w+)?(?:\s.*)?", re.DOTALL)


# --- the token never reaches a log ---------------------------------------------------


def redact(text: str) -> str:
    return _BOT_PATH.sub("/bot<oculto>", text)


class _TokenRedactor(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact(str(arg)) if "/bot" in str(arg) else arg for arg in record.args
            )
        return True


def redact_bot_token_in_logs() -> None:
    """httpx logs every request with its URL at INFO level. Call once at start-up."""
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, _TokenRedactor) for f in logger.filters):
            logger.addFilter(_TokenRedactor())


# --- webhook -------------------------------------------------------------------------


def verify_webhook_secret(header: str | None, expected: str) -> bool:
    """Whether a webhook request carries our secret. Without a configured secret,
    nothing is accepted."""
    if not expected or not header:
        return False
    return hmac.compare_digest(header.encode(), expected.encode())


# --- updates -------------------------------------------------------------------------


def _private_chat(chat: object) -> str | None:
    if isinstance(chat, dict) and chat.get("type") == "private" and isinstance(chat.get("id"), int):
        return str(chat["id"])
    return None


def _quoted_message(message: dict) -> str:
    """The id of the message this one answers by quoting it, if any: it chooses the
    proposal among several open ones (docs/multinegocio.md, 6.3)."""
    quoted = message.get("reply_to_message")
    if isinstance(quoted, dict) and isinstance(quoted.get("message_id"), int):
        return str(quoted["message_id"])
    return ""


def parse_update(update: object) -> InboundEvent | None:
    """An update of Telegram as an internal event, or None for anything the agent
    does not attend: groups and channels, bots, messages without text, other update
    types. Never raises: the update comes from outside."""
    if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
        return None
    event_id = f"u{update['update_id']}"
    message, callback = update.get("message"), update.get("callback_query")
    if isinstance(message, dict):
        sender = message.get("from")
        address = _private_chat(message.get("chat"))
        text = message.get("text")
        if address is None or not isinstance(sender, dict) or sender.get("is_bot"):
            return None
        if str(sender.get("id")) != address or not isinstance(text, str):
            return None
        if not text.strip() or len(text) > MAX_TEXT:
            return None
        name = sender.get("first_name") if isinstance(sender.get("first_name"), str) else ""
        start = _START.fullmatch(text.strip())
        if start is not None:
            payload = start.group(1) or ""
            code = payload if _START_PAYLOAD.fullmatch(payload) else ""
            return InboundEvent(
                CHANNEL, address, event_id, InboundKind.START, code, sender_name=name
            )
        command = _COMMAND.fullmatch(text.strip())
        if command is not None and command.group(1).lower() in COMMANDS:
            # Only /ayuda and /privacidad besides /start. Anything else that looks like
            # a command is plain text, and is understood as any other text.
            return InboundEvent(
                CHANNEL,
                address,
                event_id,
                InboundKind.COMMAND,
                command.group(1).lower(),
                sender_name=name,
            )
        return InboundEvent(
            CHANNEL,
            address,
            event_id,
            InboundKind.TEXT,
            text,
            sender_name=name,
            reply_to=_quoted_message(message),
        )
    if isinstance(callback, dict):
        sender = callback.get("from")
        data, callback_id = callback.get("data"), callback.get("id")
        if not isinstance(sender, dict) or not isinstance(sender.get("id"), int):
            return None
        if not isinstance(data, str) or not isinstance(callback_id, str):
            return None
        origin = callback.get("message")
        address = str(sender["id"])
        message_ref = ""
        if isinstance(origin, dict):
            # The button must have been pressed in the private chat with that user.
            if _private_chat(origin.get("chat")) != address:
                return None
            if isinstance(origin.get("message_id"), int):
                message_ref = str(origin["message_id"])
        return InboundEvent(
            CHANNEL,
            address,
            event_id,
            InboundKind.BUTTON,
            button_id=data,
            callback_id=callback_id,
            message_ref=message_ref,
        )
    return None


# --- the channel ---------------------------------------------------------------------


class TelegramChannel:
    name = CHANNEL

    def __init__(
        self,
        token: str,
        client: httpx.AsyncClient,
        *,
        api_base: str = API_BASE,
        timeout: float = 10.0,
        send_interval: float = SEND_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not token:
            raise ValueError("falta el token del bot")
        self._path = f"{api_base}/bot{token}/"
        self._client = client
        self._timeout = timeout
        self._send_interval = send_interval
        self._monotonic = clock
        self._sleep = sleep
        # Per chat: when its last message went out, and a lock so that two messages to
        # the same chat leave one after the other.
        self._last_sent: dict[str, float] = {}
        self._chat_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def __repr__(self) -> str:  # never the token
        return "TelegramChannel()"

    async def _call(
        self, method: str, payload: dict[str, Any], timeout: float | None = None
    ) -> Any:
        """One Bot API call. Raises MessagingUnavailable or MessagingRejected with a
        fixed text: the URL holds the token and must not leave this function."""
        try:
            response = await self._client.post(
                self._path + method, json=payload, timeout=timeout or self._timeout
            )
        except httpx.HTTPError as exc:
            # `from None`: the original exception names the URL.
            raise MessagingUnavailable(
                f"telegram {method}: sin respuesta ({type(exc).__name__})"
            ) from None
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code == 200 and isinstance(body, dict) and body.get("ok") is True:
            return body.get("result")
        status = response.status_code
        if isinstance(body, dict) and isinstance(body.get("error_code"), int):
            status = body["error_code"]
        if status == 429 or status >= 500 or status == 200:
            retry_after = None
            parameters = body.get("parameters") if isinstance(body, dict) else None
            if isinstance(parameters, dict) and isinstance(parameters.get("retry_after"), int):
                retry_after = float(parameters["retry_after"])
            raise MessagingUnavailable(f"telegram {method}: no disponible ({status})", retry_after)
        raise MessagingRejected(f"telegram {method}: rechazado ({status})")

    async def send(self, address: str, message: OutboundMessage) -> SentMessage:
        validate(message, TELEGRAM_LIMITS)
        payload: dict[str, Any] = {
            "chat_id": int(address),
            "text": message.text,  # plain text: no parse_mode, nothing is markup
            "link_preview_options": {"is_disabled": True},
        }
        if message.buttons:
            # One button per row, same titles as on WhatsApp. callback_data is the
            # opaque id: proposal, step and option, nothing else.
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": button.title, "callback_data": button.id}]
                    for button in message.buttons
                ]
            }
        result = await self._spaced_send(address, payload)
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
            raise MessagingUnavailable("telegram sendMessage: respuesta sin message_id")
        return SentMessage(str(result["message_id"]))

    async def _spaced_send(self, address: str, payload: dict[str, Any]) -> Any:
        """sendMessage, at least `send_interval` after the previous one to that chat. A
        429 with a short `retry_after` is waited for and tried once more."""
        async with self._chat_locks[address]:
            last = self._last_sent.get(address)
            if last is not None:
                wait = last + self._send_interval - self._monotonic()
                if wait > 0:
                    await self._sleep(wait)
            try:
                try:
                    return await self._call("sendMessage", payload)
                except MessagingUnavailable as exc:
                    retry = exc.retry_after
                    if retry is None or not 0 < retry <= MAX_RETRY_AFTER_SECONDS:
                        raise
                    await self._sleep(retry)
                    return await self._call("sendMessage", payload)
            finally:
                self._last_sent[address] = self._monotonic()

    async def acknowledge(self, callback_id: str, notice: str = "") -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if notice:
            payload["text"] = notice[:MAX_NOTICE]
        try:
            await self._call("answerCallbackQuery", payload)
        except (MessagingUnavailable, MessagingRejected) as exc:
            log.info("pulsación sin acuse: %s", exc)  # too old to answer: harmless

    async def clear_buttons(self, address: str, channel_message_id: str, text: str) -> None:
        payload = {
            "chat_id": int(address),
            "message_id": int(channel_message_id),
            "text": text[:MAX_TEXT],
            "link_preview_options": {"is_disabled": True},
            "reply_markup": {"inline_keyboard": []},
        }
        try:
            await self._call("editMessageText", payload)
        except (MessagingUnavailable, MessagingRejected) as exc:
            log.info("mensaje sin editar: %s", exc)

    # --- receiving ---------------------------------------------------------------

    async def set_webhook(self, url: str, secret: str) -> None:
        """Production: Telegram posts every update to `url` with the secret header."""
        await self._call(
            "setWebhook",
            {"url": url, "secret_token": secret, "allowed_updates": ALLOWED_UPDATES},
        )

    async def delete_webhook(self) -> None:
        """Needed before long polling: getUpdates does not work with a webhook set."""
        await self._call("deleteWebhook", {})

    async def get_updates(self, offset: int | None, wait_seconds: int = 25) -> list[dict]:
        payload: dict[str, Any] = {"timeout": wait_seconds, "allowed_updates": ALLOWED_UPDATES}
        if offset is not None:
            payload["offset"] = offset
        result = await self._call("getUpdates", payload, timeout=wait_seconds + 10)
        return [u for u in result if isinstance(u, dict)] if isinstance(result, list) else []


async def poll(
    channel: TelegramChannel,
    handle: Callable[[InboundEvent], Awaitable[None]],
    *,
    stop: asyncio.Event | None = None,
    wait_seconds: int = 25,
    retry_seconds: float = 5.0,
    on_idle: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Long polling for local development: ask for updates, handle each, confirm
    them with the next offset. Runs until `stop` is set."""
    offset: int | None = None
    while stop is None or not stop.is_set():
        try:
            updates = await channel.get_updates(offset, wait_seconds)
        except (MessagingUnavailable, MessagingRejected) as exc:
            log.warning("sondeo de Telegram: %s", exc)
            await asyncio.sleep(retry_seconds)
            continue
        for update in updates:
            if isinstance(update.get("update_id"), int):
                offset = max(offset or 0, update["update_id"] + 1)
            event = parse_update(update)
            if event is None:
                continue
            try:
                await handle(event)
            except Exception as exc:  # one bad update does not stop the loop
                log.error("actualización de Telegram sin procesar: %s", type(exc).__name__)
        if on_idle is not None:
            await on_idle()


# --- local trial ---------------------------------------------------------------------


async def _run_local() -> None:
    """`uv run python -m app.adapters.messaging.telegram`: the real bot with long
    polling, everything else in memory. Prints a link code and, once the chat is
    linked, sends a sample review and a sample holiday. Nothing reaches Google."""
    from dataclasses import replace
    from datetime import UTC, datetime, time, timedelta

    from app.adapters.conversation_store.memory import MemoryConversationStore, MemoryLocation
    from app.application.conversation_service import (
        ActionOutcome,
        ConversationService,
        KeyDateInput,
        ReviewInput,
    )
    from app.channels_config import ChannelsSettings
    from app.domain.conversation import QuietHours
    from app.ports.proposal_actions import InMemoryProposalActions

    settings = ChannelsSettings()
    if not settings.telegram_enabled or not settings.telegram_polling:
        raise SystemExit(
            "Define FICHAITOR_TELEGRAM_BOT_TOKEN y FICHAITOR_TELEGRAM_POLLING=true para la prueba."
        )
    logging.basicConfig(level=logging.INFO)
    redact_bot_token_in_logs()
    account = "cuenta-de-prueba"
    store = MemoryConversationStore()
    store.locations["loc_prueba"] = MemoryLocation(account, "Bar de prueba")
    actions = InMemoryProposalActions()
    async with httpx.AsyncClient() as client:
        channel = TelegramChannel(settings.telegram_bot_token.get_secret_value(), client)
        # No quiet hours in the trial, so that it answers at any time.
        config = replace(settings.service_config(), quiet=QuietHours(time(0), time(0)))
        service = ConversationService(
            store, {CHANNEL: channel}, actions, config, clock=lambda: datetime.now(UTC)
        )
        await channel.delete_webhook()
        code = await service.create_link_code(account)
        print(f"Abre {settings.telegram_link(code)} (el código vale 10 minutos).")
        state = {"seeded": False, "reported": 0}

        async def on_idle() -> None:
            if not state["seeded"] and await store.list_identities(account):
                state["seeded"] = True
                await service.submit_review(
                    account,
                    ReviewInput(
                        "rev_0000000000000001",
                        5,
                        "El arroz, espectacular.",
                        "Muchas gracias por tu reseña. Nos alegra que disfrutaras el arroz.",
                        "loc_prueba",
                    ),
                )
                day = (datetime.now(UTC) + timedelta(days=4)).date()
                await service.submit_key_date(
                    account,
                    KeyDateInput(
                        f"holiday:{day}", "holiday", day, "festivo de prueba", "loc_prueba"
                    ),
                )
            for request in actions.requests[state["reported"] :]:
                state["reported"] += 1
                print(f"Acción confirmada: {request.type} {dict(request.payload)}")
                await service.handle_action_result(request.proposal_id, ActionOutcome(ok=True))
            await service.tick()

        await poll(channel, service.handle_inbound, on_idle=on_idle, wait_seconds=5)


if __name__ == "__main__":
    asyncio.run(_run_local())
