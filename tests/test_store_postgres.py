"""Integration tests against a real PostgreSQL. Skipped unless FICHAITOR_TEST_DATABASE_URL is set.

    docker compose up -d db
    FICHAITOR_TEST_DATABASE_URL=postgresql://fichaitor_api_dev:dev@127.0.0.1:5433/fichaitor \
    FICHAITOR_TEST_OWNER_URL=postgresql://fichaitor:dev@127.0.0.1:5433/fichaitor uv run pytest -q
"""

import asyncio
import os

import asyncpg
import pytest

from app.adapters.store.postgres import PostgresStore
from app.ports.store import WaitlistSignup

API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL), reason="sin base de datos de pruebas configurada"
)

SIGNUP = WaitlistSignup(
    channel="whatsapp",
    contact="+34600999001",
    consent_version="test",
    place_id="p-test",
    business_name="Bar de pruebas",
    score=55,
    source="pytest",
)


def run(coro):
    return asyncio.run(coro)


async def _owner_fetch(sql: str, *args):
    conn = await asyncpg.connect(OWNER_URL)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
def clean():
    run(_owner_fetch("DELETE FROM waitlist_entry WHERE source = 'pytest'"))
    run(_owner_fetch("DELETE FROM funnel_event WHERE source = 'pytest'"))
    yield


def test_signup_is_inserted_once_and_event_recorded():
    async def scenario():
        store = PostgresStore(API_URL)
        await store.connect()
        try:
            await store.add_signup(SIGNUP)
            await store.add_signup(SIGNUP)  # duplicate: no error, no second row
            await store.record_event("signup", "pytest", "p-test", 55)
        finally:
            await store.close()

    run(scenario())
    rows = run(
        _owner_fetch(
            "SELECT contact, score, consent_version FROM waitlist_entry WHERE source = 'pytest'"
        )
    )
    assert [(r["contact"], r["score"], r["consent_version"]) for r in rows] == [
        ("+34600999001", 55, "test")
    ]
    events = run(_owner_fetch("SELECT kind FROM funnel_event WHERE source = 'pytest'"))
    assert [e["kind"] for e in events] == ["signup"]


def test_api_role_cannot_read_update_or_delete_contacts():
    async def attempt(sql: str):
        conn = await asyncpg.connect(API_URL)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    for sql in (
        "SELECT contact FROM waitlist_entry",
        "UPDATE waitlist_entry SET contact = 'x'",
        "DELETE FROM waitlist_entry",
        "SELECT * FROM funnel_event",
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            run(attempt(sql))


def test_database_rejects_invalid_rows():
    async def attempt(channel: str, score: int):
        conn = await asyncpg.connect(API_URL)
        try:
            await conn.execute(
                "INSERT INTO waitlist_entry (channel, contact, score, source, consent_version) "
                "VALUES ($1, '+34600999002', $2, 'pytest', 'test')",
                channel,
                score,
            )
        finally:
            await conn.close()

    with pytest.raises(asyncpg.CheckViolationError):
        run(attempt("sms", 50))
    with pytest.raises(asyncpg.CheckViolationError):
        run(attempt("email", 150))
