"""Integration: the opposition to the commercial communications at the sign-up and in
Ajustes (db/schema_admin.sql, migration of 2026-11-13; docs/contrato-web-api.md,
«Comunicaciones comerciales: en el alta y en Ajustes») against the database, with the
role of the API.

Covers: the box of the sign-up recorded only when the callback creates the account, in
the same write; a sign-in never touching the choice; Ajustes opposing and withdrawing
(idempotent), with the proof of the withdrawal; the sending of a commercial communication
following the same table; nothing for a deleted account; everything gone with the
account; and the API role reaching the tables only through the functions.

Skipped unless the test URLs are set. They must point to the TEST database:

    FICHAITOR_TEST_DB=fichaitor_optout_test bash scripts/preparar_bd_pruebas.sh
"""

import asyncio
import os
import uuid
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
from app.ports.google_oauth import Identity

API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")
WORKER_URL = os.environ.get("FICHAITOR_TEST_WORKER_URL")

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL and WORKER_URL),
    reason="sin base de datos de pruebas configurada",
)

PREFIX = "pytest-optout-"
MINE = f"(SELECT id FROM account WHERE google_sub LIKE '{PREFIX}%')"
PREFS = "/api/v1/me/communications"
HEADER = {"X-Requested-With": "fichaitor"}


def run(coro):
    return asyncio.run(coro)


async def as_role(url: str, sql: str, *args):
    conn = await asyncpg.connect(url)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


def owner(sql: str, *args):
    return run(as_role(OWNER_URL, sql, *args))


async def cleanup():
    for sql in (
        f"DELETE FROM session WHERE account_id IN {MINE}",
        f"DELETE FROM google_connection WHERE google_sub LIKE '{PREFIX}%'",
        f"DELETE FROM account WHERE id IN {MINE}",
    ):
        await as_role(OWNER_URL, sql)


@pytest.fixture(autouse=True)
def clean():
    run(cleanup())
    yield
    run(cleanup())


class QuietStore(PostgresStore):
    async def record_event(self, *args, **kwargs) -> None:
        """Funnel rows of this test could not be told apart from real ones: skip them."""


@pytest.fixture
def api():
    google = FakeGoogleOAuth()
    google.profile_locations = []
    settings = Settings(
        _env_file=None,
        oauth_state_secret="secreto-de-pruebas",
        token_public_key=generate_keypair()[0],
        cookie_secure=False,
    )
    app = create_app(settings, FakePlaces(), QuietStore(API_URL), google, jobs=MemoryJobQueue())
    with TestClient(app, follow_redirects=False) as client:
        yield client, google


def connect(api, sub: str, optout: str | None = None) -> str:
    """Connects as that Google identity; returns the session token."""
    client, google = api
    client.cookies.clear()
    google.identity_value = Identity(sub=sub, email=f"{sub}@example.com")
    params = {} if optout is None else {"commercial_optout": optout}
    start = client.get("/api/v1/google/oauth/start", params=params).headers["location"]
    state = parse_qs(urlparse(start).query)["state"][0]
    r = client.get("/api/v1/google/oauth/callback", params={"code": "c", "state": state})
    assert r.status_code == 303 and "google=ok" in r.headers["location"]
    return client.cookies.get(SESSION_COOKIE)


def as_(api, token: str) -> TestClient:
    client, _ = api
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, token)
    return client


def sub() -> str:
    return f"{PREFIX}{uuid.uuid4().hex[:10]}"


def optout_of(google_sub: str) -> list[str]:
    rows = owner(
        "SELECT o.source FROM communication_optout o JOIN account x ON x.id = o.account_id"
        " WHERE x.google_sub = $1",
        google_sub,
    )
    return [r["source"] for r in rows]


def account_id(google_sub: str) -> str:
    return str(owner("SELECT id FROM account WHERE google_sub = $1", google_sub)[0]["id"])


# --- at the sign-up ---------------------------------------------------------------------


def test_the_box_of_the_sign_up_opposes_only_a_new_account(api):
    ana, luis = sub(), sub()
    token = connect(api, ana, optout="1")
    assert optout_of(ana) == ["signup"]
    body = as_(api, token).get(PREFS).json()
    assert body["commercial"] is False and body["source"] == "signup"

    connect(api, luis)  # created without the box: legitimate interest
    assert optout_of(luis) == []
    connect(api, luis, optout="1")  # a later sign-in with it: nothing changes
    assert optout_of(luis) == []


def test_the_opposition_of_the_sign_up_is_in_the_same_write_as_the_account():
    # If the opposition cannot be written, the account is not created either: a retry
    # would find it and never record the box again.
    google_sub = sub()

    async def scenario():
        store = PostgresStore(API_URL)
        await store.connect()
        try:
            conn = await asyncpg.connect(OWNER_URL)
            try:
                await conn.execute(
                    "ALTER TABLE communication_optout"
                    " ADD CONSTRAINT pytest_optout_refuses CHECK (source <> 'signup') NOT VALID"
                )
                with pytest.raises(asyncpg.CheckViolationError):
                    await store.upsert_account(google_sub, "x@example.com", commercial_optout=True)
            finally:
                await conn.execute(
                    "ALTER TABLE communication_optout"
                    " DROP CONSTRAINT IF EXISTS pytest_optout_refuses"
                )
                await conn.close()
            assert not await store.account_exists(google_sub)
            account = await store.upsert_account(
                google_sub, "x@example.com", commercial_optout=True
            )
            assert await store.upsert_account(google_sub, "y@example.com") == account
        finally:
            await store.close()

    run(scenario())
    assert optout_of(google_sub) == ["signup"]


