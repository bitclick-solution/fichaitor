from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.adapters.crypto.sealed import generate_keypair, seal, unseal
from app.adapters.google_oauth.fake import FakeGoogleOAuth
from app.adapters.google_oauth.real import RealGoogleOAuth
from app.adapters.places.fake import FakePlaces
from app.adapters.store.memory import MemoryStore
from app.api.google_oauth import STATE_COOKIE
from app.api.main import create_app
from app.config import Settings
from app.domain.oauth_state import InvalidState, make_state, verify_state

START = "/api/v1/google/oauth/start"
CALLBACK = "/api/v1/google/oauth/callback"
SECRET = "secreto-de-pruebas"
PUBLIC, PRIVATE = generate_keypair()


def make(google: FakeGoogleOAuth | None = None, store: MemoryStore | None = None, **overrides):
    google = google or FakeGoogleOAuth()
    store = store or MemoryStore()
    settings = Settings(
        _env_file=None,
        oauth_state_secret=SECRET,
        token_public_key=PUBLIC,
        post_connect_url="/conectar",
        cookie_secure=False,
        **overrides,
    )
    client = TestClient(create_app(settings, FakePlaces(), store, google), follow_redirects=False)
    return client, store, google


def begin(client: TestClient) -> str:
    r = client.get(START)
    assert r.status_code == 302
    return parse_qs(urlparse(r.headers["location"]).query)["state"][0]


def result_of(response) -> dict[str, str]:
    assert response.status_code == 303
    return {k: v[0] for k, v in parse_qs(urlparse(response.headers["location"]).query).items()}


# --- state ------------------------------------------------------------------


def test_state_accepts_only_matching_signed_and_fresh_values():
    state = make_state(SECRET, 1000)
    verify_state(SECRET, state, state, 1300)
    for bad_state, cookie, now in [
        (state, None, 1300),  # no cookie: another browser
        (state, make_state(SECRET, 1000), 1300),  # cookie from another attempt
        (state, state, 1000 + 601),  # expired
        (state, state, 900),  # issued in the future
        ("1000.abc.firma-falsa", "1000.abc.firma-falsa", 1300),
        ("", "", 1300),
    ]:
        with pytest.raises(InvalidState):
            verify_state(SECRET, bad_state, cookie, now)
    forged = make_state("otro-secreto", 1000)
    with pytest.raises(InvalidState):
        verify_state(SECRET, forged, forged, 1300)


# --- encryption -------------------------------------------------------------


def test_refresh_token_is_sealed_and_only_the_private_key_opens_it():
    sealed = seal(PUBLIC, "token-secreto")
    assert "token-secreto" not in sealed
    assert seal(PUBLIC, "token-secreto") != sealed  # not deterministic
    assert unseal(PRIVATE, sealed) == "token-secreto"
    _, other_private = generate_keypair()
    with pytest.raises(Exception):  # noqa: B017
        unseal(other_private, sealed)


# --- flow -------------------------------------------------------------------


def test_start_redirects_to_google_and_sets_httponly_state_cookie():
    client, _, _ = make()
    r = client.get(START)
    assert r.status_code == 302
    assert r.headers["location"].startswith("https://accounts.google.test/auth")
    cookie = r.headers["set-cookie"]
    assert STATE_COOKIE in cookie
    assert "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower()
    assert "Path=/api/v1/google/oauth" in cookie


def test_real_authorization_url_asks_for_offline_access_and_consent():
    url = RealGoogleOAuth("cid", "secret", "https://fichaitor.es/cb").authorization_url("st")
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["client_id"] == "cid"
    assert q["redirect_uri"] == "https://fichaitor.es/cb"
    assert q["access_type"] == "offline"
    assert q["prompt"] == "consent"
    assert "business.manage" in q["scope"]
    assert q["state"] == "st"
    assert "secret" not in url


