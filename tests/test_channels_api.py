"""The routes of the messaging channels (docs/contrato-web-api.md, "Canales de
mensajería"): link code, linked addresses, unlinking and Telegram's webhook.

No database and no network: MemoryStore and an in-memory queue. Telegram is never
called: the API only validates and enqueues.
"""

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.adapters.crypto.sealed import generate_keypair
from app.adapters.google_oauth.fake import FakeGoogleOAuth
from app.adapters.job_queue.memory import MemoryJobQueue
from app.adapters.places.fake import FakePlaces
from app.adapters.store.memory import MemoryStore
from app.api.channels import MSG_LINK_CODE_LIMIT, WEBHOOK_PATH
from app.api.main import create_app
from app.channels_config import ChannelsSettings
from app.config import Settings
from app.domain import link_codes
from app.domain.conversation import ChannelIdentity
from app.mail_config import MailSettings
from app.ports.google_oauth import Identity, ProfileLocation

START = "/api/v1/google/oauth/start"
CALLBACK = "/api/v1/google/oauth/callback"
ME = "/api/v1/me"
LINK_CODE = "/api/v1/channels/link-code"
CHANNELS = "/api/v1/me/channels"
HEADER = {"X-Requested-With": "fichaitor"}
SECRET = "secreto_del_webhook-123"
PEPPER = "pimienta-de-pruebas"
PUBLIC, _ = generate_keypair()
T0 = 1_800_000_000.0
BAR = ProfileLocation("accounts/1", "locations/1", "Bar de pruebas", "place-1", True)
SMTP = MailSettings(_env_file=None, smtp_host="smtp.example.test", mail_from="avisos@f.test")


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


class World:
    def __init__(self, secret: str = SECRET, **channels) -> None:
        self.google = FakeGoogleOAuth(locations=[BAR])
        self.store = MemoryStore()
        self.clock = Clock()
        self.queue = MemoryJobQueue(lambda: datetime.fromtimestamp(self.clock(), UTC))
        self.channels_settings = ChannelsSettings(
            _env_file=None,
            telegram_webhook_secret=secret,
            link_code_pepper=PEPPER,
            **channels,
        )
        self.app = create_app(
            Settings(
                _env_file=None,
                oauth_state_secret="secreto-de-pruebas",
                token_public_key=PUBLIC,
                cookie_secure=False,
            ),
            FakePlaces(),
            self.store,
            self.google,
            clock=self.clock,
            jobs=self.queue,
            mail_settings=SMTP,
            channels_settings=self.channels_settings,
        )

    def connect(self, sub: str = "google-sub-1") -> TestClient:
        self.google.identity_value = Identity(sub=sub, email=f"{sub}@example.com")
        client = TestClient(self.app, follow_redirects=False)
        r = client.get(START)
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        assert client.get(CALLBACK, params={"code": "c", "state": state}).status_code == 303
        return client

    def link(self, account_id: str, address: str, minutes: int = 0) -> ChannelIdentity:
        """What the conversation does when a chat sends a good code."""
        identity = ChannelIdentity(
            id=f"chid_{len(self.store.channels) + 1:016x}",
            account_id=account_id,
            channel="telegram",
            address=address,
            verified_at=datetime.fromtimestamp(T0, UTC) + timedelta(minutes=minutes),
        )
        self.store.channels.append(identity)
        return identity

    def jobs(self, kind: str) -> list:
        return [j for j in self.queue.jobs.values() if j.kind == kind]


def account_of(w: World, sub: str = "google-sub-1") -> str:
    return w.store.accounts[sub].id


# --- the link code -------------------------------------------------------------------


def test_a_link_code_has_six_digits_and_a_link_with_a_long_token_not_the_digits():
    w = World()
    client = w.connect()
    r = client.post(LINK_CODE, headers=HEADER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"code", "expires_at", "telegram_url", "whatsapp_url"}
    assert link_codes.is_code_shaped(body["code"])
    assert body["whatsapp_url"] is None
    assert body["telegram_url"].startswith("https://t.me/FichaitorBot?start=")
    token = body["telegram_url"].split("?start=", 1)[1]
    # B3: the link carries a long random token in Telegram's alphabet, never the digits.
    assert link_codes.is_token_shaped(token) and len(token) >= 43 and body["code"] not in token
    expires = datetime.fromisoformat(body["expires_at"])
    assert expires - datetime.fromtimestamp(T0, UTC) == timedelta(minutes=10)
    # Only the hashes are kept, with the pepper; neither the digits nor the token.
    (record,) = w.store.link_codes
    assert record.code_hash == link_codes.code_hash(body["code"], PEPPER)
    assert record.token_hash == link_codes.code_hash(token, PEPPER)
    assert body["code"] not in repr(w.store.link_codes) and token not in repr(w.store.link_codes)


