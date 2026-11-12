"""Integration: Google connections in PostgreSQL. Same environment as test_store_postgres."""

import asyncio
import os

import asyncpg
import pytest

from app.adapters.store.postgres import PostgresStore
from app.ports.store import GoogleConnection

API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL), reason="sin base de datos de pruebas configurada"
)

SUB = "pytest-google-sub"


async def _owner(sql: str, *args):
    conn = await asyncpg.connect(OWNER_URL)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
def clean():
    asyncio.run(_owner("DELETE FROM google_connection WHERE google_sub = $1", SUB))
    yield
    asyncio.run(_owner("DELETE FROM google_connection WHERE google_sub = $1", SUB))
    asyncio.run(_owner("DELETE FROM funnel_event WHERE kind = 'connected' AND source = 'pytest'"))


def test_connection_is_appended_and_reconnection_adds_a_row():
    conn = GoogleConnection(
        google_sub=SUB,
        email="pytest@example.com",
        refresh_token_enc="cifrado",
        key_version=1,
        scopes=("openid", "https://www.googleapis.com/auth/business.manage"),
        locations=({"location": "locations/1", "title": "Bar de pruebas"},),
    )

    async def scenario():
        store = PostgresStore(API_URL)
        await store.connect()
        try:
            await store.add_google_connection(conn)
            await store.add_google_connection(conn)
            await store.record_event("connected", "pytest")
        finally:
            await store.close()

    asyncio.run(scenario())
    rows = asyncio.run(
        _owner(
            "SELECT scopes, locations->0->>'title' AS title, key_version "
            "FROM google_connection WHERE google_sub = $1",
            SUB,
        )
    )
    assert len(rows) == 2
    assert rows[0]["title"] == "Bar de pruebas"
    assert "openid" in rows[0]["scopes"]


def test_api_role_cannot_read_or_change_tokens():
    async def attempt(sql: str):
        conn = await asyncpg.connect(API_URL)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    for sql in (
        "SELECT refresh_token_enc FROM google_connection",
        "UPDATE google_connection SET refresh_token_enc = 'x' WHERE false",
        "DELETE FROM google_connection WHERE false",
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            asyncio.run(attempt(sql))