# --- in Ajustes -------------------------------------------------------------------------


def test_ajustes_opposes_withdraws_and_keeps_the_proof(api):
    google_sub = sub()
    client = as_(api, connect(api, google_sub))
    assert client.get(PREFS).json() == {"commercial": True, "changed_at": None, "source": None}

    off = client.put(PREFS, json={"commercial": False}, headers=HEADER).json()
    assert off["commercial"] is False and off["source"] == "settings"
    assert client.put(PREFS, json={"commercial": False}, headers=HEADER).json() == off
    assert optout_of(google_sub) == ["settings"]

    on = client.put(PREFS, json={"commercial": True}, headers=HEADER).json()
    assert on["commercial"] is True and on["source"] == "settings" and on["changed_at"]
    assert optout_of(google_sub) == []
    proof = owner(
        "SELECT withdrawn_at, source FROM communication_optout_withdrawal"
        " WHERE account_id = $1::uuid",
        account_id(google_sub),
    )
    assert [r["source"] for r in proof] == ["settings"]
    # Withdrawing again changes nothing, not even the date.
    assert client.put(PREFS, json={"commercial": True}, headers=HEADER).json() == on

    # Opposing again (by the link of an email this time) wins over the old withdrawal.
    run(
        as_role(
            API_URL,
            "SELECT fichaitor_communication_unsubscribe($1::uuid, 'un_clic')",
            account_id(google_sub),
        )
    )
    assert client.get(PREFS).json()["source"] == "un_clic"
    assert client.get(PREFS).json()["commercial"] is False


def test_the_sending_follows_the_same_table():
    google_sub = sub()

    async def scenario():
        store = PostgresStore(API_URL)
        await store.connect()
        try:
            account = await store.upsert_account(google_sub, "x@example.com")
            await store.unsubscribe_communications(account, "settings")
            listed = await as_role(
                OWNER_URL,
                "SELECT EXISTS (SELECT 1 FROM communication_optout WHERE account_id = $1::uuid)",
                account,
            )
            assert listed[0][0] is True
            await store.resubscribe_communications(account, "settings")
            pref = await store.communication_preference(account)
            assert pref is not None and pref.commercial is True
            return account
        finally:
            await store.close()

    account = run(scenario())
    # The same query the audience of a communication uses (communication_recipients).
    assert owner("SELECT 1 FROM communication_optout WHERE account_id = $1::uuid", account) == []


def test_nothing_for_an_account_that_does_not_exist_or_left_and_all_goes_with_it():
    google_sub = sub()

    async def scenario():
        store = PostgresStore(API_URL)
        await store.connect()
        try:
            assert await store.communication_preference(str(uuid.uuid4())) is None
            await store.resubscribe_communications(str(uuid.uuid4()), "settings")
            account = await store.upsert_account(google_sub, "x@example.com")
            await store.unsubscribe_communications(account, "settings")
            await store.resubscribe_communications(account, "settings")
            await store.unsubscribe_communications(account, "settings")
            return account
        finally:
            await store.close()

    account = run(scenario())
    owner("UPDATE account SET deleted_at = now() WHERE id = $1::uuid", account)
    for table in ("communication_optout", "communication_optout_withdrawal"):
        assert owner(f"SELECT 1 FROM {table} WHERE account_id = $1::uuid", account) == []  # noqa: S608

    async def after():
        store = PostgresStore(API_URL)
        await store.connect()
        try:
            assert await store.communication_preference(account) is None
            await store.unsubscribe_communications(account, "settings")
            await store.resubscribe_communications(account, "settings")
        finally:
            await store.close()

    run(after())
    assert owner("SELECT 1 FROM communication_optout WHERE account_id = $1::uuid", account) == []


def test_the_database_refuses_an_unknown_source():
    google_sub = sub()
    owner("INSERT INTO account (google_sub, email) VALUES ($1, 'x@example.com')", google_sub)
    account = account_id(google_sub)
    with pytest.raises(asyncpg.CheckViolationError):
        run(
            as_role(API_URL, "SELECT fichaitor_communication_unsubscribe($1::uuid, 'web')", account)
        )
    run(
        as_role(
            API_URL, "SELECT fichaitor_communication_unsubscribe($1::uuid, 'settings')", account
        )
    )
    with pytest.raises(asyncpg.CheckViolationError):
        run(
            as_role(
                API_URL, "SELECT fichaitor_communication_resubscribe($1::uuid, 'enlace')", account
            )
        )


def test_the_roles_reach_the_tables_only_through_the_functions():
    for url in (API_URL, WORKER_URL):
        for table in ("communication_optout", "communication_optout_withdrawal"):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                run(as_role(url, f"SELECT 1 FROM {table} LIMIT 1"))  # noqa: S608
    for sql in (
        "SELECT * FROM fichaitor_communication_preference(gen_random_uuid())",
        "SELECT fichaitor_communication_resubscribe(gen_random_uuid(), 'settings')",
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            run(as_role(WORKER_URL, sql))
        run(as_role(API_URL, sql))
    # SECURITY DEFINER with a fixed search_path, never a temporary schema first.
    rows = owner(
        "SELECT proname, prosecdef, proconfig FROM pg_proc WHERE proname IN"
        " ('fichaitor_communication_preference', 'fichaitor_communication_resubscribe')"
    )
    assert len(rows) == 2
    for row in rows:
        assert row["prosecdef"] is True
        assert row["proconfig"] == ["search_path=pg_catalog, public, pg_temp"]
