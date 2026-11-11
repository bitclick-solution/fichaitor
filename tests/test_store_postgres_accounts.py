"""Integration: accounts, profiles and sessions in PostgreSQL. Same environment as
test_store_postgres. Only rows whose google_sub starts with 'pytest-' are created and removed.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from app.adapters.store.postgres import PostgresStore
from app.domain.session import hash_token, new_token
from app.ports.google_oauth import ProfileLocation

API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL), reason="sin base de datos de pruebas configurada"
)

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=30)
BAR = ProfileLocation("accounts/1", "locations/pytest-1", "Bar de pruebas", "place-pytest-1", True)
SHOP = ProfileLocation("accounts/1", "locations/pytest-2", "Tienda de pruebas", "", False)
CAFE = ProfileLocation(
    "accounts/2", "locations/pytest-3", "Café de pruebas", "place-pytest-3", True
)


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


async def _api_execute(sql: str, *args):
    conn = await asyncpg.connect(API_URL)
    try:
        return await conn.execute(sql, *args)
    finally:
        await conn.close()


def test_account_is_created_once_per_google_identity_and_its_email_refreshed():
    async def scenario(store):
        first = await store.upsert_account("pytest-ana", "ana@example.com")
        again = await store.upsert_account("pytest-ana", "ana.nueva@example.com")
        other = await store.upsert_account("pytest-luis", "luis@example.com")
        return first, again, other

    first, again, other = with_store(scenario)
    assert first == again != other
    rows = asyncio.run(_owner("SELECT email FROM account WHERE google_sub = 'pytest-ana'"))
    assert [r["email"] for r in rows] == ["ana.nueva@example.com"]


def test_locations_are_synced_keep_their_id_and_are_retired_not_deleted():
    async def scenario(store):
        account = await store.upsert_account("pytest-ana", "ana@example.com")
        await store.sync_locations(account, [BAR, SHOP], NOW)
        before = await store.list_locations(account)
        assert await store.select_location(account, before[1].id)
        renamed = ProfileLocation(BAR.account, BAR.location, "Bar renombrado", BAR.place_id, False)
        await store.sync_locations(account, [renamed], LATER)
        after = await store.list_locations(account)
        await store.create_session(account, "h" * 64, NOW, LATER)
        selected = (await store.account_for_session("h" * 64, NOW)).selected_location_id
        await store.sync_locations(account, [renamed, SHOP], LATER)
        back = await store.list_locations(account)
        return before, after, selected, back

    before, after, selected, back = with_store(scenario)
    assert [(loc.title, loc.place_id, loc.verified) for loc in before] == [
        ("Bar de pruebas", "place-pytest-1", True),
        ("Tienda de pruebas", None, False),
    ]
    assert all(loc.id.startswith("loc_") and len(loc.id) == 16 for loc in before)
    assert [(loc.id, loc.title, loc.verified) for loc in after] == [
        (before[0].id, "Bar renombrado", False)
    ]
    assert selected is None  # the selected profile was retired
    assert [loc.id for loc in back] == [before[0].id, before[1].id]  # it comes back with its id
    rows = asyncio.run(
        _owner(
            "SELECT count(*) AS n FROM location l JOIN account a ON a.id = l.account_id "
            "WHERE a.google_sub = 'pytest-ana'"
        )
    )
    assert rows[0]["n"] == 2


def test_an_account_cannot_list_or_select_a_location_of_another_account():
    async def scenario(store):
        ana = await store.upsert_account("pytest-ana", "ana@example.com")
        luis = await store.upsert_account("pytest-luis", "luis@example.com")
        await store.sync_locations(ana, [BAR, SHOP], NOW)
        await store.sync_locations(luis, [CAFE, BAR], NOW)  # BAR is managed by both
        ana_locs = await store.list_locations(ana)
        luis_locs = await store.list_locations(luis)
        stolen = [await store.select_location(ana, loc.id) for loc in luis_locs]
        unknown = await store.select_location(ana, "loc_noexiste")
        own = await store.select_location(luis, luis_locs[0].id)
        return ana, luis, ana_locs, luis_locs, stolen, unknown, own

    ana, luis, ana_locs, luis_locs, stolen, unknown, own = with_store(scenario)
    assert [loc.title for loc in ana_locs] == ["Bar de pruebas", "Tienda de pruebas"]
    assert [loc.title for loc in luis_locs] == ["Bar de pruebas", "Café de pruebas"]
    assert not {loc.id for loc in ana_locs} & {loc.id for loc in luis_locs}
    assert stolen == [False, False]
    assert unknown is False
    assert own is True
    rows = asyncio.run(
        _owner(
            "SELECT google_sub, selected_location_id FROM account "
            "WHERE google_sub LIKE 'pytest-%' ORDER BY google_sub"
        )
    )
    assert [(r["google_sub"], r["selected_location_id"]) for r in rows] == [
        ("pytest-ana", None),
        ("pytest-luis", luis_locs[0].id),
    ]

    # Second barrier: even a statement that forgets the ownership check is refused.
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        asyncio.run(
            _api_execute(
                "UPDATE account SET selected_location_id = $2 WHERE id = $1::uuid",
                ana,
                luis_locs[0].id,
            )
        )


def test_session_is_found_only_by_hash_while_live():
    token = new_token()

    async def scenario(store):
        account = await store.upsert_account("pytest-ana", "ana@example.com")
        await store.create_session(account, hash_token(token), NOW, LATER)
        live = await store.account_for_session(hash_token(token), NOW)
        last_second = await store.account_for_session(
            hash_token(token), LATER - timedelta(seconds=1)
        )
        expired = await store.account_for_session(hash_token(token), LATER)
        by_plain_token = await store.account_for_session(token, NOW)
        await store.revoke_session(hash_token(token), NOW + timedelta(hours=1))
        await store.revoke_session(hash_token(token), NOW + timedelta(hours=2))  # no-op
        revoked = await store.account_for_session(hash_token(token), NOW)
        return account, live, last_second, expired, by_plain_token, revoked

    account, live, last_second, expired, by_plain_token, revoked = with_store(scenario)
    assert (live.id, live.email, live.selected_location_id) == (account, "ana@example.com", None)
    assert last_second is not None
    assert expired is None
    assert by_plain_token is None
    assert revoked is None
    rows = asyncio.run(
        _owner(
            "SELECT s.token_hash, s.revoked_at, s.expires_at FROM session s "
            "JOIN account a ON a.id = s.account_id WHERE a.google_sub = 'pytest-ana'"
        )
    )
    assert len(rows) == 1  # revoked, not deleted
    assert rows[0]["token_hash"] == hash_token(token) != token
    assert rows[0]["revoked_at"] == NOW + timedelta(hours=1)
    assert rows[0]["expires_at"] == LATER
    dump = asyncio.run(_owner("SELECT s::text AS row FROM session s"))
    assert all(token not in r["row"] for r in dump)


def test_api_role_has_only_the_privileges_the_routes_need():
    async def scenario(store):
        account = await store.upsert_account("pytest-ana", "ana@example.com")
        await store.sync_locations(account, [BAR], NOW)
        await store.create_session(account, "h" * 64, NOW, LATER)
        return account

    account = with_store(scenario)
    for sql in (
        "DELETE FROM session",
        "DELETE FROM location",
        "DELETE FROM account",
        "TRUNCATE session",
        "UPDATE account SET google_sub = 'otro'",
        "UPDATE account SET id = gen_random_uuid()",
        "UPDATE location SET account_id = gen_random_uuid()",
        "UPDATE location SET id = 'loc_otro'",
        "UPDATE location SET gbp_location = 'locations/otra'",
        "UPDATE session SET expires_at = now() + interval '10 years'",
        "UPDATE session SET token_hash = repeat('a', 64)",
        "UPDATE session SET account_id = gen_random_uuid()",
        # Unchanged: contacts, events and Google tokens stay insert-only.
        "SELECT refresh_token_enc FROM google_connection",
        "SELECT google_sub FROM google_connection",
        "SELECT contact FROM waitlist_entry",
        "SELECT kind FROM funnel_event",
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            asyncio.run(_api_execute(sql))
    assert account


def test_database_rejects_invalid_rows():
    async def scenario(store):
        return await store.upsert_account("pytest-ana", "ana@example.com")

    account = with_store(scenario)
    with pytest.raises(asyncpg.CheckViolationError):
        asyncio.run(
            _api_execute(
                "INSERT INTO session (account_id, token_hash, expires_at) "
                "VALUES ($1::uuid, 'token-en-claro', now())",
                account,
            )
        )
    with pytest.raises(asyncpg.CheckViolationError):
        asyncio.run(
            _api_execute(
                "INSERT INTO location (id, account_id, gbp_account, gbp_location, title) "
                "VALUES ('sin-prefijo', $1::uuid, 'accounts/1', 'locations/pytest-x', 'x')",
                account,
            )
        )


def test_whole_journey_over_http_on_postgresql():
    """Connect, read /me, select, diagnose and log out with the real store and fake Google."""
    from urllib.parse import parse_qs, urlparse

    from fastapi.testclient import TestClient

    from app.adapters.crypto.sealed import generate_keypair
    from app.adapters.google_oauth.fake import FakeGoogleOAuth
    from app.adapters.places.fake import FakePlaces
    from app.api.main import create_app
    from app.api.session import SESSION_COOKIE
    from app.config import Settings
    from app.ports.google_oauth import Identity

    class QuietStore(PostgresStore):
        async def record_event(self, *args, **kwargs) -> None:
            """Funnel rows of this test could not be told apart from real ones: skip them."""

    google = FakeGoogleOAuth()
    settings = Settings(
        _env_file=None,
        oauth_state_secret="secreto-de-pruebas",
        token_public_key=generate_keypair()[0],
        cookie_secure=False,
    )
    header = {"X-Requested-With": "fichaitor"}

    def connect(client, sub) -> str:
        client.cookies.clear()  # another browser
        google.identity_value = Identity(sub=sub, email=f"{sub}@example.com")
        state = parse_qs(
            urlparse(client.get("/api/v1/google/oauth/start").headers["location"]).query
        )
        r = client.get(
            "/api/v1/google/oauth/callback", params={"code": "c", "state": state["state"][0]}
        )
        assert r.headers["location"] == "/app?google=ok&fichas=1"
        return client.cookies.get(SESSION_COOKIE)

    def as_user(client, token):
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, token)
        return client

    app = create_app(settings, FakePlaces(), QuietStore(API_URL), google)
    try:
        # One client: the lifespan, and with it the connection pool, belongs to it.
        with TestClient(app, follow_redirects=False) as client:
            assert client.get("/api/v1/me").status_code == 401
            ana = connect(client, "pytest-e2e-ana")
            luis = connect(client, "pytest-e2e-luis")
            (other,) = as_user(client, luis).get("/api/v1/me").json()["locations"]
            me = as_user(client, ana).get("/api/v1/me").json()
            assert me["email"] == "pytest-e2e-ana@example.com"
            assert me["selected_location_id"] is None
            (location,) = me["locations"]
            assert location["id"] != other["id"]

            assert client.get("/api/v1/me/diagnosis").status_code == 409
            body = {"location_id": other["id"]}
            assert client.post("/api/v1/me/location", json=body, headers=header).status_code == 404
            body = {"location_id": location["id"]}
            assert client.post("/api/v1/me/location", json=body).status_code == 403
            r = client.post("/api/v1/me/location", json=body, headers=header)
            assert r.status_code == 200
            assert r.json()["selected_location_id"] == location["id"]
            # The diagnosis comes from what the worker stores: nothing yet, and no Places.
            r = client.get("/api/v1/me/diagnosis")
            assert (r.status_code, r.json()["status"]) == (404, "pendiente")

            assert client.post("/api/v1/auth/logout", headers=header).status_code == 204
            assert as_user(client, ana).get("/api/v1/me").status_code == 401
            assert as_user(client, luis).get("/api/v1/me").status_code == 200
    finally:
        asyncio.run(_owner("DELETE FROM google_connection WHERE google_sub LIKE 'pytest-e2e-%'"))
