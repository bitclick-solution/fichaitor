"""The opposition to the commercial communications at the sign-up and in Ajustes (LSSI
art. 21.2; docs/contrato-web-api.md, «Comunicaciones comerciales: en el alta y en
Ajustes»). No database: MemoryStore. The same against PostgreSQL in
tests/test_communication_preferences_postgres.py."""

import logging
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.adapters.crypto.sealed import generate_keypair
from app.adapters.google_oauth.fake import FakeGoogleOAuth
from app.adapters.places.fake import FakePlaces
from app.adapters.store.memory import MemoryStore
from app.api.main import create_app
from app.config import Settings
from app.domain.oauth_state import InvalidState, OAuthState, make_state, verify_state
from app.ports.google_oauth import Identity

START = "/api/v1/google/oauth/start"
CALLBACK = "/api/v1/google/oauth/callback"
PREFS = "/api/v1/me/communications"
ME = "/api/v1/me"
HEADER = {"X-Requested-With": "fichaitor"}
SECRET = "secreto-de-pruebas"
PUBLIC, _ = generate_keypair()


class World:
    def __init__(self) -> None:
        self.google = FakeGoogleOAuth()
        self.store = MemoryStore()
        settings = Settings(
            _env_file=None, oauth_state_secret=SECRET, token_public_key=PUBLIC, cookie_secure=False
        )
        self.app = create_app(settings, FakePlaces(), self.store, self.google)

    def connect(self, optout: str | None = None, sub: str = "google-sub-1") -> TestClient:
        self.google.identity_value = Identity(sub=sub, email=f"{sub}@example.com")
        client = TestClient(self.app, follow_redirects=False)
        params = {} if optout is None else {"commercial_optout": optout}
        r = client.get(START, params=params)
        assert r.status_code == 302
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        r = client.get(CALLBACK, params={"code": "c", "state": state})
        assert r.status_code == 303 and "google=ok" in r.headers["location"]
        return client

    def account_id(self, sub: str = "google-sub-1") -> str:
        return self.store.accounts[sub].id


# --- the signed state -------------------------------------------------------------------


def test_the_state_carries_the_box_signed_and_it_cannot_be_flipped():
    off = make_state(SECRET, 1000)
    on = make_state(SECRET, 1000, commercial_optout=True)
    assert verify_state(SECRET, off, off, 1300) == OAuthState(commercial_optout=False)
    assert verify_state(SECRET, on, on, 1300) == OAuthState(commercial_optout=True)
    issued, nonce, flag, sig = on.split(".")
    assert flag == "1"
    flipped = f"{issued}.{nonce}.0.{sig}"  # same cookie and state, flag changed
    for bad in (flipped, f"{issued}.{nonce}.2.{sig}", f"{issued}.{nonce}.{sig}"):
        with pytest.raises(InvalidState):
            verify_state(SECRET, bad, bad, 1300)
    # The state in the URL must be the one of the cookie: a flag changed only in the URL
    # (Google's redirect) does not pass either.
    with pytest.raises(InvalidState):
        verify_state(SECRET, flipped, on, 1300)


def test_the_box_never_goes_in_a_cookie_of_its_own():
    world = World()
    client = TestClient(world.app, follow_redirects=False)
    r = client.get(START, params={"commercial_optout": "1"})
    cookies = r.headers.get_list("set-cookie")
    assert len(cookies) == 1 and cookies[0].startswith("fichaitor_oauth_state=")
    assert "optout" not in cookies[0].lower()


# --- at the sign-up ---------------------------------------------------------------------


def test_the_box_of_the_sign_up_opposes_a_new_account():
    world = World()
    client = world.connect(optout="1")
    assert world.store.communication_optout == {world.account_id(): "signup"}
    body = client.get(PREFS).json()
    assert body["commercial"] is False and body["source"] == "signup"
    assert body["changed_at"] is not None


def test_without_the_box_a_new_account_gets_them_by_legitimate_interest():
    world = World()
    client = world.connect()
    assert world.store.communication_optout == {}
    assert client.get(PREFS).json() == {"commercial": True, "changed_at": None, "source": None}
    world.connect(optout="0", sub="google-sub-2")
    assert world.store.communication_optout == {}


def test_the_box_does_not_touch_an_account_that_already_existed():
    world = World()
    client = world.connect()  # created without the box
    world.connect(optout="1")  # a later sign-in with it ticked
    assert world.store.communication_optout == {}
    assert client.get(PREFS).json()["commercial"] is True
    # Nor the other way round: an opposed account stays opposed if it signs in without it.
    world.connect(optout="1", sub="google-sub-2")
    world.connect(sub="google-sub-2")
    assert world.store.communication_optout == {world.account_id("google-sub-2"): "signup"}