def test_a_new_code_voids_the_previous_one():
    w = World()
    client = w.connect()
    client.post(LINK_CODE, headers=HEADER)
    client.post(LINK_CODE, headers=HEADER)
    first, second = w.store.link_codes
    assert first.used_at is not None and second.used_at is None


def test_five_codes_per_account_and_hour_then_429_with_detail():
    w = World()
    client = w.connect()
    for _ in range(5):
        assert client.post(LINK_CODE, headers=HEADER).status_code == 200
    r = client.post(LINK_CODE, headers=HEADER)
    assert r.status_code == 429 and r.json() == {"detail": MSG_LINK_CODE_LIMIT}
    # Another account has its own five.
    other = w.connect("google-sub-2")
    assert other.post(LINK_CODE, headers=HEADER).status_code == 200
    w.clock.now += 3600
    assert client.post(LINK_CODE, headers=HEADER).status_code == 200


def test_the_link_code_needs_the_header_and_a_session():
    w = World()
    assert TestClient(w.app).post(LINK_CODE, headers=HEADER).status_code == 401
    client = w.connect()
    assert client.post(LINK_CODE).status_code == 403
    assert not w.store.link_codes


def test_while_a_leave_is_scheduled_a_link_code_is_a_409():
    w = World()
    client = w.connect()
    location = client.get(ME).json()["locations"][0]["id"]
    client.post("/api/v1/me/location", json={"location_id": location}, headers=HEADER)
    client.post("/api/v1/me/offboarding/code", json={"scope": "ficha"}, headers=HEADER)
    code = [j.payload for j in w.jobs("send_mail")][-1]["code"]
    r = client.post(
        "/api/v1/me/offboarding/confirm", json={"scope": "ficha", "code": code}, headers=HEADER
    )
    assert r.status_code == 200, r.text
    r = client.post(LINK_CODE, headers=HEADER)
    assert r.status_code == 409 and "baja programada" in r.json()["detail"]
    assert not w.store.link_codes


def test_confirming_the_leave_of_the_account_unlinks_its_channels_and_voids_its_code():
    w = World()
    client = w.connect()
    client.post(LINK_CODE, headers=HEADER)
    account = account_of(w)
    w.link(account, "7001")
    other = w.connect("google-sub-2")
    w.link(account_of(w, "google-sub-2"), "7002")
    client.post("/api/v1/me/offboarding/code", json={"scope": "cuenta"}, headers=HEADER)
    code = [j.payload for j in w.jobs("send_mail")][-1]["code"]
    r = client.post(
        "/api/v1/me/offboarding/confirm", json={"scope": "cuenta", "code": code}, headers=HEADER
    )
    assert r.status_code == 200, r.text
    mine = [c for c in w.store.channels if c.account_id == account]
    assert mine and all(c.unlinked_at is not None for c in mine)
    assert all(c.used_at is not None for c in w.store.link_codes)
    # Not the channels of anybody else.
    assert len(other.get(CHANNELS).json()["channels"]) == 1


# --- the linked addresses ----------------------------------------------------------


def test_the_linked_addresses_are_listed_masked_and_ready_to_show():
    w = World()
    client = w.connect()
    assert client.get(CHANNELS).json() == {"channels": []}
    account = account_of(w)
    first = w.link(account, "123457342")
    w.link(account, "-100998877", minutes=5)
    r = client.get(CHANNELS)
    assert r.status_code == 200
    channels = r.json()["channels"]
    assert [c["address"] for c in channels] == ["Telegram de ••••7342", "Telegram de ••••8877"]
    assert channels[0] == {
        "id": first.id,
        "channel": "telegram",
        "address": "Telegram de ••••7342",
        "linked_at": channels[0]["linked_at"],
    }
    assert "123457342" not in r.text


