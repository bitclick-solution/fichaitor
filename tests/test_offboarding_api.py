"""Leaving: the routes of the contract ("Baja: desconectar y borrar la cuenta").
No database: MemoryStore and an in-memory queue, where the emails wait as jobs."""

import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.adapters.crypto.sealed import generate_keypair
from app.adapters.google_oauth.fake import FakeGoogleOAuth
from app.adapters.job_queue.memory import MemoryJobQueue
from app.adapters.places.fake import FakePlaces
from app.adapters.store.memory import MemoryStore
from app.api.main import create_app
from app.api.session import MSG_LEAVE_PENDING
from app.config import Settings
from app.domain.offboarding import restore_token_hash
from app.mail_config import MailSettings
from app.ports.google_oauth import Identity, ProfileLocation

START = "/api/v1/google/oauth/start"
CALLBACK = "/api/v1/google/oauth/callback"
ME = "/api/v1/me"
CODE = "/api/v1/me/offboarding/code"
CONFIRM = "/api/v1/me/offboarding/confirm"
RESTORE = "/api/v1/me/offboarding/restore"
PUBLIC_RESTORE = "/api/v1/public/offboarding/restore"
HEADER = {"X-Requested-With": "fichaitor"}
PUBLIC, _ = generate_keypair()
T0 = 1_800_000_000.0
BAR = ProfileLocation("accounts/1", "locations/1", "Bar de pruebas", "place-1", True)
SMTP = MailSettings(_env_file=None, smtp_host="smtp.example.test", mail_from="avisos@f.test")
NO_MAIL = MailSettings(_env_file=None)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


class World:
    def __init__(self, mail: MailSettings = SMTP, **overrides) -> None:
        self.google = FakeGoogleOAuth(locations=[BAR])
        self.store = MemoryStore()
        self.clock = Clock()
        self.queue = MemoryJobQueue(lambda: datetime.fromtimestamp(self.clock(), UTC))
        settings = Settings(
            _env_file=None,
            oauth_state_secret="secreto-de-pruebas",
            token_public_key=PUBLIC,
            **{"cookie_secure": False, **overrides},
        )
        self.app = create_app(
            settings,
            FakePlaces(),
            self.store,
            self.google,
            clock=self.clock,
            jobs=self.queue,
            mail_settings=mail,
        )

    def connect(self, sub="google-sub-1", email="dueno@example.com") -> TestClient:
        self.google.identity_value = Identity(sub=sub, email=email)
        client = TestClient(self.app, follow_redirects=False)
        r = client.get(START)
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        assert client.get(CALLBACK, params={"code": "c", "state": state}).status_code == 303
        return client

    def ready(self) -> TestClient:
        """A session with a selected profile."""
        client = self.connect()
        location = client.get(ME).json()["locations"][0]["id"]
        r = client.post("/api/v1/me/location", json={"location_id": location}, headers=HEADER)
        assert r.status_code == 200
        return client

    def jobs(self, kind: str) -> list:
        return [j for j in self.queue.jobs.values() if j.kind == kind]

    def mail(self, template: str) -> dict:
        """Payload of the last email of that kind waiting to be sent."""
        return [j.payload for j in self.jobs("send_mail") if j.payload["template"] == template][-1]

    def ask(self, client: TestClient, scope: str = "ficha") -> str:
        r = client.post(CODE, json={"scope": scope}, headers=HEADER)
        assert r.status_code == 202, r.text
        return self.mail("offboarding_code")["code"]

    def leave(self, client: TestClient, scope: str = "ficha") -> dict:
        code = self.ask(client, scope)
        r = client.post(CONFIRM, json={"scope": scope, "code": code}, headers=HEADER)
        assert r.status_code == 200, r.text
        return r.json()


