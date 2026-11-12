"""Accounts, sessions and the routes behind a session. No database: MemoryStore."""

import hashlib
import logging
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.adapters.crypto.sealed import generate_keypair
from app.adapters.google_oauth.fake import FakeGoogleOAuth
from app.adapters.places.fake import DEMO, FakePlaces
from app.adapters.store.memory import MemoryStore
from app.api.main import create_app
from app.api.session import SESSION_COOKIE
from app.config import Settings
from app.ports.google_oauth import Identity, ProfileLocation

START = "/api/v1/google/oauth/start"
CALLBACK = "/api/v1/google/oauth/callback"
ME = "/api/v1/me"
SELECT = "/api/v1/me/location"
DIAGNOSIS = "/api/v1/me/diagnosis"
LOGOUT = "/api/v1/auth/logout"
HEADER = {"X-Requested-With": "fichaitor"}
PUBLIC, _ = generate_keypair()
T0 = 1_800_000_000.0
DAY = 86400
HOST_SESSION = "__Host-fichaitor_session="

LA_PARRA = ProfileLocation(
    "accounts/1", "locations/1", "Bar La Parra (ejemplo)", DEMO.place_id, True
)

NO_PLACE = ProfileLocation("accounts/1", "locations/2", "Demo sin identificador", "", False)
UNKNOWN = ProfileLocation("accounts/1", "locations/3", "Negocio fantasma", "", False)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


class World:
    def __init__(self, locations=None, places=None, google=None, **overrides) -> None:
        self.google = google or FakeGoogleOAuth(locations=locations)
        self.places = places or FakePlaces()
        self.store = MemoryStore()
        self.clock = Clock()
        settings = Settings(
            _env_file=None,
            oauth_state_secret="secreto-de-pruebas",
            token_public_key=PUBLIC,
            **{"cookie_secure": False, **overrides},
        )
        self.app = create_app(settings, self.places, self.store, self.google, clock=self.clock)

    def browser(self) -> TestClient:
        return TestClient(self.app, follow_redirects=False)

    def connect(self, client: TestClient | None = None):
        client = client or self.browser()
        r = client.get(START)
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        r = client.get(CALLBACK, params={"code": "c", "state": state})
        assert r.status_code == 303
        return client, r

    def connect_as(self, sub: str, email: str, locations: list[ProfileLocation]) -> TestClient:
        self.google.identity_value = Identity(sub=sub, email=email)
        self.google.profile_locations = locations
        return self.connect()[0]


def location_ids(client: TestClient) -> list[str]:
    return [loc["id"] for loc in client.get(ME).json()["locations"]]


# --- session cookie ---------------------------------------------------------


def test_me_without_session_is_401_in_spanish():
    client = World().browser()
    r = client.get(ME)
    assert r.status_code == 401
    assert isinstance(r.json()["detail"], str)
    client.cookies.set(SESSION_COOKIE, "token-inventado")
    assert client.get(ME).status_code == 401
    assert client.get(DIAGNOSIS).status_code == 401
    assert client.post(SELECT, json={"location_id": "loc_x"}, headers=HEADER).status_code == 401


def test_connecting_sets_the_session_cookie_and_returns_to_app_by_default():
    _, r = World().connect()
    assert r.headers["location"] == "/app?google=ok&fichas=1"
    cookie = next(c for c in r.headers.get_list("set-cookie") if c.startswith(SESSION_COOKIE))
    lowered = cookie.lower()
    assert "httponly" in lowered
    assert "samesite=lax" in lowered
    assert "path=/;" in lowered or lowered.endswith("path=/")
    assert f"max-age={30 * DAY}" in lowered
    assert "secure" not in lowered

    world = World(cookie_secure=True)
    https = TestClient(world.app, base_url="https://testserver", follow_redirects=False)
    _, r = world.connect(https)
    cookie = next(c for c in r.headers.get_list("set-cookie") if c.startswith(HOST_SESSION))
    assert "; secure" in cookie.lower()


