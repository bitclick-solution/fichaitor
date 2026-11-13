"""The WhatsApp adapter (app/adapters/messaging/whatsapp.py), without network: every call
to Meta goes to an httpx.MockTransport. Nothing here reaches graph.facebook.com."""

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.adapters.messaging import whatsapp as wa
from app.domain.channel_messages import (
    Button,
    InboundKind,
    MessageKind,
    OutboundMessage,
    TemplateFallback,
)
from app.ports.messaging import MessagingRejected, MessagingUnavailable

PHONE_ID = "106540352242922"
TOKEN = "EAAG-token-de-pruebas-que-nunca-sale"
NOW = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)
OWNER = "+34600111222"
WAMID = "wamid.HBgLMzQ2MDAxMTEyMjIVAgASGBQzQUM5QTFFNjk0MDQ0N0E4RTQ0MAA="
APP_SECRET = "secreto-de-la-app-de-pruebas"
TOKEN_LINK = "a" * 43


def run(coro):
    return asyncio.run(coro)


class Meta:
    """A fake Graph API: records each request and answers what it is told."""

    def __init__(self, status: int = 200, body: object = None) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status
        self.body = (
            body
            if body is not None
            else {
                "messaging_product": "whatsapp",
                "contacts": [{"input": OWNER, "wa_id": "34600111222"}],
                "messages": [{"id": WAMID}],
            }
        )
        self.error: Exception | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, json=self.body)

    @property
    def last(self) -> dict:
        return json.loads(self.requests[-1].content)


def channel(meta: Meta) -> wa.WhatsAppChannel:
    client = httpx.AsyncClient(transport=httpx.MockTransport(meta.handler))
    return wa.WhatsAppChannel(PHONE_ID, TOKEN, client, clock=lambda: NOW)


def message(text="Hola", buttons=(), fallback=None, wrote=timedelta(hours=1)):
    return OutboundMessage(
        "chid_0000000000000001",
        MessageKind.PROPOSAL,
        text,
        tuple(buttons),
        template_fallback=fallback,
        last_inbound_at=None if wrote is None else NOW - wrote,
    )


BUTTONS = (
    Button("prop_0123456789abcdef.a1b2c3.publish", "Publicar"),
    Button("prop_0123456789abcdef.a1b2c3.edit", "Editar"),
    Button("prop_0123456789abcdef.a1b2c3.no_reply", "No responder"),
)
FALLBACK = TemplateFallback(
    "resena_nueva_v1", ("Bar", "5 estrellas", "Muy bien", "Gracias"), tuple(b.id for b in BUTTONS)
)


# --- sending -------------------------------------------------------------------------


def test_a_text_inside_the_window_goes_to_the_messages_endpoint_with_the_token_in_a_header():
    meta = Meta()
    sent = run(channel(meta).send(OWNER, message("Hola")))
    request = meta.requests[-1]
    assert str(request.url) == f"https://graph.facebook.com/v27.0/{PHONE_ID}/messages"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(request.url)
    assert meta.last == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": OWNER,
        "type": "text",
        "text": {"body": "Hola", "preview_url": False},
    }
    assert sent.channel_message_id == WAMID


def test_buttons_inside_the_window_are_reply_buttons():
    meta = Meta()
    run(channel(meta).send(OWNER, message("¿La envío?", BUTTONS, FALLBACK)))
    body = meta.last
    assert body["type"] == "interactive"
    assert body["interactive"]["type"] == "button"
    assert body["interactive"]["body"] == {"text": "¿La envío?"}
    assert body["interactive"]["action"]["buttons"] == [
        {"type": "reply", "reply": {"id": b.id, "title": b.title}} for b in BUTTONS
    ]


def test_texts_are_cut_to_the_limits_of_whatsapp():
    meta = Meta()
    long_title = Button("prop_0123456789abcdef.a1b2c3.edit", "T" * 30)
    run(channel(meta).send(OWNER, message("x" * 2000, (long_title,))))
    interactive = meta.last["interactive"]
    assert len(interactive["body"]["text"]) == 1024
    assert len(interactive["action"]["buttons"][0]["reply"]["title"]) == 20
    run(channel(meta).send(OWNER, message("y" * 5000)))
    assert len(meta.last["text"]["body"]) == 4096


def test_more_than_three_options_go_as_a_list():
    meta = Meta()
    options = tuple(
        Button(f"prop_0123456789abcdef.a1b2c3.opt_{i}", f"Opción {i}") for i in range(5)
    )
    run(channel(meta).send(OWNER, message("Elige", options)))
    interactive = meta.last["interactive"]
    assert interactive["type"] == "list"
    rows = interactive["action"]["sections"][0]["rows"]
    assert [r["id"] for r in rows] == [b.id for b in options]