def wrong(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


# --- asking for a code -----------------------------------------------------------


def test_asking_for_a_code_answers_202_with_the_masked_email_and_enqueues_the_email():
    w = World()
    client = w.ready()
    r = client.post(CODE, json={"scope": "ficha"}, headers=HEADER)
    assert r.status_code == 202
    assert r.json() == {"sent_to": "d***o@example.com", "expires_in": 600}
    payload = w.mail("offboarding_code")
    assert payload["to"] == "dueno@example.com" and payload["scope"] == "ficha"
    assert len(payload["code"]) == 6 and payload["code"].isdigit() and payload["minutes"] == 10
    # Only a fingerprint is stored.
    (stored,) = w.store.codes
    assert payload["code"] not in repr(stored) and len(stored.code_hash) == 64
    assert client.get(CODE).json() == {"status": "pendiente"}


def test_the_routes_need_a_session_and_the_protection_header():
    w = World()
    anonymous = TestClient(w.app)
    for path, body in ((CODE, {"scope": "ficha"}), (CONFIRM, {"scope": "ficha", "code": "1"})):
        assert anonymous.post(path, json=body, headers=HEADER).status_code == 401
    assert anonymous.post(RESTORE, headers=HEADER).status_code == 401
    client = w.ready()
    assert client.post(CODE, json={"scope": "ficha"}).status_code == 403
    assert client.post(CONFIRM, json={"scope": "ficha", "code": "123456"}).status_code == 403
    assert client.post(RESTORE).status_code == 403
    assert client.post(CODE, json={"scope": "todo"}, headers=HEADER).status_code == 422
    assert w.store.codes == []


def test_without_mail_configured_a_code_is_a_503_and_nothing_is_created():
    w = World(mail=NO_MAIL)
    client = w.ready()
    r = client.post(CODE, json={"scope": "cuenta"}, headers=HEADER)
    assert r.status_code == 503 and "correo" in r.json()["detail"]
    assert w.store.codes == [] and w.jobs("send_mail") == []


def test_too_many_codes_are_a_429_per_account():
    w = World(offboarding_code_rate_limit=2)
    client = w.ready()
    assert client.post(CODE, json={"scope": "ficha"}, headers=HEADER).status_code == 202
    assert client.post(CODE, json={"scope": "ficha"}, headers=HEADER).status_code == 202
    r = client.post(CODE, json={"scope": "ficha"}, headers=HEADER)
    assert r.status_code == 429 and isinstance(r.json()["detail"], str)
    w.clock.now += 3601
    assert client.post(CODE, json={"scope": "ficha"}, headers=HEADER).status_code == 202


def test_the_web_can_learn_that_the_email_could_not_be_sent():
    w = World()
    client = w.ready()
    assert client.get(CODE).json() == {"status": "ninguno"}
    w.ask(client)
    w.store.codes[-1].mail_state = "failed"  # what the worker writes when it gives up
    assert client.get(CODE).json() == {"status": "fallido"}
    w.store.codes[-1].mail_state = "sent"
    assert client.get(CODE).json() == {"status": "enviado"}


# --- confirming ------------------------------------------------------------------


def test_the_right_code_schedules_the_leave_48_hours_ahead_and_is_spent():
    w = World()
    client = w.ready()
    code = w.ask(client)
    r = client.post(CONFIRM, json={"scope": "ficha", "code": code}, headers=HEADER)
    assert r.status_code == 200
    delete_at = datetime.fromtimestamp(T0, UTC) + timedelta(hours=48)
    assert r.json() == {"scope": "ficha", "delete_at": delete_at.isoformat().replace("+00:00", "Z")}
    assert client.get(ME).json()["pending_deletion"] == r.json()
    # The job that makes it effective waits until that date; nothing is revoked now.
    (apply,) = w.jobs("apply_offboarding")
    assert apply.run_at == delete_at and set(apply.payload) == {"offboarding_id"}
    assert len(w.store.connections) == 1
    # The notice carries the restore token; the store keeps only its hash.
    notice = w.mail("offboarding_scheduled")
    assert notice["delete_at"] == delete_at.isoformat() and notice["scope"] == "ficha"
    (leave,) = w.store.offboardings
    assert leave.restore_token_hash == restore_token_hash(notice["token"])
    assert notice["token"] not in repr(leave)
    # Spent: the same code is worth nothing after a restore.
    assert client.post(RESTORE, headers=HEADER).status_code == 204
    again = client.post(CONFIRM, json={"scope": "ficha", "code": code}, headers=HEADER)
    assert again.status_code == 410


def test_a_wrong_code_says_how_many_attempts_are_left_and_the_fifth_kills_the_code():
    w = World()
    client = w.ready()
    code = w.ask(client)
    for left in (4, 3, 2, 1):
        r = client.post(CONFIRM, json={"scope": "ficha", "code": wrong(code)}, headers=HEADER)
        assert r.status_code == 422
        expected = "Te queda 1 intento." if left == 1 else f"Te quedan {left} intentos."
        assert expected in r.json()["detail"]
    r = client.post(CONFIRM, json={"scope": "ficha", "code": wrong(code)}, headers=HEADER)
    assert r.status_code == 429 and isinstance(r.json()["detail"], str)
    # Dead: not even the right code works now.
    r = client.post(CONFIRM, json={"scope": "ficha", "code": code}, headers=HEADER)
    assert r.status_code == 410
    assert client.get(ME).json()["pending_deletion"] is None


def test_a_malformed_code_is_a_wrong_code_with_a_message_ready_to_show():
    w = World()
    client = w.ready()
    w.ask(client)
    r = client.post(CONFIRM, json={"scope": "ficha", "code": "abc"}, headers=HEADER)
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)