def test_over_https_the_cookies_carry_the_host_and_secure_prefixes():
    # B6 (docs/seguridad/revision-2026-11-12.md): another site under fichaitor.es cannot
    # plant either cookie for the main domain.
    world = World(cookie_secure=True)
    https = TestClient(world.app, base_url="https://testserver", follow_redirects=False)
    state_cookie = https.get(START).headers["set-cookie"]
    assert state_cookie.startswith("__Secure-fichaitor_oauth_state=")
    assert "; secure" in state_cookie.lower()

    _, r = world.connect(https)
    cookie = next(c for c in r.headers.get_list("set-cookie") if c.startswith(HOST_SESSION))
    lowered = cookie.lower()
    assert "; secure" in lowered and "path=/" in lowered and "domain=" not in lowered
    assert not any(c.startswith(f"{SESSION_COOKIE}=") for c in r.headers.get_list("set-cookie"))
    assert https.get(ME).status_code == 200

    # The same token under the name without the prefix, as a subdomain could set it,
    # opens nothing.
    token = https.cookies.get(HOST_SESSION.rstrip("="))
    planted = TestClient(world.app, base_url="https://testserver")
    planted.cookies.set(SESSION_COOKIE, token)
    assert planted.get(ME).status_code == 401

    out = https.post(LOGOUT, headers=HEADER)
    assert any(c.startswith(f'{HOST_SESSION}""') for c in out.headers.get_list("set-cookie"))
    assert https.get(ME).status_code == 401


def test_failed_connections_do_not_open_a_session():
    for google in (
        FakeGoogleOAuth(scopes=("openid", "email")),
        FakeGoogleOAuth(refresh_token=None),
        FakeGoogleOAuth(fail_exchange=True),
    ):
        world = World(google=google)
        client, r = world.connect()
        assert "google=ok" not in r.headers["location"]
        assert SESSION_COOKIE not in r.headers.get("set-cookie", "")
        assert world.store.sessions == {}
        assert client.get(ME).status_code == 401


def test_only_the_hash_of_the_token_is_stored():
    world = World()
    client, _ = world.connect()
    token = client.cookies.get(SESSION_COOKIE)
    assert len(token) >= 43  # 32 random bytes, urlsafe
    assert list(world.store.sessions) == [hashlib.sha256(token.encode()).hexdigest()]
    assert token not in repr(world.store.__dict__)


def test_tokens_and_emails_never_reach_the_logs(caplog):
    caplog.set_level(logging.DEBUG)
    world = World()
    client, _ = world.connect()
    token = client.cookies.get(SESSION_COOKIE)
    client.get(ME)
    client.post(SELECT, json={"location_id": "loc_otra"}, headers=HEADER)
    client.post(SELECT, json={"location_id": location_ids(client)[0]}, headers=HEADER)
    client.get(DIAGNOSIS)
    client.post(LOGOUT, headers=HEADER)
    client.cookies.set(SESSION_COOKIE, token)
    client.get(ME)
    logged = caplog.text + "".join(str(r.args) for r in caplog.records)
    assert logged  # something was logged (httpx request lines at least)
    for secret in (token, "dueno@example.com", "fake-refresh-token", "fake-access-token"):
        assert secret not in logged


def test_expired_session_is_401():
    world = World()
    client, _ = world.connect()
    world.clock.now = T0 + 30 * DAY - 1
    assert client.get(ME).status_code == 200
    world.clock.now = T0 + 30 * DAY
    assert client.get(ME).status_code == 401
    assert client.get(DIAGNOSIS).status_code == 401


def test_connecting_again_revokes_the_previous_session_of_that_browser():
    world = World()
    client, _ = world.connect()
    old = client.cookies.get(SESSION_COOKIE)
    world.connect(client)
    new = client.cookies.get(SESSION_COOKIE)
    assert new != old
    assert client.get(ME).status_code == 200
    other = world.browser()
    other.cookies.set(SESSION_COOKIE, old)
    assert other.get(ME).status_code == 401


