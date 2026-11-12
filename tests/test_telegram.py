"""Telegram adapter against a simulated HTTP transport. Nothing here calls Telegram."""

import asyncio
import json
import logging

import httpx
import pytest

from app.adapters.messaging import telegram as tg
from app.adapters.messaging.telegram import TelegramChannel, parse_update, verify_webhook_secret
from app.domain import channel_messages as cm
from app.domain.channel_messages import Button, InboundKind, MessageKind, OutboundMessage
from app.ports.messaging import MessagingRejected, MessagingUnavailable

TOKEN = "123456789:AAFakeTokenForTests_abc-DEF"
BUTTON_ID = "prop_0123456789abcdef.a1b2c3.publish"


def run(coro):
    return asyncio.run(coro)


def make(handler):
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return TelegramChannel(TOKEN, client), seen


def ok(result):
    return httpx.Response(200, json={"ok": True, "result": result})


def body(request):
    return json.loads(request.content)


def message(text="Hola", buttons=()):
    return OutboundMessage("chid_1", MessageKind.PROPOSAL, text, tuple(buttons))


# --- sending -----------------------------------------------------------------------


def test_send_message_with_inline_buttons_one_per_row():
    channel, seen = make(lambda r: ok({"message_id": 77}))
    buttons = [
        Button(BUTTON_ID, "Publicar"),
        Button(BUTTON_ID.replace("publish", "edit"), "Editar"),
    ]
    sent = run(channel.send("1001", message("¿La envío?", buttons)))
    assert sent.channel_message_id == "77"
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = body(request)
    assert payload["chat_id"] == 1001 and payload["text"] == "¿La envío?"
    assert "parse_mode" not in payload  # plain text: a review cannot inject markup
    assert payload["link_preview_options"] == {"is_disabled": True}
    assert payload["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "Publicar", "callback_data": BUTTON_ID}],
            [{"text": "Editar", "callback_data": BUTTON_ID.replace("publish", "edit")}],
        ]
    }
    for row in payload["reply_markup"]["inline_keyboard"]:
        assert 1 <= len(row[0]["callback_data"].encode()) <= 64


def test_send_without_buttons_has_no_reply_markup():
    channel, seen = make(lambda r: ok({"message_id": 1}))
    run(channel.send("1001", message("Hecho.")))
    assert "reply_markup" not in body(seen[0])


def test_send_refuses_what_telegram_would_refuse_before_calling():
    channel, seen = make(lambda r: ok({"message_id": 1}))
    with pytest.raises(cm.InvalidMessage):
        run(channel.send("1001", message("x" * 4097)))
    with pytest.raises(cm.InvalidMessage):
        run(channel.send("1001", message("hola", [Button("x" * 65, "A")])))
    assert not seen


def test_answer_callback_query_with_and_without_notice():
    channel, seen = make(lambda r: ok(True))
    run(channel.acknowledge("cb-1"))
    run(channel.acknowledge("cb-2", "n" * 300))
    assert str(seen[0].url).endswith("/answerCallbackQuery")
    assert body(seen[0]) == {"callback_query_id": "cb-1"}
    assert body(seen[1])["text"] == "n" * 200  # 0-200 characters


def test_clear_buttons_edits_the_message_with_an_empty_keyboard():
    channel, seen = make(lambda r: ok({"message_id": 77}))
    run(channel.clear_buttons("1001", "77", "Texto\n\nTu respuesta: Publicar"))
    assert str(seen[0].url).endswith("/editMessageText")
    assert body(seen[0]) == {
        "chat_id": 1001,
        "message_id": 77,
        "text": "Texto\n\nTu respuesta: Publicar",
        "link_preview_options": {"is_disabled": True},
        "reply_markup": {"inline_keyboard": []},
    }


def test_acknowledge_and_clear_never_raise():
    channel, _ = make(lambda r: httpx.Response(400, json={"ok": False, "error_code": 400}))
    run(channel.acknowledge("old"))
    run(channel.clear_buttons("1001", "77", "x"))


# --- errors and the token ----------------------------------------------------------


def failing(exc):
    def handler(request):
        raise exc

    return handler