def test_a_code_expires_after_ten_minutes():
    w = World()
    client = w.ready()
    code = w.ask(client)
    w.clock.now += 599
    assert (
        client.post(
            CONFIRM, json={"scope": "ficha", "code": wrong(code)}, headers=HEADER
        ).status_code
        == 422
    )
    w.clock.now += 2
    r = client.post(CONFIRM, json={"scope": "ficha", "code": code}, headers=HEADER)
    assert r.status_code == 410 and isinstance(r.json()["detail"], str)


def test_confirming_without_ever_asking_is_a_410():
    client = World().ready()
    r = client.post(CONFIRM, json={"scope": "ficha", "code": "123456"}, headers=HEADER)
    assert r.status_code == 410


def test_asking_again_voids_the_earlier_code():
    w = World()
    client = w.ready()
    first = w.ask(client)
    second = w.ask(client)
    if first != second:
        r = client.post(CONFIRM, json={"scope": "ficha", "code": first}, headers=HEADER)
        assert r.status_code == 422
    assert w.store.codes[0].invalidated_at is not None
    assert (
        client.post(CONFIRM, json={"scope": "ficha", "code": second}, headers=HEADER).status_code
        == 200
    )


def test_a_code_for_the_profile_is_no_good_for_the_account():
    w = World()
    client = w.ready()
    code = w.ask(client, "ficha")
    r = client.post(CONFIRM, json={"scope": "cuenta", "code": code}, headers=HEADER)
    assert r.status_code == 422
    assert client.get(ME).json()["pending_deletion"] is None


def test_a_code_is_no_good_from_another_session_of_the_same_account():
    w = World()
    client = w.ready()
    other = w.connect()  # same Google identity, another browser
    code = w.ask(client)
    r = other.post(CONFIRM, json={"scope": "ficha", "code": code}, headers=HEADER)
    assert r.status_code == 422
    assert (
        client.post(CONFIRM, json={"scope": "ficha", "code": code}, headers=HEADER).status_code
        == 200
    )


def test_a_code_of_one_account_is_no_good_for_another():
    w = World()
    ana = w.ready()
    luis = w.connect("google-sub-2", "luis@example.com")
    code = w.ask(ana)
    r = luis.post(CONFIRM, json={"scope": "ficha", "code": code}, headers=HEADER)
    assert r.status_code == 410  # that account never asked for one
    assert luis.get(ME).json()["pending_deletion"] is None


# --- while the leave is scheduled --------------------------------------------------