# --- GET /me ----------------------------------------------------------------


def test_me_has_the_shape_of_the_contract():
    client, _ = World(locations=[LA_PARRA, NO_PLACE]).connect()
    r = client.get(ME)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "email",
        "locations",
        "selected_location_id",
        "connected",
        "pending_deletion",
    }
    assert body["connected"] is True and body["pending_deletion"] is None
    assert body["email"] == "dueno@example.com"
    assert body["selected_location_id"] is None
    first, second = body["locations"]
    assert set(first) == {"id", "title", "place_id", "verified"}
    assert first["id"].startswith("loc_") and first["id"] != second["id"]
    assert (first["title"], first["place_id"], first["verified"]) == (
        "Bar La Parra (ejemplo)",
        DEMO.place_id,
        True,
    )
    assert second["place_id"] is None and second["verified"] is False


def test_reconnecting_keeps_the_account_and_refreshes_its_locations():
    world = World(locations=[LA_PARRA, NO_PLACE])
    client, _ = world.connect()
    kept, gone = location_ids(client)
    assert client.post(SELECT, json={"location_id": kept}, headers=HEADER).status_code == 200

    renamed = ProfileLocation("accounts/1", "locations/1", "Bar La Parra", DEMO.place_id, True)
    world.google.profile_locations = [renamed, UNKNOWN]
    world.connect(client)
    body = client.get(ME).json()
    assert len(world.store.accounts) == 1
    assert body["selected_location_id"] == kept
    assert [loc["title"] for loc in body["locations"]] == ["Bar La Parra", "Negocio fantasma"]
    assert body["locations"][0]["id"] == kept  # same profile, same identifier
    assert gone not in [loc["id"] for loc in body["locations"]]


def test_a_selected_location_that_google_no_longer_lists_is_deselected():
    world = World(locations=[LA_PARRA, NO_PLACE])
    client, _ = world.connect()
    _, second = location_ids(client)
    client.post(SELECT, json={"location_id": second}, headers=HEADER)
    world.google.profile_locations = [LA_PARRA]
    world.connect(client)
    assert client.get(ME).json()["selected_location_id"] is None
    assert client.post(SELECT, json={"location_id": second}, headers=HEADER).status_code == 404


def test_locations_are_kept_when_google_cannot_list_them():
    world = World(locations=[LA_PARRA, NO_PLACE])
    client, _ = world.connect()
    before = client.get(ME).json()
    world.google.fail_locations = True
    _, r = world.connect(client)
    assert r.headers["location"] == "/app?google=ok&fichas=pendiente"
    assert client.get(ME).json() == before


# --- isolation between accounts --------------------------------------------


def test_an_account_never_sees_or_selects_locations_of_another_account():
    world = World()
    ana = world.connect_as("sub-ana", "ana@example.com", [LA_PARRA])
    luis = world.connect_as("sub-luis", "luis@example.com", [NO_PLACE, UNKNOWN])

    ana_body, luis_body = ana.get(ME).json(), luis.get(ME).json()
    assert ana_body["email"] == "ana@example.com"
    assert [loc["title"] for loc in ana_body["locations"]] == [LA_PARRA.title]
    assert [loc["title"] for loc in luis_body["locations"]] == [NO_PLACE.title, UNKNOWN.title]
    ana_ids = {loc["id"] for loc in ana_body["locations"]}
    luis_ids = {loc["id"] for loc in luis_body["locations"]}
    assert not ana_ids & luis_ids

    for stolen in luis_ids:
        r = ana.post(SELECT, json={"location_id": stolen}, headers=HEADER)
        assert r.status_code == 404
        assert isinstance(r.json()["detail"], str)
    assert ana.get(ME).json()["selected_location_id"] is None
    assert ana.get(DIAGNOSIS).status_code == 409
    assert luis.get(ME).json()["selected_location_id"] is None


