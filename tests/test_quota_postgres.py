"""Integration: the caps on paid calls for all clients together live in PostgreSQL
(B2 of docs/seguridad/revision-2026-11-12.md). A restart, a deploy or a second API
process no longer starts them from zero.

Skipped unless the three URLs of the test database are set (see tests/conftest.py).
Only counters whose scope starts with 'pytest_' are created, and they are removed
afterwards. Every statement a privilege test expects to be denied carries WHERE false.
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from app.adapters.store.postgres import PostgresStore
from app.adapters.worker_store.postgres import PostgresWorkerStore
from app.domain.offboarding import RetentionPolicy

API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")
WORKER_URL = os.environ.get("FICHAITOR_TEST_WORKER_URL")

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL and WORKER_URL), reason="sin base de datos de pruebas configurada"
)

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
DAY = 86400
DENIED = asyncpg.InsufficientPrivilegeError


def run(coro):
    return asyncio.run(coro)


async def _as(url: str, sql: str, *args):
    conn = await asyncpg.connect(url)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
def clean():
    run(_as(OWNER_URL, "DELETE FROM usage_counter WHERE scope LIKE 'pytest\\_%'"))
    yield
    run(_as(OWNER_URL, "DELETE FROM usage_counter WHERE scope LIKE 'pytest\\_%'"))


@pytest.fixture
def scope() -> str:
    return f"pytest_cap:{uuid.uuid4().hex}"


async def _store() -> PostgresStore:
    store = PostgresStore(API_URL)
    await store.connect()
    return store


def test_a_cap_survives_a_restart_and_is_shared_by_two_processes(scope):
    async def go():
        first, second = await _store(), await _store()
        try:
            taken = [await first.take_quota(scope, 2, DAY, NOW) for _ in range(2)]
            # Another process, or the same one after a restart: the count is the same.
            return taken, await second.take_quota(scope, 2, DAY, NOW)
        finally:
            await first.close()
            await second.close()

    taken, third = run(go())
    assert taken == [True, True] and third is False


def test_taking_is_atomic_under_concurrency(scope):
    async def go():
        stores = [await _store() for _ in range(4)]
        try:
            results = await asyncio.gather(
                *(stores[n % 4].take_quota(scope, 5, DAY, NOW) for n in range(40))
            )
            hits = await _as(OWNER_URL, "SELECT hits FROM usage_counter WHERE scope = $1", scope)
            return results, hits
        finally:
            for store in stores:
                await store.close()

    results, hits = run(go())
    assert results.count(True) == 5
    assert [r["hits"] for r in hits] == [5]


def test_a_new_window_starts_from_zero_and_asking_counts_nothing(scope):
    async def go():
        store = await _store()
        try:
            asked = [await store.quota_left(scope, 1, DAY, NOW) for _ in range(3)]
            taken = await store.take_quota(scope, 1, DAY, NOW)
            after = await store.quota_left(scope, 1, DAY, NOW)
            denied = await store.take_quota(scope, 1, DAY, NOW)
            tomorrow = await store.take_quota(scope, 1, DAY, NOW + timedelta(days=1))
            return asked, taken, after, denied, tomorrow
        finally:
            await store.close()

    asked, taken, after, denied, tomorrow = run(go())
    assert asked == [True, True, True] and taken is True
    assert (after, denied, tomorrow) == (False, False, True)


def test_a_cap_of_zero_lets_nothing_through(scope):
    async def go():
        store = await _store()
        try:
            return await store.take_quota(scope, 0, DAY, NOW), await store.quota_left(
                scope, 0, DAY, NOW
            )
        finally:
            await store.close()

    assert run(go()) == (False, False)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM usage_counter WHERE false",
        "UPDATE usage_counter SET scope = 'x' WHERE false",
        "UPDATE usage_counter SET window_start = now() WHERE false",
        "UPDATE usage_counter SET expires_at = now() WHERE false",
        "TRUNCATE usage_counter",
    ],
)
def test_the_api_role_can_only_count(sql):
    with pytest.raises(DENIED):
        run(_as(API_URL, sql))


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO usage_counter (scope, window_start, expires_at, hits)"
        " SELECT 'pytest_x', now(), now() + interval '1 day', 0 WHERE false",
        "UPDATE usage_counter SET hits = 0 WHERE false",
        "SELECT scope FROM usage_counter LIMIT 1",
    ],
)
def test_the_worker_role_can_only_delete_old_counters(sql):
    with pytest.raises(DENIED):
        run(_as(WORKER_URL, sql))


def test_the_retention_pass_deletes_the_counters_of_past_windows(scope):
    old, current = f"{scope}0", f"{scope}1"

    async def go():
        store = await _store()
        pool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=2)
        try:
            await store.take_quota(old, 5, DAY, NOW - timedelta(days=2))
            await store.take_quota(current, 5, DAY, NOW)
            counts = await PostgresWorkerStore(pool).run_retention(NOW, RetentionPolicy())
            left = await _as(
                OWNER_URL,
                "SELECT scope FROM usage_counter WHERE scope IN ($1, $2)",
                old,
                current,
            )
            return counts, {r["scope"] for r in left}
        finally:
            await pool.close()
            await store.close()

    counts, left = run(go())
    assert left == {current}
    assert counts["usage_counter"] >= 1
