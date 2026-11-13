"""Integration: multinegocio, step 4 (docs/multinegocio.md, 13): the routes of the API
with PostgresStore and the API role, against the TEST database only. The database
decides (trigger location_active_guard): the limit, the free plan taken in another
account and the profile of another account.

Skipped unless the four URLs are set. They must point to a database whose name ends in
`_test` (tests/conftest.py stops the run otherwise). Rows are created for accounts whose
google_sub starts with 'pytest-p4-' and deleted at the end. Jobs go to a queue in
memory: nothing is enqueued in the database.
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.adapters.crypto.sealed import generate_keypair
from app.adapters.google_oauth.fake import FakeGoogleOAuth
from app.adapters.job_queue.memory import MemoryJobQueue
from app.adapters.places.fake import FakePlaces
from app.adapters.store.postgres import PostgresStore
from app.api.main import create_app
from app.api.session import SESSION_COOKIE
from app.config import Settings
from app.ports.google_oauth import Identity, ProfileLocation

API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")
WORKER_URL = os.environ.get("FICHAITOR_TEST_WORKER_URL")
CONVERSATION_URL = os.environ.get("FICHAITOR_TEST_CONVERSATION_URL")

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL and WORKER_URL and CONVERSATION_URL),
    reason="sin base de datos de pruebas configurada",
)

PREFIX = "pytest-p4-"
MINE = f"(SELECT id FROM account WHERE google_sub LIKE '{PREFIX}%')"
ME = "/api/v1/me"
SELECT = "/api/v1/me/location"
HEADER = {"X-Requested-With": "fichaitor"}
TODAY = datetime.now(UTC).date()


def run(coro):
    return asyncio.run(coro)


async def owner(sql: str, *args):
    conn = await asyncpg.connect(OWNER_URL)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def cleanup():
    for sql in (
        f"DELETE FROM conversation_lane WHERE account_id IN {MINE}",
        f"DELETE FROM conversation WHERE account_id IN {MINE}",
        f"DELETE FROM proposal WHERE account_id IN {MINE}",
        f"DELETE FROM profile_summary WHERE google_sub LIKE '{PREFIX}%'",
        f"DELETE FROM session WHERE account_id IN {MINE}",
        f"DELETE FROM google_connection WHERE google_sub LIKE '{PREFIX}%'",
        f"UPDATE account SET selected_location_id = NULL WHERE id IN {MINE}",
        f"DELETE FROM location_activation_log WHERE account_id IN {MINE}",
        f"DELETE FROM plan_entitlement WHERE account_id IN {MINE}",
        f"DELETE FROM location WHERE account_id IN {MINE}",
        f"DELETE FROM account WHERE id IN {MINE}",
    ):
        await owner(sql)


@pytest.fixture(autouse=True)
def clean():
    run(cleanup())
    yield
    run(cleanup())


class QuietStore(PostgresStore):
    async def record_event(self, *args, **kwargs) -> None:
        """Funnel rows of this test could not be told apart from real ones: skip them."""


def place(name: str) -> ProfileLocation:
    # A Google name of its own: the free plan of a Google profile is one in the database.
    return ProfileLocation("accounts/1", f"locations/{PREFIX}{name}", f"Bar {name}", "", True)


class Api:
    """The API on the test database, one browser at a time."""

    def __init__(self, client: TestClient, google: FakeGoogleOAuth) -> None:
        self.client = client
        self.google = google
        self.tokens: dict[str, str] = {}

    def connect(self, name: str, locations: list[ProfileLocation]) -> str:
        sub = f"{PREFIX}{name}-{uuid.uuid4().hex[:6]}"
        self.client.cookies.clear()
        self.google.identity_value = Identity(sub=sub, email=f"{name}@example.com")
        self.google.profile_locations = locations
        start = self.client.get("/api/v1/google/oauth/start").headers["location"]
        state = parse_qs(urlparse(start).query)["state"][0]
        r = self.client.get("/api/v1/google/oauth/callback", params={"code": "c", "state": state})
        assert r.status_code == 303
        self.tokens[name] = self.client.cookies.get(SESSION_COOKIE)
        return sub

    def as_(self, name: str) -> TestClient:
        self.client.cookies.clear()
        self.client.cookies.set(SESSION_COOKIE, self.tokens[name])
        return self.client

    def ids(self, name: str) -> list[str]:
        return [loc["id"] for loc in self.as_(name).get(ME).json()["locations"]]

    def activate(self, name: str, location_id: str, plan: str | None = None):
        body = {"plan": plan} if plan else {}
        return self.as_(name).post(
            f"/api/v1/me/locations/{location_id}/activation", json=body, headers=HEADER
        )

    def deactivate(self, name: str, location_id: str):
        return self.as_(name).delete(
            f"/api/v1/me/locations/{location_id}/activation", headers=HEADER
        )


@pytest.fixture
def api():
    google = FakeGoogleOAuth()
    settings = Settings(
        _env_file=None,
        oauth_state_secret="secreto-de-pruebas",
        token_public_key=generate_keypair()[0],
        cookie_secure=False,
    )
    app = create_app(settings, FakePlaces(), QuietStore(API_URL), google, jobs=MemoryJobQueue())
    # One client: the lifespan, and with it the connection pool, belongs to it.
    with TestClient(app, follow_redirects=False) as client:
        yield Api(client, google)


def by_id(body: dict) -> dict[str, dict]:
    return {loc["id"]: loc for loc in body["locations"]}


def test_activation_counts_in_the_database_up_to_the_limit(api):
    names = [f"{uuid.uuid4().hex[:6]}-{n}" for n in range(5)]
    api.connect("ana", [place(n) for n in names])
    ids = api.ids("ana")

    body = api.activate("ana", ids[0], "gratis").json()  # during the beta no plan is Pro
    assert by_id(body)[ids[0]]["plan"] == "gratis"
    assert by_id(body)[ids[0]]["status"] == "pendiente"
    assert by_id(body)[ids[0]]["pending"] == {"reviews": 0, "key_dates": 0}
    for location_id in ids[1:4]:
        assert by_id(api.activate("ana", location_id).json())[location_id]["plan"] == "pro"
    body = api.as_("ana").get(ME).json()
    assert body["plans"] == {
        "active": 4,
        "free_available": False,
        "available": {"pro": 0, "pro_whatsapp": 0},
    }
    assert body["pricing"]["total"] == 3500 + 2 * 3325
    assert body["pricing"]["charging"] is False

    r = api.activate("ana", ids[4])
    assert r.status_code == 402 and isinstance(r.json()["detail"], str)
    assert api.activate("ana", ids[4], "pro_whatsapp").status_code == 402
    assert by_id(api.as_("ana").get(ME).json())[ids[4]]["active"] is False

    # Free one up, and the fifth fits.
    assert api.deactivate("ana", ids[0]).status_code == 200
    assert by_id(api.activate("ana", ids[4]).json())[ids[4]]["plan"] == "gratis"
    log = run(
        owner(
            "SELECT event, plan FROM location_activation_log WHERE location_id = $1 ORDER BY id",
            ids[0],
        )
    )
    assert [(r["event"], r["plan"]) for r in log] == [
        ("activated", "gratis"),
        ("deactivated", "gratis"),  # the plan it had
    ]


def test_the_free_plan_of_a_google_profile_is_taken_once_across_accounts(api):
    shared = place(uuid.uuid4().hex[:6])
    api.connect("ana", [shared])
    api.connect("luis", [shared])
    (ana_bar,), (luis_bar,) = api.ids("ana"), api.ids("luis")
    assert api.activate("ana", ana_bar, "gratis").status_code == 200
    r = api.activate("luis", luis_bar, "gratis")
    assert r.status_code == 409
    assert r.json()["code"] == "free_taken" and isinstance(r.json()["detail"], str)
    assert by_id(api.activate("luis", luis_bar).json())[luis_bar]["plan"] == "pro"


def test_another_accounts_profile_is_404_on_every_route(api):
    api.connect("ana", [place(uuid.uuid4().hex[:6])])
    api.connect("luis", [place(uuid.uuid4().hex[:6])])
    (ana_bar,), (luis_bar,) = api.ids("ana"), api.ids("luis")
    assert api.activate("luis", luis_bar).status_code == 200
    not_found = {"detail": "Esa ficha no está en tu cuenta."}
    for r in (
        api.activate("ana", luis_bar),
        api.deactivate("ana", luis_bar),
        api.as_("ana").get("/api/v1/me/summary", params={"location": luis_bar}),
        api.as_("ana").get("/api/v1/me/reviews", params={"location": luis_bar}),
        api.as_("ana").get("/api/v1/me/key-dates", params={"location": luis_bar}),
        api.as_("ana").get("/api/v1/me/sentiment", params={"location": luis_bar}),
        api.as_("ana").get("/api/v1/me/diagnosis", params={"location": luis_bar}),
        api.as_("ana").get("/api/v1/me/summary", params={"location": "loc_noexiste"}),
    ):
        assert (r.status_code, r.json()) == (404, not_found)
    assert by_id(api.as_("luis").get(ME).json())[luis_bar]["active"] is True
    r = api.as_("ana").get("/api/v1/me/summary", params={"location": ana_bar})
    assert (r.status_code, r.json()) == (409, {"detail": "Esa ficha no está activa."})


def test_reads_name_their_profile_and_deactivating_moves_the_preference(api):
    names = sorted(uuid.uuid4().hex[:6] for _ in range(2))  # listed by title
    sub = api.connect("ana", [place(n) for n in names])
    bar, shop = api.ids("ana")
    client = api.as_("ana")
    assert client.post(SELECT, json={"location_id": shop}, headers=HEADER).status_code == 409
    assert api.activate("ana", shop).status_code == 200
    assert client.post(SELECT, json={"location_id": shop}, headers=HEADER).status_code == 200
    assert api.activate("ana", bar).status_code == 200
    run(
        owner(
            "INSERT INTO profile_summary (google_sub, location, status, unanswered_count)"
            " VALUES ($1, $2, 'ok', 4)",
            sub,
            f"locations/{PREFIX}{names[0]}",
        )
    )

    body = api.as_("ana").get(ME).json()
    assert body["selected_location_id"] == shop
    assert by_id(body)[bar]["status"] == "ok"
    assert by_id(body)[bar]["pending"] == {"reviews": 4, "key_dates": 0}
    client = api.as_("ana")
    assert client.get("/api/v1/me/summary").json()["location_id"] == shop
    r = client.get("/api/v1/me/summary", params={"location": bar})
    assert (r.json()["location_id"], r.json()["status"], r.json()["unanswered_count"]) == (
        bar,
        "ok",
        4,
    )
    r = client.get("/api/v1/me/reviews", params={"location": bar})
    assert r.status_code == 200 and r.json()["location_id"] == bar
    r = client.get("/api/v1/me/key-dates", params={"location": bar})
    assert r.json() == {"dates": [], "location_id": bar}

    body = api.deactivate("ana", shop).json()
    assert body["selected_location_id"] == bar
    assert body["plans"]["active"] == 1
    body = api.deactivate("ana", bar).json()
    assert body["selected_location_id"] is None and body["plans"]["active"] == 0
    r = api.as_("ana").get("/api/v1/me/summary")
    assert r.status_code == 409


def test_a_key_date_is_counted_and_answered_for_its_own_profile(api):
    names = sorted(uuid.uuid4().hex[:6] for _ in range(2))  # listed by title
    api.connect("ana", [place(n) for n in names])
    bar, shop = api.ids("ana")
    for location_id in (bar, shop):
        api.activate("ana", location_id)
    rows = run(owner(f"SELECT id FROM account WHERE id IN {MINE}"))
    (account_id,) = [str(r["id"]) for r in rows]
    day = (TODAY + timedelta(days=20)).isoformat()
    pid = f"prop_{uuid.uuid4().hex[:16]}"
    run(
        owner(
            "INSERT INTO proposal (id, account_id, type, source_ref, state, payload, step,"
            " step_token, location_id) VALUES ($1, $2::uuid, 'key_date', $3, 'enviada',"
            " $4::jsonb, 'holiday.ask', 'abc123', $5)",
            pid,
            account_id,
            f"{shop}:{day}",
            f'{{"kind": "holiday", "date": "{day}", "name": "Fiesta", "location_id": "{shop}"}}',
            shop,
        )
    )
    body = api.as_("ana").get(ME).json()
    assert by_id(body)[shop]["pending"]["key_dates"] == 1
    assert by_id(body)[bar]["pending"]["key_dates"] == 0
    listed = api.as_("ana").get("/api/v1/me/key-dates", params={"location": shop}).json()
    assert [d["id"] for d in listed["dates"]] == [pid]
    assert listed["dates"][0]["location_id"] == shop

    # Deactivating cancels it: it is neither counted nor answered.
    api.deactivate("ana", shop)
    r = api.as_("ana").post(f"/api/v1/me/key-dates/{pid}/dismiss", headers=HEADER)
    assert r.status_code == 404
    (state,) = run(owner("SELECT state, outcome_reason FROM proposal WHERE id = $1", pid))
    assert (state["state"], state["outcome_reason"]) == ("cancelada", "location_inactive")
