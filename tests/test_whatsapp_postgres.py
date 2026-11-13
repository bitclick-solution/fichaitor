"""Integration: the bag of WhatsApp messages and the job key of its webhook on PostgreSQL,
with the roles of the API and of the conversation, against the TEST database only.

Skipped unless the four URLs are set (tests/conftest.py stops the run if one does not end
in `_test`). Every statement expected to be denied runs in a transaction rolled back.
"""

import asyncio
import os
from datetime import UTC, datetime

import asyncpg
import pytest

from app.adapters.conversation_store.postgres import PostgresConversationStore
from app.adapters.store.postgres import PostgresStore
from app.domain import message_allowance as allowance
from app.domain.conversation import DEFAULT_TIMEZONE, timezone_for
from app.ports.google_oauth import ProfileLocation

API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")
WORKER_URL = os.environ.get("FICHAITOR_TEST_WORKER_URL")
CONVERSATION_URL = os.environ.get("FICHAITOR_TEST_CONVERSATION_URL")

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL and WORKER_URL and CONVERSATION_URL),
    reason="sin base de datos de pruebas configurada",
)

NOW = datetime(2027, 3, 10, 11, 0, tzinfo=UTC)
SUB = "pytest-wa-1"
PLACES = [
    ProfileLocation(
        "accounts/pytest-wa", f"locations/pytest-wa-{i}", f"Bar {i}", f"pl-wa-{i}", True
    )
    for i in range(3)
]
ZONE = timezone_for(DEFAULT_TIMEZONE)


def run(coro):
    return asyncio.run(coro)


async def owner(sql: str, *args):
    conn = await asyncpg.connect(OWNER_URL)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def refused(url: str, sql: str, *args) -> str | None:
    """The error the role of `url` gets for that statement (None if it is let in), in a
    transaction that is always rolled back."""
    conn = await asyncpg.connect(url)
    try:
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(sql, *args)
        except asyncpg.PostgresError as exc:
            return type(exc).__name__ + ": " + str(exc)
        finally:
            await tx.rollback()
        return None
    finally:
        await conn.close()


async def cleanup() -> None:
    mine = "(SELECT id FROM account WHERE google_sub LIKE 'pytest-wa-%')"
    for sql in (
        "DELETE FROM usage_counter WHERE scope LIKE 'whatsapp_month:%'"
        f" AND split_part(scope, ':', 2) IN (SELECT id::text FROM account WHERE id IN {mine})",
        f"UPDATE account SET selected_location_id = NULL WHERE id IN {mine}",
        f"DELETE FROM location_activation_log WHERE account_id IN {mine}",
        f"DELETE FROM plan_entitlement WHERE account_id IN {mine}",
        f"DELETE FROM conversation WHERE account_id IN {mine}",
        f"DELETE FROM location WHERE account_id IN {mine}",
        f"DELETE FROM account WHERE id IN {mine}",
    ):
        try:
            await owner(sql)
        except asyncpg.UndefinedTableError:
            pass


@pytest.fixture(autouse=True)
def clean():
    run(cleanup())
    yield
    run(cleanup())


async def account(pro_plus: int) -> str:
    """An account with three active profiles, `pro_plus` of them in Pro+."""
    api = PostgresStore(API_URL)
    await api.connect()
    try:
        account_id = await api.upsert_account(SUB, f"{SUB}@example.com")
        await api.sync_locations(account_id, PLACES, NOW)
    finally:
        await api.close()
    rows = await owner(
        "SELECT id FROM location WHERE account_id = $1::uuid ORDER BY gbp_location", account_id
    )
    for index, row in enumerate(rows):
        plan = "pro_whatsapp" if index < pro_plus else "pro"
        await owner(
            "UPDATE location SET active_since = now(), plan = $2 WHERE id = $1", row["id"], plan
        )
    return account_id


async def with_store(fn):
    pool = await asyncpg.create_pool(CONVERSATION_URL, min_size=1, max_size=8)
    try:
        return await fn(PostgresConversationStore(pool))
    finally:
        await pool.close()


async def hits(account_id: str, now: datetime = NOW) -> int | None:
    start, _ = allowance.month_window(now, ZONE)
    rows = await owner(
        "SELECT hits FROM usage_counter WHERE scope = $1 AND window_start = $2",
        allowance.scope(account_id),
        start,
    )
    return rows[0]["hits"] if rows else None