def test_outside_the_window_the_template_goes_with_the_button_ids_as_payloads():
    meta = Meta()
    run(
        channel(meta).send(
            OWNER, message("¿La envío?", BUTTONS, FALLBACK, wrote=timedelta(hours=30))
        )
    )
    body = meta.last
    assert body["type"] == "template"
    template = body["template"]
    assert template["name"] == "resena_nueva_v1"
    assert template["language"] == {"code": "es"}
    body_component, *buttons = template["components"]
    assert body_component == {
        "type": "body",
        "parameters": [{"type": "text", "text": p} for p in FALLBACK.params],
    }
    assert buttons == [
        {
            "type": "button",
            "sub_type": "quick_reply",
            "index": str(i),
            "parameters": [{"type": "payload", "payload": b.id}],
        }
        for i, b in enumerate(BUTTONS)
    ]


@pytest.mark.parametrize("wrote", [None, timedelta(hours=24), timedelta(hours=23, minutes=55)])
def test_a_message_without_template_is_refused_once_the_window_is_closed(wrote):
    meta = Meta()
    with pytest.raises(MessagingRejected):
        run(channel(meta).send(OWNER, message("Hola", wrote=wrote)))
    assert meta.requests == []


def test_the_window_keeps_a_margin_before_the_24_hours():
    assert wa.window_open(NOW - timedelta(hours=23, minutes=49), NOW)
    assert not wa.window_open(NOW - timedelta(hours=23, minutes=51), NOW)
    assert not wa.window_open(None, NOW)


@pytest.mark.parametrize(
    ("status", "code"),
    [(429, None), (500, None), (503, 2), (400, 130429), (400, 131056), (400, 131000), (400, 4)],
)
def test_rate_limits_and_server_errors_are_temporary(status, code):
    body = {"error": {"message": f"algo con {TOKEN}", "code": code}} if code else {}
    meta = Meta(status, body)
    with pytest.raises(MessagingUnavailable) as caught:
        run(channel(meta).send(OWNER, message()))
    assert TOKEN not in str(caught.value) and OWNER not in str(caught.value)


@pytest.mark.parametrize(("status", "code"), [(400, 131047), (401, 190), (400, 100), (400, 132001)])
def test_other_errors_are_permanent_and_say_only_the_status_and_code(status, code):
    meta = Meta(status, {"error": {"message": f"token {TOKEN} número {OWNER}", "code": code}})
    with pytest.raises(MessagingRejected) as caught:
        run(channel(meta).send(OWNER, message()))
    text = str(caught.value)
    assert str(code) in text and TOKEN not in text and OWNER not in text


def test_a_network_failure_is_temporary_and_names_neither_the_url_nor_the_token():
    meta = Meta()
    meta.error = httpx.ConnectError(f"https://graph.facebook.com/?access_token={TOKEN}")
    with pytest.raises(MessagingUnavailable) as caught:
        run(channel(meta).send(OWNER, message()))
    assert TOKEN not in str(caught.value) and caught.value.__cause__ is None


def test_an_answer_without_message_id_is_temporary():
    with pytest.raises(MessagingUnavailable):
        run(channel(Meta(200, {"messages": []})).send(OWNER, message()))


def test_the_token_is_never_in_the_repr_nor_in_the_logs(caplog):
    meta = Meta()
    ch = channel(meta)
    with caplog.at_level(logging.DEBUG):
        run(ch.send(OWNER, message()))
    assert TOKEN not in repr(ch)
    assert TOKEN not in caplog.text


def test_it_cannot_edit_nor_show_notices():
    ch = channel(Meta())
    assert ch.shows_notices is False
    assert run(ch.clear_buttons(OWNER, WAMID, "x")) is None
    assert run(ch.acknowledge("x", "aviso")) is None


def test_it_refuses_to_start_without_token_or_with_a_bad_number_id():
    client = httpx.AsyncClient()
    with pytest.raises(ValueError):
        wa.WhatsAppChannel(PHONE_ID, "", client)
    with pytest.raises(ValueError):
        wa.WhatsAppChannel("../me", TOKEN, client)


# --- webhook -------------------------------------------------------------------------


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_the_signature_is_the_hmac_of_the_raw_body_with_the_app_secret():
    body = b'{"object":"whatsapp_business_account"}'
    assert wa.verify_signature(body, sign(body), APP_SECRET)
    assert not wa.verify_signature(body + b" ", sign(body), APP_SECRET)
    assert not wa.verify_signature(body, sign(body, "otro"), APP_SECRET)
    assert not wa.verify_signature(body, sign(body).replace("sha256=", "sha1="), APP_SECRET)
    assert not wa.verify_signature(body, None, APP_SECRET)
    assert not wa.verify_signature(body, sign(body, ""), "")


def test_the_subscription_echoes_the_challenge_only_with_our_token():
    assert wa.verify_subscription("subscribe", "tk", "1158201444", "tk") == "1158201444"
    assert wa.verify_subscription("subscribe", "otro", "1158201444", "tk") is None
    assert wa.verify_subscription("unsubscribe", "tk", "1158201444", "tk") is None
    assert wa.verify_subscription("subscribe", "tk", "<script>", "tk") is None
    assert wa.verify_subscription("subscribe", "", "1", "") is None


