"""The WhatsApp side of the API and of the conversation process: the link of the panel,
the webhook (verification, signature, size, one job per message) and the job that hands a
message to the conversation. No database and no network: Meta is never called."""

import asyncio
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.api.channels import WHATSAPP_WEBHOOK_PATH
from app.domain import channel_jobs
from app.domain.jobs import Job
from app.worker.channel_inbound import ChannelInbound
from app.worker.config import ConfigError
from app.worker.conversation import ConversationSettings
from tests.test_channels_api import HEADER, LINK_CODE, World
from tests.test_whatsapp import PHONE_ID, WAMID, delivery, text_message

APP_SECRET = "secreto-de-la-app-de-meta-0123456789"
VERIFY = "testigo-de-verificacion-0123456789"
WHATSAPP = {
    "whatsapp_phone_number_id": PHONE_ID,
    "whatsapp_app_secret": APP_SECRET,
    "whatsapp_verify_token": VERIFY,
    "whatsapp_display_number": "+34 600 000 000",
}


def post(client, payload, secret=APP_SECRET, raw: bytes | None = None, signature=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    sign = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {"Content-Type": "application/json", "X-Hub-Signature-256": signature or sign}
    return client.post(WHATSAPP_WEBHOOK_PATH, content=body, headers=headers)


# --- the link ------------------------------------------------------------------------


def test_the_link_code_carries_the_wa_me_link_with_the_token_when_whatsapp_is_configured():
    w = World(**WHATSAPP)
    client = w.connect()
    body = client.post(LINK_CODE, headers=HEADER).json()
    token = body["telegram_url"].split("start=")[1]
    assert body["whatsapp_url"] == (
        f"https://wa.me/34600000000?text=Vincular%20Fichaitor%20{token}"
    )
    assert body["code"] not in body["whatsapp_url"]


@pytest.mark.parametrize(
    "missing", ["whatsapp_display_number", "whatsapp_app_secret", "whatsapp_verify_token"]
)
def test_without_any_piece_of_whatsapp_the_link_is_null(missing):
    w = World(**{**WHATSAPP, missing: ""})
    body = w.connect().post(LINK_CODE, headers=HEADER).json()
    assert body["whatsapp_url"] is None


# --- verification --------------------------------------------------------------------


def test_meta_verifies_the_callback_with_our_token():
    client = TestClient(World(**WHATSAPP).app)
    params = {"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "1158201444"}
    r = client.get(WHATSAPP_WEBHOOK_PATH, params=params)
    assert r.status_code == 200 and r.text == "1158201444"
    assert r.headers["content-type"].startswith("text/plain")
    r = client.get(WHATSAPP_WEBHOOK_PATH, params={**params, "hub.verify_token": "otro"})
    assert r.status_code == 403


@pytest.mark.parametrize(
    "missing",
    ["whatsapp_app_secret", "whatsapp_verify_token", "whatsapp_phone_number_id"],
)
def test_the_routes_do_not_exist_without_their_secrets(missing):
    w = World(**{**WHATSAPP, missing: ""})
    client = TestClient(w.app)
    assert post(client, delivery(text_message())).status_code in (404, 405)
    assert client.get(WHATSAPP_WEBHOOK_PATH).status_code in (404, 405)
    assert not w.jobs("channel_inbound")


# --- receiving -----------------------------------------------------------------------


def test_each_message_for_our_number_is_enqueued_once_with_its_wamid():
    w = World(**WHATSAPP)
    client = TestClient(w.app)
    payload = delivery(text_message(), text_message("otro", wamid="wamid.SEGUNDO"))
    assert post(client, payload).status_code == 200
    assert post(client, payload).status_code == 200  # Meta delivers it again
    jobs = sorted(w.jobs("channel_inbound"), key=lambda j: j.idempotency_key)
    assert [j.idempotency_key for j in jobs] == [f"whatsapp:{WAMID}", "whatsapp:wamid.SEGUNDO"]
    assert jobs[0].payload == {
        "channel": "whatsapp",
        "update": {"phone_number_id": PHONE_ID, "message": text_message(), "name": "Marta"},
    }
    assert jobs[0].max_attempts == 1


@pytest.mark.parametrize(
    "signature",
    [None, "", "sha256=00", "sha1=abc", "sha256=" + "0" * 64],
)
def test_a_delivery_without_a_good_signature_is_refused(signature):
    w = World(**WHATSAPP)
    client = TestClient(w.app)
    body = json.dumps(delivery(text_message())).encode()
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    assert client.post(WHATSAPP_WEBHOOK_PATH, content=body, headers=headers).status_code == 403
    assert post(client, delivery(text_message()), secret="otro-secreto").status_code == 403
    assert not w.jobs("channel_inbound")


def test_a_delivery_over_64_kb_is_refused_unread():
    w = World(**WHATSAPP)
    big = delivery(text_message("a" * 70_000))
    assert post(TestClient(w.app), big).status_code == 413
    assert not w.jobs("channel_inbound")


def test_statuses_other_numbers_and_bad_json_are_answered_200_and_dropped():
    w = World(**WHATSAPP)
    client = TestClient(w.app)
    status = {"id": WAMID, "status": "delivered", "recipient_id": "34600111222"}
    assert post(client, delivery(statuses=[status])).status_code == 200
    assert post(client, delivery(text_message(), phone_id="999")).status_code == 200
    assert post(client, None, raw=b"esto no es json").status_code == 200
    assert not w.jobs("channel_inbound")


# --- the job -------------------------------------------------------------------------


class Service:
    def __init__(self) -> None:
        self.events = []

    async def handle_inbound(self, event) -> None:
        self.events.append(event)


class Jobs:
    def __init__(self) -> None:
        self.scrubbed = []

    async def scrub_job(self, job_id: str) -> None:
        self.scrubbed.append(job_id)

    async def scrub_finished_jobs(self, kind: str) -> int:
        return 0


def test_the_job_hands_the_message_to_the_conversation_only_for_our_number():
    from app.adapters.messaging import whatsapp

    service, jobs = Service(), Jobs()
    handler = ChannelInbound(
        service, jobs, {"whatsapp": lambda u: whatsapp.parse_update(u, PHONE_ID)}
    )
    item = {"phone_number_id": PHONE_ID, "message": text_message("hola"), "name": "Marta"}
    asyncio.run(
        handler(Job("w1", channel_jobs.CHANNEL_INBOUND, {"channel": "whatsapp", "update": item}))
    )
    other = {"channel": "whatsapp", "update": {**item, "phone_number_id": "9"}}
    asyncio.run(handler(Job("w2", channel_jobs.CHANNEL_INBOUND, other)))
    (event,) = service.events
    assert event.channel == "whatsapp" and event.address == "+34600111222"
    assert jobs.scrubbed == ["w1", "w2"]


def test_the_conversation_refuses_the_secrets_of_the_webhook():
    settings = ConversationSettings(
        _env_file=None,
        conversation_database_url="postgresql://x/y",
        whatsapp_app_secret=APP_SECRET,
    )
    with pytest.raises(ConfigError):
        settings.check()


# --- GET /me -------------------------------------------------------------------------


class Counters:
    def __init__(self, used=0, fail=False) -> None:
        self.used, self.fail, self.asked = used, fail, []

    async def quota_used(self, scope, start):
        self.asked.append((scope, start))
        if self.fail:
            raise RuntimeError("sin base")
        return self.used


def test_me_shows_the_bag_of_the_month_only_with_pro_plus():
    from datetime import UTC, datetime

    from app.api.me import _whatsapp_bag

    now = datetime(2026, 11, 30, 23, 30, tzinfo=UTC)  # already 1 December in Madrid
    store = Counters(used=37)
    bag = asyncio.run(_whatsapp_bag(store, "acc", 2, now))
    assert bag.model_dump() == {"month": "2026-12", "used": 37, "limit": 160, "remaining": 123}
    assert store.asked[0][0] == "whatsapp_month:acc"
    assert asyncio.run(_whatsapp_bag(Counters(), "acc", 0, now)) is None
    broken = asyncio.run(_whatsapp_bag(Counters(fail=True), "acc", 1, now))
    assert broken.used == 0 and broken.remaining == 80