def test_the_bag_is_80_per_pro_plus_profile_and_never_goes_over_with_senders_at_once():
    async def scenario():
        account_id = await account(pro_plus=2)

        async def go(store):
            results = await asyncio.gather(
                *(store.take_message_allowance(account_id, 1, NOW) for _ in range(200))
            )
            return sum(results)

        assert await with_store(go) == 160
        assert await hits(account_id) == 160
        # The API reads it for GET /me (it has SELECT on the counter).
        api = PostgresStore(API_URL)
        await api.connect()
        try:
            start, _ = allowance.month_window(NOW, ZONE)
            assert await api.quota_used(allowance.scope(account_id), start) == 160
        finally:
            await api.close()

    run(scenario())


def test_several_units_are_counted_all_or_none():
    async def scenario():
        account_id = await account(pro_plus=1)

        async def go(store):
            for _ in range(77):
                assert await store.take_message_allowance(account_id, 1, NOW)
            assert not await store.take_message_allowance(account_id, 4, NOW)  # 81 > 80
            assert await hits(account_id) == 77  # nothing of the four stayed
            assert await store.take_message_allowance(account_id, 3, NOW)
            assert not await store.take_message_allowance(account_id, 1, NOW)

        await with_store(go)
        assert await hits(account_id) == 80

    run(scenario())


def test_without_pro_plus_there_is_no_bag_and_next_month_it_starts_again():
    async def scenario():
        none = await account(pro_plus=0)
        assert not await with_store(lambda s: s.take_message_allowance(none, 1, NOW))
        assert await hits(none) is None
        await owner("UPDATE location SET plan = 'pro_whatsapp' WHERE account_id = $1::uuid", none)
        april = datetime(2027, 4, 1, 0, 30, tzinfo=UTC)  # 02:30 of 1 April in Madrid

        async def go(store):
            assert await store.take_message_allowance(none, 1, NOW)
            assert await store.take_message_allowance(none, 1, april)

        await with_store(go)
        assert await hits(none) == 1 and await hits(none, april) == 1

    run(scenario())


def test_nobody_but_the_owner_can_set_the_bag_back_b11():
    async def scenario():
        account_id = await account(pro_plus=1)
        await with_store(lambda s: s.take_message_allowance(account_id, 1, NOW))
        scope = allowance.scope(account_id)
        for url in (CONVERSATION_URL, API_URL):
            error = await refused(url, "UPDATE usage_counter SET hits = 0 WHERE scope = $1", scope)
            assert error is not None and "CheckViolation" in error
            error = await refused(
                url, "UPDATE usage_counter SET hits = hits + 5 WHERE scope = $1", scope
            )
            assert error is not None
        error = await refused(CONVERSATION_URL, "DELETE FROM usage_counter WHERE scope = $1", scope)
        assert error is not None and "InsufficientPrivilege" in error

    run(scenario())


@pytest.mark.parametrize(
    ("key", "ok"),
    [
        ("whatsapp:wamid.HBgLMzQ2MDAxMTEyMjIVAgASGBQzQUM5QTFFNjk0MDQ0N0E4RTQ0MAA=", True),
        ("whatsapp:wamid.abc+/_-", True),
        ("whatsapp:wamid con espacio", False),
        ("whatsapp:" + "w" * 191, False),
        ("retention:2027-03-10", False),
    ],
)
def test_the_api_enqueues_a_whatsapp_message_only_under_its_key(key, ok):
    error = run(
        refused(
            API_URL,
            "INSERT INTO job (kind, payload, idempotency_key, max_attempts)"
            " VALUES ('channel_inbound', '{}'::jsonb, $1, 1)",
            key,
        )
    )
    assert (error is None) is ok, error


def test_the_key_function_keeps_pg_temp_last_in_its_search_path_m6():
    rows = run(
        owner(
            "SELECT p.proconfig FROM pg_catalog.pg_proc p"
            " JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace"
            " WHERE p.proname = 'job_key_fits' AND n.nspname = 'public'"
        )
    )
    assert rows and rows[0]["proconfig"] == ["search_path=pg_catalog, public, pg_temp"]