def delivery(*messages, phone_id=PHONE_ID, statuses=()):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "34600000000",
                                "phone_number_id": phone_id,
                            },
                            "contacts": [{"profile": {"name": "Marta"}, "wa_id": "34600111222"}],
                            "messages": list(messages),
                            "statuses": list(statuses),
                        },
                    }
                ],
            }
        ],
    }


def text_message(text="hola", wamid=WAMID, sender="34600111222", **extra):
    return {"from": sender, "id": wamid, "timestamp": "1763000000", "type": "text",
            "text": {"body": text}, **extra}  # fmt: skip


def test_the_messages_for_our_number_are_extracted_one_by_one():
    other = text_message(wamid="wamid.OTRO")
    items = wa.extract_messages(delivery(text_message(), other), PHONE_ID)
    assert [wa.message_id(i) for i in items] == [WAMID, "wamid.OTRO"]
    assert items[0] == {"phone_number_id": PHONE_ID, "message": text_message(), "name": "Marta"}


def test_another_number_of_the_account_statuses_and_media_are_left_out():
    assert wa.extract_messages(delivery(text_message(), phone_id="999"), PHONE_ID) == []
    image = {"from": "34600111222", "id": WAMID, "type": "image", "image": {"id": "1"}}
    assert wa.extract_messages(delivery(image), PHONE_ID) == []
    status = {"id": WAMID, "status": "failed", "recipient_id": "34600111222",
              "errors": [{"code": 131047, "title": "Re-engagement message"}]}  # fmt: skip
    payload = delivery(statuses=[status])
    assert wa.extract_messages(payload, PHONE_ID) == []
    assert wa.statuses(payload, PHONE_ID) == [("failed", 131047)]
    assert wa.extract_messages({"object": "page"}, PHONE_ID) == []
    assert wa.extract_messages([], PHONE_ID) == []


def test_a_text_is_a_text_event_with_the_phone_in_e164():
    event = wa.parse_message(text_message("de 10 a 14"), "Marta")
    assert event.channel == "whatsapp" and event.address == OWNER
    assert event.kind is InboundKind.TEXT and event.text == "de 10 a 14"
    assert event.event_id == WAMID and event.sender_name == "Marta"


def test_a_quoted_text_carries_the_id_of_the_quoted_message():
    quoted = text_message(context={"from": "34600000000", "id": "wamid.CITADO"})
    assert wa.parse_message(quoted).reply_to == "wamid.CITADO"


@pytest.mark.parametrize("text", [f"Vincular Fichaitor {TOKEN_LINK}", TOKEN_LINK,
                                  f"vincular fichaitor: {TOKEN_LINK}"])  # fmt: skip
def test_the_link_of_the_panel_is_a_start_with_its_token(text):
    event = wa.parse_message(text_message(text))
    assert event.kind is InboundKind.START and event.text == TOKEN_LINK


def test_the_six_digits_typed_are_a_text_as_on_telegram():
    event = wa.parse_message(text_message("123456"))
    assert event.kind is InboundKind.TEXT and event.text == "123456"


def test_a_reply_button_a_list_row_and_a_template_quick_reply_are_presses():
    pressed = "prop_0123456789abcdef.a1b2c3.publish"
    context = {"from": "34600000000", "id": "wamid.ORIGEN"}
    for interactive in (
        {"type": "button_reply", "button_reply": {"id": pressed, "title": "Publicar"}},
        {"type": "list_reply", "list_reply": {"id": pressed, "title": "Publicar"}},
    ):
        event = wa.parse_message({"from": "34600111222", "id": WAMID, "type": "interactive",
                                  "interactive": interactive, "context": context})  # fmt: skip
        assert event.kind is InboundKind.BUTTON and event.button_id == pressed
        assert event.message_ref == "wamid.ORIGEN" and event.callback_id == ""
    template = {"from": "34600111222", "id": WAMID, "type": "button", "context": context,
                "button": {"payload": pressed, "text": "Publicar"}}  # fmt: skip
    assert wa.parse_message(template).button_id == pressed


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        text_message(sender=None),  # a username without phone
        text_message(sender="0"),
        text_message(wamid="wamid con espacios"),
        text_message(wamid="w" * 300),
        text_message(""),
        text_message("x" * 5000),
        {"from": "34600111222", "id": WAMID, "type": "reaction", "reaction": {"emoji": "👍"}},
        {"from": "34600111222", "id": WAMID, "type": "interactive", "interactive": {"type": "x"}},
        {"from": "34600111222", "id": WAMID, "type": "button", "button": {"payload": ""}},
    ],
)
def test_anything_else_is_not_attended(bad):
    assert wa.parse_message(bad) is None


def test_the_job_payload_is_read_again_only_for_our_number():
    (item,) = wa.extract_messages(delivery(text_message()), PHONE_ID)
    assert wa.parse_update(item, PHONE_ID).address == OWNER
    assert wa.parse_update(item, "999") is None
    assert wa.parse_update({"message": text_message()}, PHONE_ID) is None


def test_the_link_opens_our_number_with_the_token_written():
    url = wa.link_url("+34 600 000 000", TOKEN_LINK)
    assert url == f"https://wa.me/34600000000?text=Vincular%20Fichaitor%20{TOKEN_LINK}"