CLOSED = [
    ("get", "/api/v1/me/summary", None),
    ("get", "/api/v1/me/diagnosis", None),
    ("get", "/api/v1/me/reviews", None),
    ("get", "/api/v1/me/sentiment", None),
    ("post", "/api/v1/me/reviews/rev_0123456789abcdef/draft", None),
    ("post", "/api/v1/me/reviews/rev_0123456789abcdef/reply", {"text": "Gracias."}),
    ("get", "/api/v1/me/actions/act_0123456789abcdef", None),
    ("post", "/api/v1/me/location", {"location_id": "loc_000000000000"}),
    ("post", CODE, {"scope": "cuenta"}),
    ("post", CONFIRM, {"scope": "cuenta", "code": "123456"}),
]


@pytest.mark.parametrize(("method", "path", "body"), CLOSED)
def test_with_a_scheduled_leave_every_route_of_the_profile_is_a_409(method, path, body):
    w = World()
    client = w.ready()
    assert client.get("/api/v1/me/summary").status_code == 200
    w.leave(client)
    syncs = len(w.jobs("sync_profile"))
    r = client.request(method, path, json=body, headers=HEADER)
    assert r.status_code == 409, r.text
    assert "baja programada" in r.json()["detail"]
    if not path.startswith("/api/v1/me/offboarding"):
        assert r.json()["detail"] == MSG_LEAVE_PENDING
    # Nothing is read: no sync is asked for.
    assert len(w.jobs("sync_profile")) == syncs


def test_with_a_scheduled_leave_the_store_itself_gives_no_data():
    w = World()
    client = w.ready()
    account = next(iter(w.store.accounts.values()))
    assert w.app is not None and client.get("/api/v1/me/summary").status_code == 200
    w.leave(client)

    async def read():
        now = datetime.fromtimestamp(T0, UTC)
        return (
            await w.store.selected_profile(account.id),
            await w.store.list_reviews(account.id, unanswered_only=False, limit=50),
            await w.store.sentiment(account.id, now),
        )

    import asyncio

    profile, reviews, sentiment = asyncio.run(read())
    assert profile is None and reviews.total == 0 and sentiment is None


def test_connecting_again_during_a_scheduled_leave_reads_nothing():
    w = World()
    client = w.ready()
    w.leave(client)
    syncs = len(w.jobs("sync_profile"))
    again = w.connect()
    assert again.get(ME).json()["pending_deletion"]["scope"] == "ficha"
    assert again.get("/api/v1/me/summary").status_code == 409
    assert len(w.jobs("sync_profile")) == syncs


# --- restoring ---------------------------------------------------------------------


def test_restoring_from_the_session_brings_everything_back_and_asks_for_a_sync():
    w = World()
    client = w.ready()
    w.leave(client)
    token = w.mail("offboarding_scheduled")["token"]
    syncs = len(w.jobs("sync_profile"))
    r = client.post(RESTORE, headers=HEADER)
    assert r.status_code == 204 and r.content == b""
    assert client.get(ME).json()["pending_deletion"] is None
    assert client.get("/api/v1/me/summary").status_code == 200
    assert len(w.jobs("sync_profile")) > syncs
    # The link of the email died with the restore.
    assert TestClient(w.app).post(PUBLIC_RESTORE, json={"token": token}).status_code == 410
    # Nothing to restore any more.
    assert client.post(RESTORE, headers=HEADER).status_code == 409


def test_restoring_from_the_link_needs_no_session_and_works_once():
    w = World()
    client = w.ready()
    w.leave(client)
    token = w.mail("offboarding_scheduled")["token"]
    anonymous = TestClient(w.app)
    syncs = len(w.jobs("sync_profile"))
    r = anonymous.post(PUBLIC_RESTORE, json={"token": token})
    assert r.status_code == 204
    assert client.get(ME).json()["pending_deletion"] is None
    assert len(w.jobs("sync_profile")) > syncs
    used = anonymous.post(PUBLIC_RESTORE, json={"token": token})
    unknown = anonymous.post(PUBLIC_RESTORE, json={"token": "no-existe"})
    assert used.status_code == unknown.status_code == 410
    assert used.json() == unknown.json()  # nothing tells one case from the other


def restored_mails(w: World) -> list[dict]:
    return [
        j.payload for j in w.jobs("send_mail") if j.payload["template"] == "offboarding_restored"
    ]