def test_successful_connection_stores_encrypted_token_and_locations():
    client, store, google = make()
    state = begin(client)
    r = client.get(CALLBACK, params={"code": "codigo-1", "state": state})
    assert result_of(r) == {"google": "ok", "fichas": "1"}
    assert google.exchanged == ["codigo-1"]
    (conn,) = store.connections
    assert conn.google_sub == "google-sub-1"
    assert conn.refresh_token_enc != "fake-refresh-token"
    assert unseal(PRIVATE, conn.refresh_token_enc) == "fake-refresh-token"
    assert conn.locations[0]["title"].startswith("Bar La Parra")
    assert ("connected", None, None, None) in store.events
    assert "fake-refresh-token" not in r.headers["location"]


def test_the_funnel_counts_the_first_connection_not_every_sign_in():
    client, store, _ = make()
    for code in ("codigo-1", "codigo-2"):
        state = begin(client)
        r = client.get(CALLBACK, params={"code": code, "state": state})
        assert result_of(r)["google"] == "ok"
    assert [e for e in store.events if e[0] == "connected"] == [("connected", None, None, None)]


def test_callback_without_valid_state_is_rejected_and_never_calls_google():
    client, store, google = make()
    begin(client)
    assert client.get(CALLBACK, params={"code": "c", "state": "falso"}).status_code == 400
    other = TestClient(client.app, follow_redirects=False)  # no cookie: another browser
    state = make_state(SECRET, __import__("time").time())
    assert other.get(CALLBACK, params={"code": "c", "state": state}).status_code == 400
    assert google.exchanged == []
    assert store.connections == []


def test_user_cancelling_returns_to_the_app_without_storing_anything():
    client, store, google = make()
    state = begin(client)
    r = client.get(CALLBACK, params={"error": "access_denied", "state": state})
    assert result_of(r) == {"google": "cancelado"}
    assert google.exchanged == []
    assert store.connections == []


def test_connection_without_the_business_scope_is_not_stored():
    client, store, _ = make(FakeGoogleOAuth(scopes=("openid", "email")))
    state = begin(client)
    r = client.get(CALLBACK, params={"code": "c", "state": state})
    assert result_of(r) == {"google": "sin_permiso"}
    assert store.connections == []


def test_missing_refresh_token_or_failed_exchange_is_an_error_without_leaks():
    for google in (FakeGoogleOAuth(refresh_token=None), FakeGoogleOAuth(fail_exchange=True)):
        client, store, _ = make(google)
        state = begin(client)
        r = client.get(CALLBACK, params={"code": "c", "state": state})
        assert result_of(r) == {"google": "error"}
        assert store.connections == []


def test_connection_is_kept_when_profiles_cannot_be_listed():
    client, store, _ = make(FakeGoogleOAuth(fail_locations=True))
    state = begin(client)
    r = client.get(CALLBACK, params={"code": "c", "state": state})
    assert result_of(r) == {"google": "ok", "fichas": "pendiente"}
    assert len(store.connections) == 1


def test_store_failure_is_reported_as_error():
    client, _, _ = make(store=MemoryStore(fail=True))
    state = begin(client)
    r = client.get(CALLBACK, params={"code": "c", "state": state})
    assert result_of(r) == {"google": "error"}


def test_state_cannot_be_replayed_after_use():
    client, store, _ = make()
    state = begin(client)
    client.get(CALLBACK, params={"code": "c1", "state": state})
    assert client.get(CALLBACK, params={"code": "c2", "state": state}).status_code == 400
    assert len(store.connections) == 1


def test_routes_do_not_exist_when_google_is_not_configured():
    app = create_app(Settings(_env_file=None), FakePlaces(), MemoryStore())
    assert TestClient(app).get(START, follow_redirects=False).status_code == 404


def test_app_refuses_to_start_with_google_but_without_keys():
    with pytest.raises(RuntimeError):
        create_app(Settings(_env_file=None, fake_google=True), FakePlaces(), MemoryStore())
    with pytest.raises(RuntimeError):
        create_app(
            Settings(
                _env_file=None,
                google_client_id="cid",
                oauth_state_secret="s",
                token_public_key=PUBLIC,
            ),
            FakePlaces(),
            MemoryStore(),
        )