@pytest.mark.parametrize(
    ("handler", "error", "retry_after"),
    [
        (
            lambda r: httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": f"Too Many Requests https://api.telegram.org/bot{TOKEN}/x",
                    "parameters": {"retry_after": 7},
                },
            ),
            MessagingUnavailable,
            7.0,
        ),
        (lambda r: httpx.Response(502, text="Bad Gateway"), MessagingUnavailable, None),
        (
            lambda r: httpx.Response(
                403, json={"ok": False, "error_code": 403, "description": "bot was blocked"}
            ),
            MessagingRejected,
            None,
        ),
        (
            lambda r: httpx.Response(404, json={"ok": False, "error_code": 404}),
            MessagingRejected,
            None,
        ),
        (lambda r: httpx.Response(200, text="not json"), MessagingUnavailable, None),
        (lambda r: ok({"sin": "message_id"}), MessagingUnavailable, None),
        (
            failing(httpx.ConnectError(f"cannot reach https://api.telegram.org/bot{TOKEN}/")),
            MessagingUnavailable,
            None,
        ),
        (failing(httpx.ReadTimeout("timeout")), MessagingUnavailable, None),
    ],
)
def test_errors_are_classified_and_never_carry_the_token(handler, error, retry_after):
    channel, _ = make(handler)
    with pytest.raises(error) as caught:
        run(channel.send("1001", message()))
    exc = caught.value
    assert getattr(exc, "retry_after", None) == retry_after
    rendered = f"{exc} {exc!r} {exc.args}"
    assert TOKEN not in rendered and "AAFake" not in rendered and "api.telegram.org" not in rendered
    assert "blocked" not in rendered and "Too Many" not in rendered  # nothing the provider said
    # The original httpx exception (which names the URL) is not chained.
    assert exc.__cause__ is None and (exc.__context__ is None or exc.__suppress_context__)


def test_the_token_is_not_in_the_repr_of_the_channel():
    channel, _ = make(lambda r: ok(True))
    assert TOKEN not in repr(channel) and TOKEN not in str(channel.__dict__.get("_client"))
    with pytest.raises(ValueError):
        TelegramChannel("", httpx.AsyncClient())


def test_nothing_logged_contains_the_token(caplog):
    tg.redact_bot_token_in_logs()
    tg.redact_bot_token_in_logs()  # twice: one filter
    assert sum(isinstance(f, tg._TokenRedactor) for f in logging.getLogger("httpx").filters) == 1
    channel, _ = make(lambda r: httpx.Response(500, text="boom"))
    # The worker silences httpx below WARNING; here it is made to talk on purpose.
    with caplog.at_level(logging.DEBUG), caplog.at_level(logging.DEBUG, logger="httpx"):
        run(channel.acknowledge("cb"))
        run(channel.clear_buttons("1001", "77", "x"))
        with pytest.raises(MessagingUnavailable):
            run(channel.send("1001", message()))
        ok_channel, _ = make(lambda r: ok({"message_id": 1}))
        run(ok_channel.send("1001", message()))
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "httpx" in {record.name for record in caplog.records}  # httpx did log the request
    assert "/bot<oculto>/sendMessage" in logged
    assert TOKEN not in logged and "AAFake" not in logged
    assert TOKEN not in caplog.text


def test_redact():
    assert tg.redact(f"POST https://api.telegram.org/bot{TOKEN}/getUpdates") == (
        "POST https://api.telegram.org/bot<oculto>/getUpdates"
    )


# --- webhook secret ----------------------------------------------------------------


def test_webhook_without_the_right_secret_is_rejected():
    assert verify_webhook_secret("s3cret_-OK", "s3cret_-OK")
    assert not verify_webhook_secret("otro", "s3cret_-OK")
    assert not verify_webhook_secret("", "s3cret_-OK")
    assert not verify_webhook_secret(None, "s3cret_-OK")
    assert not verify_webhook_secret("s3cret_-OK ", "s3cret_-OK")
    # No secret configured: nothing is accepted, not even an empty header.
    assert not verify_webhook_secret("", "")
    assert not verify_webhook_secret("cualquiera", "")
    assert tg.SECRET_HEADER == "X-Telegram-Bot-Api-Secret-Token"


# --- updates -----------------------------------------------------------------------


def text_update(text, update_id=10, chat_type="private", user=1001, chat=1001, **sender):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 5,
            "from": {"id": user, "is_bot": False, "first_name": "Marta", **sender},
            "chat": {"id": chat, "type": chat_type},
            "date": 1794477600,
            "text": text,
        },
    }


def callback_update(data=BUTTON_ID, update_id=11, user=1001, chat=1001, chat_type="private"):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "4382bfdwdsb323b2d9",
            "from": {"id": user, "is_bot": False, "first_name": "Marta"},
            "message": {"message_id": 77, "chat": {"id": chat, "type": chat_type}},
            "data": data,
        },
    }