@pytest.mark.parametrize("scope", ["ficha", "cuenta"])
def test_restoring_from_the_session_sends_the_restored_email(scope):
    w = World()
    client = w.ready()
    w.leave(client, scope)
    if scope == "cuenta":
        client = w.connect()  # its sessions were closed: it signs in again
    assert restored_mails(w) == []
    assert client.post(RESTORE, headers=HEADER).status_code == 204
    assert restored_mails(w) == [
        {"template": "offboarding_restored", "to": "dueno@example.com", "scope": scope}
    ]


@pytest.mark.parametrize("scope", ["ficha", "cuenta"])
def test_restoring_from_the_link_sends_the_restored_email_once(scope):
    w = World()
    client = w.ready()
    w.leave(client, scope)
    token = w.mail("offboarding_scheduled")["token"]
    anonymous = TestClient(w.app)
    assert anonymous.post(PUBLIC_RESTORE, json={"token": token}).status_code == 204
    assert anonymous.post(PUBLIC_RESTORE, json={"token": token}).status_code == 410
    # No token, no code: only what the email needs.
    assert restored_mails(w) == [
        {"template": "offboarding_restored", "to": "dueno@example.com", "scope": scope}
    ]


def test_the_restored_email_goes_to_the_address_the_account_has_when_restoring():
    w = World()
    client = w.ready()
    w.leave(client, "cuenta")
    w.connect(email="nuevo@example.com")  # Google says the address changed meanwhile
    token = w.mail("offboarding_scheduled")["token"]
    assert TestClient(w.app).post(PUBLIC_RESTORE, json={"token": token}).status_code == 204
    assert [m["to"] for m in restored_mails(w)] == ["nuevo@example.com"]


def test_without_an_address_left_the_leave_is_restored_and_no_email_is_sent():
    w = World()
    client = w.ready()
    w.leave(client, "cuenta")
    token = w.mail("offboarding_scheduled")["token"]
    (account,) = w.store.accounts.values()
    account.email = ""
    assert TestClient(w.app).post(PUBLIC_RESTORE, json={"token": token}).status_code == 204
    assert restored_mails(w) == []


class FailingOnce:
    """Wraps the queue: the restored email cannot be enqueued, once."""

    def __init__(self, inner) -> None:
        self.inner, self.failed = inner, False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def enqueue(self, kind, payload, **options):
        if payload.get("template") == "offboarding_restored" and not self.failed:
            self.failed = True
            raise RuntimeError("fallo simulado a mitad de la restauración")
        return await self.inner.enqueue(kind, payload, **options)


def test_a_server_error_while_restoring_from_the_link_does_not_spend_the_token():
    w = World()
    client = w.ready()
    w.leave(client)
    token = w.mail("offboarding_scheduled")["token"]
    w.store.jobs = FailingOnce(w.store.jobs)
    anonymous = TestClient(w.app)
    failed = anonymous.post(PUBLIC_RESTORE, json={"token": token})
    assert failed.status_code == 503
    # Nothing changed: the leave is still scheduled and the link still works.
    assert client.get(ME).json()["pending_deletion"]["scope"] == "ficha"
    assert anonymous.post(PUBLIC_RESTORE, json={"token": token}).status_code == 204
    assert client.get(ME).json()["pending_deletion"] is None
    assert len(restored_mails(w)) == 1


def test_a_server_error_while_restoring_from_the_session_leaves_the_leave_scheduled():
    w = World()
    client = w.ready()
    w.leave(client)
    w.store.jobs = FailingOnce(w.store.jobs)
    assert client.post(RESTORE, headers=HEADER).status_code == 503
    assert client.get(ME).json()["pending_deletion"]["scope"] == "ficha"
    assert client.post(RESTORE, headers=HEADER).status_code == 204


def test_the_link_stops_working_at_the_date_of_the_leave():
    w = World()
    client = w.ready()
    w.leave(client)
    token = w.mail("offboarding_scheduled")["token"]
    w.clock.now += 48 * 3600
    late = TestClient(w.app).post(PUBLIC_RESTORE, json={"token": token})
    unknown = TestClient(w.app).post(PUBLIC_RESTORE, json={"token": "no-existe"})
    assert late.status_code == 410 and late.json() == unknown.json()