def test_same_google_profile_in_two_accounts_gets_two_identifiers():
    world = World()
    ana = world.connect_as("sub-ana", "ana@example.com", [LA_PARRA])
    luis = world.connect_as("sub-luis", "luis@example.com", [LA_PARRA])
    (ana_id,), (luis_id,) = location_ids(ana), location_ids(luis)
    assert ana_id != luis_id
    assert luis.post(SELECT, json={"location_id": ana_id}, headers=HEADER).status_code == 404


# --- POST /me/location ------------------------------------------------------


def test_selecting_a_location_returns_me_and_requires_the_header():
    client, _ = World(locations=[LA_PARRA, NO_PLACE]).connect()
    first, second = location_ids(client)

    assert client.post(SELECT, json={"location_id": first}).status_code == 403
    r = client.post(SELECT, json={"location_id": first}, headers={"X-Requested-With": "otro"})
    assert r.status_code == 403
    assert isinstance(r.json()["detail"], str)
    assert client.get(ME).json()["selected_location_id"] is None

    r = client.post(SELECT, json={"location_id": second}, headers=HEADER)
    assert r.status_code == 200
    assert r.json() == client.get(ME).json()
    assert r.json()["selected_location_id"] == second

    assert (
        client.post(SELECT, json={"location_id": "loc_noexiste"}, headers=HEADER).status_code == 404
    )
    assert client.post(SELECT, json={}, headers=HEADER).status_code == 422
    assert client.get(ME).json()["selected_location_id"] == second


# --- GET /me/diagnosis ------------------------------------------------------


def select(client: TestClient, index: int = 0) -> None:
    location_id = location_ids(client)[index]
    assert client.post(SELECT, json={"location_id": location_id}, headers=HEADER).status_code == 200


def test_diagnosis_without_a_selected_location_is_409():
    client, _ = World().connect()
    r = client.get(DIAGNOSIS)
    assert r.status_code == 409
    assert isinstance(r.json()["detail"], str)


def test_diagnosis_never_asks_google_places_and_is_pending_until_the_worker_syncs():
    # The diagnosis itself is covered in tests/test_me_diagnosis.py.
    world = World(places=FakePlaces(fail=True), diagnosis_rate_limit=1)
    client, _ = world.connect()
    select(client)
    for _ in range(3):  # no limit per account any more
        r = client.get(DIAGNOSIS)
        assert r.status_code == 404
        assert r.json()["status"] == "pendiente"
        assert isinstance(r.json()["detail"], str) and r.json()["detail"]
    assert world.places.calls == 0 and world.places.id_calls == []


# --- POST /auth/logout ------------------------------------------------------


def test_logout_revokes_the_session_and_clears_the_cookie():
    world = World()
    client, _ = world.connect()
    token = client.cookies.get(SESSION_COOKIE)

    assert client.post(LOGOUT).status_code == 403
    assert client.get(ME).status_code == 200

    r = client.post(LOGOUT, headers=HEADER)
    assert r.status_code == 204
    assert r.content == b""
    cleared = r.headers["set-cookie"].lower()
    assert cleared.startswith(f'{SESSION_COOKIE}=""') and "max-age=0" in cleared
    assert "path=/" in cleared and "httponly" in cleared
    assert client.cookies.get(SESSION_COOKIE) is None

    client.cookies.set(SESSION_COOKIE, token)  # a stolen copy of the cookie
    assert client.get(ME).status_code == 401
    assert client.post(SELECT, json={"location_id": "loc_x"}, headers=HEADER).status_code == 401
    (session,) = world.store.sessions.values()
    assert session.revoked_at is not None  # kept for audit, not deleted


def test_logout_without_a_session_is_still_204():
    client = World().browser()
    assert client.post(LOGOUT, headers=HEADER).status_code == 204
    assert client.post(LOGOUT).status_code == 403


def test_session_routes_exist_even_without_google_configured():
    app = create_app(Settings(_env_file=None), FakePlaces(), MemoryStore())
    assert TestClient(app).get(ME).status_code == 401