def test_each_account_sees_only_its_own_addresses():
    w = World()
    ana = w.connect("google-sub-1")
    luis = w.connect("google-sub-2")
    w.link(account_of(w, "google-sub-2"), "5550001")
    assert ana.get(CHANNELS).json() == {"channels": []}
    assert len(luis.get(CHANNELS).json()["channels"]) == 1


def test_unlinking_answers_204_and_the_address_disappears():
    w = World()
    client = w.connect()
    identity = w.link(account_of(w), "123457342")
    r = client.delete(f"{CHANNELS}/{identity.id}", headers=HEADER)
    assert r.status_code == 204 and r.content == b""
    assert client.get(CHANNELS).json() == {"channels": []}
    # A second time it no longer exists.
    assert client.delete(f"{CHANNELS}/{identity.id}", headers=HEADER).status_code == 404


def test_unlinking_an_address_of_another_account_or_an_unknown_id_is_a_404():
    w = World()
    client = w.connect()
    w.connect("google-sub-2")
    theirs = w.link(account_of(w, "google-sub-2"), "5550001")
    for target in (theirs.id, "chid_00000000deadbeef", "otra-cosa", "chid_ZZ"):
        r = client.delete(f"{CHANNELS}/{target}", headers=HEADER)
        assert r.status_code == 404, target
        assert r.json()["detail"]
    assert w.store.channels[0].unlinked_at is None


def test_unlinking_needs_the_header():
    w = World()
    client = w.connect()
    identity = w.link(account_of(w), "123457342")
    assert client.delete(f"{CHANNELS}/{identity.id}").status_code == 403
    assert w.store.channels[0].unlinked_at is None


# --- the webhook ---------------------------------------------------------------------


def text_update(update_id: int = 10, text: str = "hola", chat: int = 1001) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 5,
            "from": {"id": chat, "is_bot": False, "first_name": "Marta"},
            "chat": {"id": chat, "type": "private"},
            "date": 1,
            "text": text,
        },
    }


def post_update(client: TestClient, update, secret: str | None = SECRET):
    headers = {"X-Telegram-Bot-Api-Secret-Token": secret} if secret is not None else {}
    body = update if isinstance(update, bytes) else json.dumps(update).encode()
    return client.post(WEBHOOK_PATH, content=body, headers=headers)


def test_the_webhook_enqueues_the_update_once_per_update_id():
    w = World()
    client = TestClient(w.app)
    update = text_update(42, "/start abc")
    assert post_update(client, update).status_code == 200
    assert post_update(client, update).status_code == 200  # Telegram delivers it again
    (job,) = w.jobs("channel_inbound")
    assert job.payload == {"channel": "telegram", "update": update}
    assert job.idempotency_key == "telegram:42" and job.max_attempts == 1


@pytest.mark.parametrize("secret", [None, "", "otro-secreto", SECRET + "x", SECRET[:-1]])
def test_the_webhook_refuses_a_request_without_the_secret(secret):
    w = World()
    r = post_update(TestClient(w.app), text_update(), secret)
    assert r.status_code == 403
    assert not w.jobs("channel_inbound")


def test_the_webhook_does_not_exist_without_a_configured_secret():
    w = World(secret="")
    r = post_update(TestClient(w.app), text_update())
    assert r.status_code in (404, 405)
    assert not w.jobs("channel_inbound")


def test_the_webhook_refuses_more_than_64_kb():
    w = World()
    big = text_update(text="a" * 70_000)
    assert post_update(TestClient(w.app), big).status_code == 413
    assert not w.jobs("channel_inbound")


@pytest.mark.parametrize(
    "update",
    [
        b"esto no es json",
        {"update_id": 1, "edited_message": {"text": "x"}},
        {"update_id": 2, "message": {"chat": {"id": -5, "type": "group"}, "text": "hola"}},
        {"message": {"text": "sin update_id"}},
        [],
    ],
)
def test_what_the_conversation_does_not_attend_is_answered_200_and_dropped(update):
    w = World()
    assert post_update(TestClient(w.app), update).status_code == 200
    assert not w.jobs("channel_inbound")