def test_a_state_tampered_with_records_nothing():
    world = World()
    client = TestClient(world.app, follow_redirects=False)
    r = client.get(START)  # without the box
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    issued, nonce, _, sig = state.split(".")
    forged = f"{issued}.{nonce}.1.{sig}"
    assert client.get(CALLBACK, params={"code": "c", "state": forged}).status_code == 400
    assert world.store.accounts == {} and world.store.communication_optout == {}


def test_a_value_that_is_not_a_boolean_is_refused_before_going_to_google():
    world = World()
    client = TestClient(world.app, follow_redirects=False)
    assert client.get(START, params={"commercial_optout": "quizas"}).status_code == 422


# --- in Ajustes -------------------------------------------------------------------------


def test_ajustes_opposes_and_withdraws_idempotently():
    world = World()
    client = world.connect()
    account = world.account_id()

    off = client.put(PREFS, json={"commercial": False}, headers=HEADER)
    assert off.status_code == 200
    assert off.json()["commercial"] is False and off.json()["source"] == "settings"
    first_change = off.json()["changed_at"]
    assert world.store.communication_optout == {account: "settings"}

    again = client.put(PREFS, json={"commercial": False}, headers=HEADER)
    assert again.json() == off.json()  # the first opposition keeps its date and source
    assert client.get(PREFS).json() == off.json()
    assert first_change is not None

    on = client.put(PREFS, json={"commercial": True}, headers=HEADER)
    assert on.status_code == 200
    assert on.json()["commercial"] is True and on.json()["source"] == "settings"
    assert on.json()["changed_at"] is not None
    assert world.store.communication_optout == {}
    assert client.put(PREFS, json={"commercial": True}, headers=HEADER).json() == on.json()


def test_an_opposition_by_the_email_link_shows_in_ajustes_and_can_be_withdrawn():
    import asyncio

    world = World()
    client = world.connect()
    asyncio.run(world.store.unsubscribe_communications(world.account_id(), "un_clic"))
    assert client.get(PREFS).json()["source"] == "un_clic"
    client.put(PREFS, json={"commercial": True}, headers=HEADER)
    assert world.store.communication_optout == {}


def test_ajustes_needs_a_session_the_csrf_header_and_a_strict_body():
    world = World()
    anonymous = TestClient(world.app)
    assert anonymous.get(PREFS).status_code == 401
    assert anonymous.put(PREFS, json={"commercial": False}, headers=HEADER).status_code == 401
    client = world.connect()
    assert client.put(PREFS, json={"commercial": False}).status_code == 403
    for body in (
        {},
        {"commercial": "false"},
        {"commercial": 0},
        {"commercial": None},
        {"commercial": False, "account_id": "otra"},
    ):
        assert client.put(PREFS, json=body, headers=HEADER).status_code == 422
    assert world.store.communication_optout == {}


def test_each_session_only_changes_its_own_account():
    world = World()
    ana = world.connect(sub="google-sub-ana")
    luis = world.connect(sub="google-sub-luis")
    ana.put(PREFS, json={"commercial": False}, headers=HEADER)
    assert world.store.communication_optout == {world.account_id("google-sub-ana"): "settings"}
    assert luis.get(PREFS).json()["commercial"] is True


def test_ajustes_answers_during_a_scheduled_leave_and_a_billing_block():
    from datetime import UTC, datetime

    from app.domain.offboarding import PendingDeletion
    from app.ports.store import BillingBlock

    world = World()
    client = world.connect()
    original = world.store.account_for_session

    async def leaving(token_hash, now):
        from dataclasses import replace

        account = await original(token_hash, now)
        return replace(
            account,
            pending_deletion=PendingDeletion("cuenta", datetime(2030, 1, 1, tzinfo=UTC)),
            blocked=BillingBlock(datetime(2029, 1, 1, tzinfo=UTC), "payment_failed"),
        )

    world.store.account_for_session = leaving  # type: ignore[method-assign]
    assert client.put(PREFS, json={"commercial": False}, headers=HEADER).status_code == 200


def test_a_store_failure_is_a_503_without_identifiers_in_the_log(caplog):
    world = World()
    client = world.connect()
    account = world.account_id()

    async def broken(*_args, **_kwargs):
        raise RuntimeError(f"fallo con {account}")

    world.store.unsubscribe_communications = broken  # type: ignore[method-assign]
    world.store.communication_preference = broken  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        r = client.put(PREFS, json={"commercial": False}, headers=HEADER)
        assert r.status_code == 503
        assert client.get(PREFS).status_code == 503
    assert account not in caplog.text


def test_get_me_does_not_carry_the_preference():
    # Decided (docs/contrato-web-api.md): the preference has its own route, read by Ajustes.
    world = World()
    client = world.connect(optout="1")
    assert "commercial_communications" not in client.get(ME).json()