def test_text_message_becomes_a_text_event():
    event = parse_update(text_update("de 10 a 14"))
    assert event == cm.InboundEvent(
        "telegram", "1001", "u10", InboundKind.TEXT, "de 10 a 14", sender_name="Marta"
    )


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("/start 123456", "123456"),
        ("/start", ""),
        ("/start@FichaitorBot 654321", "654321"),
        ("/start   123456  ", "123456"),
        ("/start <script>", ""),  # not a deep link payload: no code
        ("/start " + "9" * 65, ""),
    ],
)
def test_start_command_with_and_without_code(text, code):
    event = parse_update(text_update(text))
    assert event.kind is InboundKind.START and event.text == code


def test_text_that_only_looks_like_start_is_text():
    assert parse_update(text_update("/startup 123456")).kind is InboundKind.TEXT
    assert parse_update(text_update("hola /start 123456")).kind is InboundKind.TEXT


def test_callback_query_becomes_a_button_event():
    event = parse_update(callback_update())
    assert event == cm.InboundEvent(
        "telegram",
        "1001",
        "u11",
        InboundKind.BUTTON,
        button_id=BUTTON_ID,
        callback_id="4382bfdwdsb323b2d9",
        message_ref="77",
    )


@pytest.mark.parametrize(
    "update",
    [
        None,
        "texto",
        {},
        {"update_id": "10", "message": {}},
        {"update_id": 10},
        {"update_id": 10, "edited_message": {"text": "x"}},
        text_update("hola", chat_type="group", chat=-100),
        text_update("hola", chat_type="supergroup", chat=-100),
        text_update("hola", user=2002),  # sender is not the chat
        text_update("hola", is_bot=True),
        text_update(""),
        text_update("x" * 4097),
        text_update(None),
        {"update_id": 10, "message": {"from": {"id": 1}, "chat": {"id": 1, "type": "private"}}},
        callback_update(chat_type="group", chat=-100),  # a button pressed in a group
        callback_update(user=2002),  # in somebody else's chat
        callback_update(data=None),
        {"update_id": 11, "callback_query": {"id": 5, "from": {"id": 1}, "data": "x"}},
    ],
)
def test_updates_the_agent_does_not_attend(update):
    assert parse_update(update) is None


def test_parse_update_never_raises_on_garbage():
    for garbage in (
        [],
        3,
        {"update_id": 1, "message": "x"},
        {"update_id": 1, "callback_query": []},
    ):
        assert parse_update(garbage) is None


# --- receiving ---------------------------------------------------------------------


def test_get_updates_long_polling_parameters():
    channel, seen = make(lambda r: ok([text_update("hola", 41), "basura"]))
    updates = run(channel.get_updates(40, wait_seconds=25))
    assert [u["update_id"] for u in updates] == [41]
    assert str(seen[0].url).endswith("/getUpdates")
    assert body(seen[0]) == {
        "timeout": 25,
        "allowed_updates": ["message", "callback_query"],
        "offset": 40,
    }
    run(channel.get_updates(None))
    assert "offset" not in body(seen[1])


def test_set_and_delete_webhook():
    channel, seen = make(lambda r: ok(True))
    run(channel.set_webhook("https://fichaitor.es/api/v1/webhooks/telegram", "s3cret"))
    run(channel.delete_webhook())
    assert body(seen[0]) == {
        "url": "https://fichaitor.es/api/v1/webhooks/telegram",
        "secret_token": "s3cret",
        "allowed_updates": ["message", "callback_query"],
    }
    assert str(seen[1].url).endswith("/deleteWebhook")


def test_poll_handles_each_update_once_and_confirms_with_the_offset():
    batches = [
        [text_update("uno", 100), callback_update(update_id=101), {"update_id": 102, "poll": {}}],
        [text_update("dos", 103)],
    ]
    offsets = []
    stop = asyncio.Event()

    def handler(request):
        offsets.append(body(request).get("offset"))
        if batches:
            return ok(batches.pop(0))
        stop.set()
        return ok([])

    channel, _ = make(handler)
    handled = []

    async def handle(event):
        handled.append(event.event_id)
        if event.event_id == "u101":
            raise RuntimeError("un fallo no para el sondeo")

    run(tg.poll(channel, handle, stop=stop, wait_seconds=0))
    assert handled == ["u100", "u101", "u103"]
    assert offsets == [None, 103, 104]  # unknown update types are confirmed too


def test_poll_keeps_going_after_an_error():
    calls = []
    stop = asyncio.Event()

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(502)
        stop.set()
        return ok([])

    channel, _ = make(handler)
    run(tg.poll(channel, lambda e: None, stop=stop, wait_seconds=0, retry_seconds=0))
    assert len(calls) == 2