def test_the_public_restore_is_limited_per_ip():
    w = World(offboarding_restore_rate_limit=3)
    anonymous = TestClient(w.app)
    for _ in range(3):
        assert anonymous.post(PUBLIC_RESTORE, json={"token": "x"}).status_code == 410
    r = anonymous.post(PUBLIC_RESTORE, json={"token": "x"})
    assert r.status_code == 429 and isinstance(r.json()["detail"], str)


def test_the_token_and_the_code_never_travel_in_a_url():
    w = World()
    paths = {path for path in w.app.openapi()["paths"] if "offboarding" in path}
    assert paths == {CODE, CONFIRM, RESTORE, PUBLIC_RESTORE}
    anonymous = TestClient(w.app)
    assert anonymous.post(f"{PUBLIC_RESTORE}?token=x").status_code == 422  # body only
    assert anonymous.get(PUBLIC_RESTORE, params={"token": "x"}).status_code == 405


# --- deleting the account ------------------------------------------------------------


def test_deleting_the_account_closes_every_session_at_once():
    w = World()
    client = w.ready()
    other = w.connect()
    assert other.get(ME).status_code == 200
    w.leave(client, "cuenta")
    assert client.get(ME).status_code == 401 and other.get(ME).status_code == 401
    assert client.post(RESTORE, headers=HEADER).status_code == 401
    # Only the link of the email restores it.
    token = w.mail("offboarding_scheduled")["token"]
    assert TestClient(w.app).post(PUBLIC_RESTORE, json={"token": token}).status_code == 204
    assert client.get(ME).status_code == 401  # the closed sessions stay closed


def test_signing_in_during_the_48_hours_shows_the_leave_and_can_only_restore_it():
    w = World()
    client = w.ready()
    w.leave(client, "cuenta")
    back = w.connect()  # same Google identity: the same account, no new one
    assert len(w.store.accounts) == 1
    me = back.get(ME).json()
    assert me["pending_deletion"]["scope"] == "cuenta"
    assert back.get("/api/v1/me/summary").status_code == 409
    assert back.post(CODE, json={"scope": "ficha"}, headers=HEADER).status_code == 409
    assert back.post(RESTORE, headers=HEADER).status_code == 204
    assert back.get(ME).json()["pending_deletion"] is None


# --- nothing sensitive in the logs ---------------------------------------------------


def test_neither_the_code_nor_the_token_nor_the_address_reach_the_logs_of_the_api(caplog):
    w = World()
    with caplog.at_level(logging.DEBUG):
        client = w.ready()
        code = w.ask(client)
        client.post(CONFIRM, json={"scope": "ficha", "code": wrong(code)}, headers=HEADER)
        client.post(CONFIRM, json={"scope": "ficha", "code": code}, headers=HEADER)
        token = w.mail("offboarding_scheduled")["token"]
        TestClient(w.app).post(PUBLIC_RESTORE, json={"token": token})
    assert code not in caplog.text and token not in caplog.text
    assert "dueno@example.com" not in caplog.text


def test_with_fake_mail_and_no_database_the_code_is_written_to_the_log(caplog):
    fake = MailSettings(_env_file=None, fake_mail=True)
    google = FakeGoogleOAuth(locations=[BAR])
    store = MemoryStore()
    settings = Settings(
        _env_file=None, oauth_state_secret="s", token_public_key=PUBLIC, cookie_secure=False
    )
    app = create_app(settings, FakePlaces(), store, google, mail_settings=fake)
    client = TestClient(app, follow_redirects=False)
    state = parse_qs(urlparse(client.get(START).headers["location"]).query)["state"][0]
    client.get(CALLBACK, params={"code": "c", "state": state})
    with caplog.at_level(logging.WARNING, logger="fichaitor.mail"):
        assert client.post(CODE, json={"scope": "ficha"}, headers=HEADER).status_code == 202
    assert "[CORREO SIMULADO]" in caplog.text and "Has pedido desconectar tu ficha" in caplog.text
