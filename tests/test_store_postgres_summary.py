"""Integration: the panel summary and the sync jobs in PostgreSQL, with the API role.

Same environment as test_store_postgres. Only rows whose google_sub starts with
'pytest-' are created and removed; the worker role is needed for the last test only.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.adapters.crypto.sealed import generate_keypair
from app.adapters.google_oauth.fake import FakeGoogleOAuth
from app.adapters.google_profile.fake import FakeGoogleProfile
from app.adapters.places.fake import FakePlaces
from app.adapters.store.postgres import PostgresStore
from app.adapters.worker_store.postgres import PostgresWorkerStore
from app.api.main import create_app
from app.application.profile_sync import ProfileSync
from app.config import Settings
from app.domain.jobs import Job
from app.ports.google_oauth import Identity, ProfileLocation
from app.worker.sync_profile import SyncProfile

API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")
WORKER_URL = os.environ.get("FICHAITOR_TEST_WORKER_URL")

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL), reason="sin base de datos de pruebas configurada"
)

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
SHARED = ProfileLocation(
    "accounts/1", "locations/pytest-compartida", "Bar compartido", "place-pytest-9", True
)
OTHER = ProfileLocation("accounts/1", "locations/pytest-otra", "Otra ficha", "", False)


async def _owner(sql: str, *args):
    conn = await asyncpg.connect(OWNER_URL)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def _clean():
    mine = "(SELECT id FROM account WHERE google_sub LIKE 'pytest-%')"
    await _owner(
        f"UPDATE account SET selected_location_id = NULL WHERE id IN {mine}"  # noqa: S608
    )
    await _owner(f"DELETE FROM session WHERE account_id IN {mine}")  # noqa: S608
    await _owner(f"DELETE FROM location WHERE account_id IN {mine}")  # noqa: S608
    await _owner("DELETE FROM account WHERE google_sub LIKE 'pytest-%'")
    await _owner("DELETE FROM profile_summary WHERE google_sub LIKE 'pytest-%'")
    await _owner("DELETE FROM review WHERE google_sub LIKE 'pytest-%'")  # a sync stores them
    await _owner("DELETE FROM google_connection WHERE google_sub LIKE 'pytest-%'")
    await _owner("DELETE FROM job WHERE payload->>'google_sub' LIKE 'pytest-%'")


@pytest.fixture(autouse=True)
def clean():
    asyncio.run(_clean())
    yield
    asyncio.run(_clean())


def with_store(scenario):
    async def run():
        store = PostgresStore(API_URL)
        await store.connect()
        try:
            return await scenario(store)
        finally:
            await store.close()

    return asyncio.run(run())


async def _account_with(store, sub: str, locations, selected: ProfileLocation | None) -> str:
    account_id = await store.upsert_account(sub, f"{sub}@example.com")
    await store.sync_locations(account_id, locations, NOW)
    if selected is not None:
        rows = await _owner(
            "SELECT id FROM location WHERE account_id = $1::uuid AND gbp_location = $2",
            account_id,
            selected.location,
        )
        assert await store.select_location(account_id, rows[0]["id"])
    return account_id


async def _write_summary(sub: str, location: str, review_count: int, status: str = "ok"):
    await _owner(
        """
        INSERT INTO profile_summary
            (google_sub, location, status, status_at, synced_at, review_count, average_rating,
             unanswered_count, unanswered_low_count, last_review_at, has_regular_hours,
             upcoming_special_hours, google_changed, google_changed_fields)
        VALUES ($1, $2, $3, $4, $4, $5, 4.70, 3, 1, $6, true, 2, true, '{specialHours,title}')
        """,
        sub,
        location,
        status,
        NOW,
        review_count,
        NOW - timedelta(days=40),
    )


def test_selected_profile_is_none_without_a_selection_and_has_no_summary_until_written():
    async def scenario(store):
        account_id = await _account_with(store, "pytest-ana", [SHARED], None)
        before = await store.selected_profile(account_id)
        rows = await _owner("SELECT id FROM location WHERE account_id = $1::uuid", account_id)
        await store.select_location(account_id, rows[0]["id"])
        return before, await store.selected_profile(account_id)

    before, after = with_store(scenario)
    assert before is None
    assert (after.google_sub, after.location, after.summary) == (
        "pytest-ana",
        SHARED.location,
        None,
    )


def test_summary_columns_are_read_with_their_real_types():
    async def scenario(store):
        account_id = await _account_with(store, "pytest-ana", [SHARED], SHARED)
        await _write_summary("pytest-ana", SHARED.location, 13)
        return await store.selected_profile(account_id)

    summary = with_store(scenario).summary
    assert summary.status == "ok"
    assert summary.status_at == summary.synced_at == NOW
    assert summary.review_count == 13
    assert summary.average_rating == 4.7
    assert isinstance(summary.average_rating, float)
    assert not isinstance(summary.average_rating, Decimal)
    assert (summary.unanswered_count, summary.unanswered_low_count) == (3, 1)
    assert summary.last_review_at == NOW - timedelta(days=40)
    assert summary.has_regular_hours is True
    assert summary.upcoming_special_hours == 2
    assert summary.google_changed is True
    assert summary.google_changed_fields == ("specialHours", "title")


def test_a_status_without_figures_is_read_as_such():
    async def scenario(store):
        account_id = await _account_with(store, "pytest-ana", [SHARED], SHARED)
        await _owner(
            "INSERT INTO profile_summary (google_sub, location, status, status_at)"
            " VALUES ('pytest-ana', $1, 'needs_reconnect', $2)",
            SHARED.location,
            NOW,
        )
        return await store.selected_profile(account_id)

    summary = with_store(scenario).summary
    assert summary.status == "needs_reconnect"
    assert summary.synced_at is None
    assert summary.review_count is None
    assert summary.google_changed_fields == ()


def test_an_account_never_reads_the_summary_of_another_for_the_same_google_profile():
    async def scenario(store):
        ana = await _account_with(store, "pytest-ana", [SHARED, OTHER], SHARED)
        luis = await _account_with(store, "pytest-luis", [SHARED], SHARED)
        await _write_summary("pytest-ana", SHARED.location, 40)
        await _write_summary("pytest-ana", OTHER.location, 99)  # not the selected one
        only_ana = (await store.selected_profile(ana), await store.selected_profile(luis))
        await _write_summary("pytest-luis", SHARED.location, 7)
        both = (await store.selected_profile(ana), await store.selected_profile(luis))
        return only_ana, both

    only_ana, both = with_store(scenario)
    assert only_ana[0].summary.review_count == 40
    assert only_ana[1].google_sub == "pytest-luis"
    assert only_ana[1].summary is None
    assert [p.summary.review_count for p in both] == [40, 7]


def test_a_retired_profile_is_not_the_selected_one():
    async def scenario(store):
        account_id = await _account_with(store, "pytest-ana", [SHARED, OTHER], OTHER)
        await _write_summary("pytest-ana", OTHER.location, 5)
        await store.sync_locations(account_id, [SHARED], NOW)  # Google no longer lists it
        return await store.selected_profile(account_id)

    assert with_store(scenario) is None


def test_the_api_role_enqueues_a_sync_once_per_profile_and_window():
    clock = [1_800_000_000.0]

    async def scenario(store):
        account_id = await _account_with(store, "pytest-ana", [SHARED], SHARED)
        sync = ProfileSync(
            store, store, window_seconds=900, stale_after_seconds=6 * 3600, clock=lambda: clock[0]
        )
        await sync.request_for_account(account_id)
        clock[0] += 300
        await sync.request_for_account(account_id)
        assert (await sync.view(account_id)).summary is None  # asks again, same window
        first = await _owner(
            "SELECT kind, payload::text AS payload, state, idempotency_key FROM job"
            " WHERE payload->>'google_sub' = 'pytest-ana'"
        )
        clock[0] += 900
        await sync.request_for_account(account_id)
        second = await _owner(
            "SELECT count(*) AS n FROM job WHERE payload->>'google_sub' = 'pytest-ana'"
        )
        return first, second[0]["n"]

    first, total = with_store(scenario)
    assert len(first) == 1
    assert first[0]["kind"] == "sync_profile"
    assert '"location": "locations/pytest-compartida"' in first[0]["payload"]
    assert "pytest-ana" not in first[0]["idempotency_key"]
    assert total == 2


def test_the_api_role_still_cannot_read_jobs_nor_write_summaries():
    async def attempt(sql: str) -> type[Exception] | None:
        conn = await asyncpg.connect(API_URL)
        try:
            await conn.execute(sql)
        except asyncpg.InsufficientPrivilegeError as exc:
            return type(exc)
        finally:
            await conn.close()
        return None

    for sql in (
        "SELECT 1 FROM job LIMIT 1",
        "UPDATE job SET state = 'done' WHERE false",
        "DELETE FROM job WHERE false",
        "INSERT INTO profile_summary (google_sub, location, status)"
        " VALUES ('pytest-x', 'locations/pytest-x', 'ok')",
        "UPDATE profile_summary SET status = 'ok' WHERE false",
        "DELETE FROM profile_summary WHERE false",
        "SELECT 1 FROM google_connection LIMIT 1",
    ):
        assert asyncio.run(attempt(sql)) is asyncpg.InsufficientPrivilegeError, sql


class CapturedJobs:
    """Keeps the jobs out of the shared queue, so a worker running on the development
    database cannot take them; the test runs the handler itself."""

    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def enqueue(self, kind, payload, **_) -> bool:
        self.payloads.append({"kind": kind, **payload})
        return True


class NoFunnelStore(PostgresStore):
    """The real store, without the funnel: an event row has no mark that tells a test
    from a real visit ('connected' carries no source nor place), so it could never be
    cleaned up afterwards. Everything else is written to the database as usual."""

    def __init__(self, dsn: str) -> None:
        super().__init__(dsn)
        self.events: list[str] = []

    async def record_event(self, kind, source=None, place_id=None, score=None) -> None:
        self.events.append(kind)


@pytest.mark.skipif(not WORKER_URL, reason="sin usuario de worker de pruebas")
def test_whole_journey_api_writes_the_job_the_worker_handles_and_the_api_reads_its_summary():
    public, private = generate_keypair()
    google = FakeGoogleOAuth(
        locations=[ProfileLocation("accounts/pytest-1", "locations/pytest-e2e", "Bar", "", True)]
    )
    google.identity_value = Identity(sub="pytest-e2e", email="pytest-e2e@example.com")
    jobs = CapturedJobs()
    settings = Settings(
        _env_file=None,
        oauth_state_secret="secreto-de-pruebas",
        token_public_key=public,
        cookie_secure=False,
    )
    store = NoFunnelStore(API_URL)
    app = create_app(settings, FakePlaces(), store, google, jobs=jobs)

    async def run_worker(payload: dict) -> None:
        pool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=1)
        try:
            handler = SyncProfile(
                PostgresWorkerStore(pool),
                FakeGoogleProfile(changed_fields=("specialHours", "campoNuevo")),
                private,
            )
            await handler(Job("pytest-job", payload.pop("kind"), payload, 1, 5))
        finally:
            await pool.close()

    with TestClient(app, follow_redirects=False) as client:
        r = client.get("/api/v1/google/oauth/start")
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        r = client.get("/api/v1/google/oauth/callback", params={"code": "c", "state": state})
        assert r.headers["location"] == "/app?google=ok&fichas=1"
        assert client.get("/api/v1/me/summary").status_code == 409

        location_id = client.get("/api/v1/me").json()["locations"][0]["id"]
        r = client.post(
            "/api/v1/me/location",
            json={"location_id": location_id},
            headers={"X-Requested-With": "fichaitor"},
        )
        assert r.status_code == 200
        assert jobs.payloads[0] == {
            "kind": "sync_profile",
            "google_sub": "pytest-e2e",
            "location": "locations/pytest-e2e",
        }
        assert client.get("/api/v1/me/summary").json()["status"] == "pendiente"

        asyncio.run(run_worker(dict(jobs.payloads[0])))

        body = client.get("/api/v1/me/summary").json()
    assert body["status"] == "ok"
    assert body["synced_at"].endswith("Z")
    assert body["review_count"] == 13
    assert body["unanswered_count"] == 9
    assert body["unanswered_low_count"] == 2
    assert len(body["last_review_at"]) == 10
    assert body["has_regular_hours"] is True
    assert body["upcoming_special_hours"] == 1
    assert body["google_changed"] is True
    assert body["google_changed_fields"] == ["specialHours", "campoNuevo"]
    assert body["google_changed_labels"] == ["Horarios especiales", "campoNuevo"]
    assert body["stale"] is False
    # The connection was counted by the store of the test: no funnel row reached the
    # database, where it could not be told apart from a real one nor removed.
    assert store.events == ["connected"]


# --- facts of the profile (what the diagnosis is computed from) ---------------------


def test_selected_profile_carries_title_and_place_id_of_the_connection():
    async def scenario(store):
        account_id = await _account_with(store, "pytest-ana", [SHARED, OTHER], SHARED)
        first = await store.selected_profile(account_id)
        rows = await _owner(
            "SELECT id FROM location WHERE account_id = $1::uuid AND gbp_location = $2",
            account_id,
            OTHER.location,
        )
        await store.select_location(account_id, rows[0]["id"])
        return first, await store.selected_profile(account_id)

    first, second = with_store(scenario)
    assert (first.title, first.place_id) == ("Bar compartido", "place-pytest-9")
    assert (second.title, second.place_id) == ("Otra ficha", None)


def test_facts_written_by_the_worker_are_read_by_the_api_role():
    import json

    from app.domain.profile_summary import ProfileFacts, facts_to_json

    facts = ProfileFacts(
        title="Bar compartido",
        address="Calle Elvira 1, 18010 Granada",
        category="Bar",
        additional_categories=1,
        description_length=300,
        has_phone=True,
        has_website=False,
        service_count=2,
        place_id="place-pytest-9",
        special_dates=(NOW.date() + timedelta(days=3),),
        photo_count=7,
        latest_photo_at=NOW - timedelta(days=10),
        attribute_count=None,
    )

    async def scenario(store):
        account_id = await _account_with(store, "pytest-ana", [SHARED], SHARED)
        await _write_summary("pytest-ana", SHARED.location, 13)
        before = (await store.selected_profile(account_id)).summary
        await _owner(
            "UPDATE profile_summary SET facts = $1::jsonb WHERE google_sub = 'pytest-ana'",
            json.dumps(facts_to_json(facts)),
        )
        return before, (await store.selected_profile(account_id)).summary

    before, after = with_store(scenario)
    assert before.facts is None and before.review_count == 13  # a row from before the facts
    assert after.facts == facts
    assert after.review_count == 13


def test_facts_of_an_unknown_version_or_shape_are_read_as_missing():
    async def scenario(store):
        account_id = await _account_with(store, "pytest-ana", [SHARED], SHARED)
        await _write_summary("pytest-ana", SHARED.location, 13)
        seen = []
        for raw in ('{"v": 99}', '{"v": 1, "title": 7}', "{}"):
            await _owner(
                "UPDATE profile_summary SET facts = $1::jsonb WHERE google_sub = 'pytest-ana'",
                raw,
            )
            seen.append((await store.selected_profile(account_id)).summary)
        return seen

    for summary in with_store(scenario):
        assert summary.facts is None
        assert summary.status == "ok" and summary.review_count == 13


def test_facts_must_be_a_json_object():
    async def scenario(store):
        await _write_summary("pytest-ana", SHARED.location, 13)
        try:
            await _owner(
                "UPDATE profile_summary SET facts = '[1]'::jsonb WHERE google_sub = 'pytest-ana'"
            )
        except asyncpg.CheckViolationError:
            return True
        return False

    assert with_store(scenario)


def test_the_whole_diagnosis_over_http_reads_stored_facts_on_postgresql():
    """Connect, select and diagnose with the real store; the worker's write is simulated."""
    import json
    from urllib.parse import parse_qs, urlparse

    from fastapi.testclient import TestClient

    from app.adapters.crypto.sealed import generate_keypair
    from app.adapters.google_oauth.fake import FakeGoogleOAuth
    from app.api.main import create_app
    from app.config import Settings
    from app.domain.profile_summary import ProfileFacts, facts_to_json
    from app.ports.google_oauth import Identity

    class QuietStore(PostgresStore):
        async def record_event(self, *args, **kwargs) -> None:
            """Funnel rows of this test could not be told apart from real ones: skip them."""

        async def enqueue(self, *args, **kwargs) -> bool:
            """No job is left in the real queue: a worker may be running against it."""
            return True

    class NoPlaces:
        async def find_id(self, query):
            raise AssertionError("Google Places consultado")

        async def get(self, place_id):
            raise AssertionError("Google Places consultado")

    google = FakeGoogleOAuth(locations=[SHARED])
    google.identity_value = Identity(sub="pytest-ana", email="pytest-ana@example.com")
    settings = Settings(
        _env_file=None,
        oauth_state_secret="secreto-de-pruebas",
        token_public_key=generate_keypair()[0],
        cookie_secure=False,
    )
    app = create_app(settings, NoPlaces(), QuietStore(API_URL), google, holidays=())
    facts = ProfileFacts(
        address="Calle Elvira 1, 18010 Granada",
        category="Bar",
        additional_categories=1,
        description_length=300,
        has_phone=True,
        has_website=True,
        photo_count=30,
        attribute_count=3,
    )
    with TestClient(app, follow_redirects=False) as client:
        state = parse_qs(
            urlparse(client.get("/api/v1/google/oauth/start").headers["location"]).query
        )["state"][0]
        client.get("/api/v1/google/oauth/callback", params={"code": "c", "state": state})
        (location,) = client.get("/api/v1/me").json()["locations"]
        r = client.post(
            "/api/v1/me/location",
            json={"location_id": location["id"]},
            headers={"X-Requested-With": "fichaitor"},
        )
        assert r.status_code == 200
        r = client.get("/api/v1/me/diagnosis")
        assert (r.status_code, r.json()["status"]) == (404, "pendiente")

        asyncio.run(_write_summary("pytest-ana", SHARED.location, 13))
        asyncio.run(
            _owner(
                "UPDATE profile_summary SET facts = $1::jsonb, synced_at = now(), status_at = now()"
                " WHERE google_sub = 'pytest-ana'",
                json.dumps(facts_to_json(facts)),
            )
        )
        r = client.get("/api/v1/me/diagnosis")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["name"] == "Bar compartido"
        assert body["place_id"] == "place-pytest-9"
        assert body["attribution"] == "Datos de tu Perfil de Empresa de Google"
        assert [f["code"] for f in body["findings"]] == ["unanswered_reviews", "google_changed"]
        assert body["findings"][0]["title"] == "Tienes 3 reseñas sin responder"
        assert body["score"] == 77
