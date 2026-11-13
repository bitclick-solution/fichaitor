"""Integration: the limits of the free plan against PostgreSQL (TEST database only).

- The monthly counter of the replies drafted by the model lives in usage_counter, per
  profile ("ai_replies:loc_..."), with the window of the calendar month in Madrid. It is
  taken in one statement, so two drafts at once never go over 25, and the B11 trigger
  still holds: the API only adds one.
- `location_plan` reads the plan of an active profile of the account of the session, and
  nothing of another account.

Skipped unless the four URLs are set (tests/conftest.py checks the name of the database).
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime

import asyncpg
import pytest

from app.adapters.store.postgres import PostgresStore
from app.domain.plans import Plan
from app.domain.quota import ai_replies_scope, month_window

API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")
WORKER_URL = os.environ.get("FICHAITOR_TEST_WORKER_URL")
CONVERSATION_URL = os.environ.get("FICHAITOR_TEST_CONVERSATION_URL")

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL and WORKER_URL and CONVERSATION_URL),
    reason="sin base de datos de pruebas configurada",
)

PREFIX = "pytest-plan-"
NOW = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


async def owner(sql: str, *args):
    conn = await asyncpg.connect(OWNER_URL)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def cleanup():
    mine = f"(SELECT id FROM account WHERE google_sub LIKE '{PREFIX}%')"
    for sql in (
        "DELETE FROM usage_counter WHERE scope LIKE 'ai_replies:loc_pytestplan%'",
        f"UPDATE account SET selected_location_id = NULL WHERE id IN {mine}",
        f"DELETE FROM location_activation_log WHERE account_id IN {mine}",
        f"DELETE FROM location WHERE account_id IN {mine}",
        f"DELETE FROM plan_entitlement WHERE account_id IN {mine}",
        f"DELETE FROM account WHERE id IN {mine}",
    ):
        await owner(sql)


@pytest.fixture(autouse=True)
def clean():
    run(cleanup())
    yield
    run(cleanup())


async def account_with(plan: str | None) -> tuple[str, str]:
    sub = f"{PREFIX}{uuid.uuid4().hex[:8]}"
    account = (
        await owner(
            "INSERT INTO account (google_sub, email) VALUES ($1, 'x@x') RETURNING id::text", sub
        )
    )[0]["id"]
    location = f"loc_pytestplan{uuid.uuid4().hex[:8]}"
    await owner(
        "INSERT INTO location (id, account_id, gbp_account, gbp_location, title)"
        " VALUES ($1, $2::uuid, 'accounts/1', $3, 'Bar')",
        location,
        account,
        f"locations/{sub}",
    )
    if plan is not None:
        await owner(
            "UPDATE location SET active_since = now(), plan = $2 WHERE id = $1", location, plan
        )
    return account, location


class Api:
    async def __aenter__(self) -> PostgresStore:
        self.store = PostgresStore(API_URL)
        await self.store.connect()
        return self.store

    async def __aexit__(self, *exc) -> None:
        await self.store.close()


def test_the_monthly_counter_stops_at_the_cap_even_with_drafts_at_once():
    async def go():
        _, location = await account_with("gratis")
        scope = ai_replies_scope(location)
        start, end = month_window(NOW)
        async with Api() as store:
            taken = await asyncio.gather(
                *(store.take_quota_in(scope, 25, start, end) for _ in range(40))
            )
            used = await store.quota_used(scope, start)
            next_month = await store.quota_used(scope, end)
        rows = await owner(
            "SELECT window_start, expires_at, hits FROM usage_counter WHERE scope = $1", scope
        )
        return taken, used, next_month, rows, start, end

    taken, used, next_month, rows, start, end = run(go())
    assert sum(taken) == 25 and used == 25 and next_month == 0
    assert [(r["window_start"], r["expires_at"], r["hits"]) for r in rows] == [(start, end, 25)]


def test_the_api_still_cannot_set_the_counter_back():
    # B11 holds for the new scope too: the API only adds one.
    async def go():
        _, location = await account_with("gratis")
        scope = ai_replies_scope(location)
        start, end = month_window(NOW)
        async with Api() as store:
            assert await store.take_quota_in(scope, 25, start, end)
        conn = await asyncpg.connect(API_URL)
        try:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("UPDATE usage_counter SET hits = 0 WHERE scope = $1", scope)
        finally:
            await conn.close()

    run(go())


def test_the_plan_is_read_per_profile_and_only_for_the_account_of_the_session():
    async def go():
        ana, ana_loc = await account_with("gratis")
        luis, luis_loc = await account_with("pro")
        _, inactive = await account_with(None)
        async with Api() as store:
            return (
                await store.location_plan(ana, ana_loc),
                await store.location_plan(luis, luis_loc),
                await store.location_plan(ana, luis_loc),
                await store.location_plan(ana, inactive),
            )

    assert run(go()) == (Plan.GRATIS, Plan.PRO, None, None)
