"""Integration: the limits of the free plan against PostgreSQL (TEST database only).

- The 25 replies drafted by the model of the free plan live in free_allowance, once and
  for ever per Google profile, under an HMAC of its id (never the id itself, the account
  or an email). Taken in one statement, so drafts at once never go over 25; only +1 at a
  time (B11); deleting the account and connecting the same profile from another account
  gives nothing back; the worker deletes a row only 3 years after its last use.
- `location_plan` reads the plan of an active profile of the account of the session, and
  nothing of another account.

Skipped unless the four URLs are set (tests/conftest.py checks the name of the database).
"""

import asyncio
import os
import uuid

import asyncpg
import pytest

from app.adapters.store.postgres import PostgresStore
from app.domain.plans import Plan
from app.domain.quota import allowance_key

API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")
WORKER_URL = os.environ.get("FICHAITOR_TEST_WORKER_URL")
CONVERSATION_URL = os.environ.get("FICHAITOR_TEST_CONVERSATION_URL")

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL and WORKER_URL and CONVERSATION_URL),
    reason="sin base de datos de pruebas configurada",
)

PREFIX = "pytest-plan-"
PEPPER = "pimienta-de-pruebas-de-32-caracteres"
GBP_LOCATIONS: list[str] = []


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
    keys = [allowance_key(PEPPER, g) for g in GBP_LOCATIONS]
    await owner("DELETE FROM free_allowance WHERE key = ANY($1::text[])", keys)
    for sql in (
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


async def account_with(plan: str | None, gbp: str | None = None) -> tuple[str, str]:
    sub = f"{PREFIX}{uuid.uuid4().hex[:8]}"
    gbp = gbp or f"locations/{sub}"
    GBP_LOCATIONS.append(gbp)
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
        gbp,
    )
    if plan is not None:
        await owner(
            "UPDATE location SET active_since = now(), plan = $2 WHERE id = $1", location, plan
        )
    return account, location


class Api:
    def __init__(self, url: str | None = None) -> None:
        self.url = url or API_URL

    async def __aenter__(self) -> PostgresStore:
        self.store = PostgresStore(self.url, allowance_pepper=PEPPER)
        await self.store.connect()
        return self.store

    async def __aexit__(self, *exc) -> None:
        await self.store.close()


def test_the_free_allowance_stops_at_25_even_with_drafts_at_once():
    async def go():
        account, location = await account_with("gratis")
        async with Api() as store:
            taken = await asyncio.gather(
                *(store.take_ai_reply(account, location, 25) for _ in range(40))
            )
            used = await store.ai_replies_used(account, location)
        rows = await owner(
            "SELECT used FROM free_allowance WHERE key = $1",
            allowance_key(PEPPER, GBP_LOCATIONS[-1]),
        )
        return taken, used, rows

    taken, used, rows = run(go())
    assert sum(taken) == 25 and used == 25
    assert [r["used"] for r in rows] == [25]


def test_the_conversation_counts_on_the_same_allowance():
    async def go():
        account, location = await account_with("gratis")
        async with Api() as api, Api(CONVERSATION_URL) as conversation:
            assert await api.take_ai_reply(account, location, 25)
            assert await conversation.take_ai_reply(account, location, 25)
            return await api.ai_replies_used(account, location)

    assert run(go()) == 2


def test_the_table_keeps_no_profile_id_account_nor_email():
    async def go():
        account, location = await account_with("gratis")
        async with Api() as store:
            assert await store.take_ai_reply(account, location, 25)
        columns = await owner(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'free_allowance' ORDER BY ordinal_position"
        )
        rows = await owner(
            "SELECT * FROM free_allowance WHERE key = $1",
            allowance_key(PEPPER, GBP_LOCATIONS[-1]),
        )
        return [c["column_name"] for c in columns], dict(rows[0])

    columns, row = run(go())
    assert columns == ["key", "used", "first_used_at", "last_used_at"]
    assert len(row["key"]) == 64 and "locations" not in row["key"]


def test_deleting_the_account_and_connecting_from_another_gives_nothing_back():
    async def go():
        gbp = f"locations/{PREFIX}shared{uuid.uuid4().hex[:6]}"
        first, first_loc = await account_with("gratis", gbp)
        async with Api() as store:
            for _ in range(25):
                assert await store.take_ai_reply(first, first_loc, 25)
        # The account is deleted (a tombstone, as the worker leaves it) and its profile
        # gone with the retention: free_allowance is not touched.
        await owner("DELETE FROM location_activation_log WHERE location_id = $1", first_loc)
        await owner("DELETE FROM location WHERE id = $1", first_loc)
        await owner("UPDATE account SET deleted_at = now() WHERE id = $1::uuid", first)
        second, second_loc = await account_with("gratis", gbp)
        async with Api() as store:
            used = await store.ai_replies_used(second, second_loc)
            again = await store.take_ai_reply(second, second_loc, 25)
        return used, again

    assert run(go()) == (25, False)


def test_the_api_and_the_conversation_only_add_one():
    # B11 holds for the free allowance too: nobody but the owner sets it back or forward.
    async def go():
        account, location = await account_with("gratis")
        async with Api() as store:
            assert await store.take_ai_reply(account, location, 25)
        key = allowance_key(PEPPER, GBP_LOCATIONS[-1])
        results = []
        for url in (API_URL, CONVERSATION_URL):
            conn = await asyncpg.connect(url)
            try:
                for sql in (
                    "UPDATE free_allowance SET used = 0 WHERE key = $1",
                    "UPDATE free_allowance SET used = used + 2 WHERE key = $1",
                    "DELETE FROM free_allowance WHERE key = $1",
                ):
                    try:
                        await conn.execute(sql, key)
                        results.append("ok")
                    except asyncpg.PostgresError as exc:
                        results.append(type(exc).__name__)
            finally:
                await conn.close()
        return results

    assert (
        run(go())
        == [
            "CheckViolationError",
            "CheckViolationError",
            "InsufficientPrivilegeError",
        ]
        * 2
    )


def test_the_worker_deletes_a_row_only_three_years_after_its_last_use():
    async def go():
        account, location = await account_with("gratis")
        async with Api() as store:
            assert await store.take_ai_reply(account, location, 25)
        key = allowance_key(PEPPER, GBP_LOCATIONS[-1])
        conn = await asyncpg.connect(WORKER_URL)
        try:
            # As the retention does: by the date of the last use only (the worker reads no
            # key). A row used less than 3 years ago is refused by the database itself.
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(
                    "DELETE FROM free_allowance WHERE last_used_at > now() - interval '1 hour'"
                )
            await owner(
                "UPDATE free_allowance SET last_used_at = now() - interval '3 years 1 day'"
                " WHERE key = $1",
                key,
            )
            return await conn.execute(
                "DELETE FROM free_allowance WHERE last_used_at <= now() - interval '3 years'"
            )
        finally:
            await conn.close()

    assert run(go()) == "DELETE 1"


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
