"""Integration: the findings of docs/seguridad/revision-2026-11-12-c.md that live in the
database (M6, B14, B16, I16 and I17), with the four roles, against the TEST database only.

Skipped unless the four URLs are set. They must point to a database whose name ends in
`_test` (tests/conftest.py stops the run otherwise). Create it with
scripts/preparar_bd_pruebas.sh.

M6 is tested in two layers:
- The roles of the application cannot create a temporary table at all (REVOKE TEMPORARY).
- The proofs of concept of the report, with TEMPORARY granted back inside a transaction of
  the owner that is always rolled back, run with SET LOCAL ROLE as the API: every guard
  still holds, because every function fixes `search_path = pg_catalog, public, pg_temp`.

Every attack runs in a transaction that is always rolled back. Rows the tests need are
written by the owner and deleted at the end. Jobs run in the year 2100, so that no
process attached to this database takes them.
"""

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from app.adapters.worker_store.postgres import PostgresWorkerStore

API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")
WORKER_URL = os.environ.get("FICHAITOR_TEST_WORKER_URL")
CONVERSATION_URL = os.environ.get("FICHAITOR_TEST_CONVERSATION_URL")

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL and WORKER_URL and CONVERSATION_URL),
    reason="sin base de datos de pruebas configurada",
)

FAR = datetime(2100, 1, 1, tzinfo=UTC)
PREFIX = "pytest-sc-"
API_LOGIN = "fichaitor_api_dev"


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
        f"UPDATE account SET selected_location_id = NULL WHERE id IN {mine}",
        f"DELETE FROM offboarding WHERE account_id IN {mine}",
        f"DELETE FROM location_activation_log WHERE account_id IN {mine}",
        f"DELETE FROM location WHERE account_id IN {mine}",
        f"DELETE FROM plan_entitlement WHERE account_id IN {mine}",
        f"DELETE FROM account WHERE id IN {mine}",
        f"DELETE FROM google_connection WHERE google_sub LIKE '{PREFIX}%'",
        f"DELETE FROM job WHERE payload->>'pytest' = '{PREFIX}'",
        "DELETE FROM usage_counter WHERE scope = 'pytest_sc'",
    ):
        await owner(sql)


@pytest.fixture(autouse=True)
def clean():
    run(cleanup())
    yield
    run(cleanup())


async def new_account(name: str) -> tuple[str, str]:
    """An account of its own, with the beta entitlement (3 Pro). Returns (id, google_sub)."""
    sub = f"{PREFIX}{name}-{uuid.uuid4().hex[:8]}"
    rows = await owner(
        "INSERT INTO account (google_sub, email) VALUES ($1, $2) RETURNING id::text", sub, "x@x"
    )
    return rows[0]["id"], sub


async def new_location(account: str) -> str:
    rows = await owner(
        "INSERT INTO location (account_id, gbp_account, gbp_location, title)"
        " VALUES ($1::uuid, 'accounts/1', $2, 'Bar') RETURNING id",
        account,
        f"locations/{PREFIX}{uuid.uuid4().hex[:10]}",
    )
    return rows[0]["id"]


async def as_attacker(setup: tuple[str, ...], attack, *, role: str = API_LOGIN):
    """The proofs of concept of M6 as `role`, WITH the TEMPORARY privilege it no longer
    has: granted inside a transaction of the owner and rolled back with it. `attack`
    receives the connection after the setup (temporary tables) and returns what the test
    checks; a refusal of the database is raised. Nothing stays."""
    conn = await asyncpg.connect(OWNER_URL)
    try:
        tx = conn.transaction()
        await tx.start()
        try:
            database = await conn.fetchval("SELECT current_database()")
            await conn.execute(f'GRANT TEMPORARY ON DATABASE "{database}" TO {role}')
            await conn.execute(f"SET LOCAL ROLE {role}")
            for statement in setup:
                await conn.execute(statement)
            return await attack(conn)
        finally:
            await tx.rollback()
    finally:
        await conn.close()


FAKE_SUPERUSER = (
    "CREATE TEMP TABLE pg_roles (rolname name, rolsuper boolean, oid oid)",
    "INSERT INTO pg_roles VALUES (current_user, true, 0)",
)


# =============================================================================
# M6, first layer: no role of the application can create a temporary table.
# =============================================================================


@pytest.mark.parametrize("variable", ["API_URL", "WORKER_URL", "CONVERSATION_URL"])
def test_m6_the_roles_of_the_application_cannot_create_temporary_tables(variable):
    url = globals()[variable]

    async def go():
        conn = await asyncpg.connect(url)
        try:
            await conn.execute("CREATE TEMP TABLE pg_roles (rolname name, rolsuper boolean)")
        finally:
            await conn.close()

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        run(go())


def test_m6_public_has_no_temporary_privilege_on_the_database():
    rows = run(
        owner(
            """
            SELECT has_database_privilege(r, current_database(), 'TEMPORARY') AS temp
            FROM unnest(ARRAY['fichaitor_api', 'fichaitor_worker', 'fichaitor_conversation',
                              'fichaitor_api_dev', 'fichaitor_worker_dev',
                              'fichaitor_conversation_dev']) AS r
            """
        )
    )
    assert [r["temp"] for r in rows] == [False] * 6


def test_m6_every_function_of_the_schema_fixes_its_search_path_with_pg_temp_last():
    rows = run(
        owner(
            """
            SELECT p.proname, p.proconfig FROM pg_proc p
            WHERE p.pronamespace = 'public'::regnamespace
              AND NOT EXISTS (SELECT 1 FROM pg_depend d
                              WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid
                                AND d.deptype = 'e')
            ORDER BY p.proname
            """
        )
    )
    names = {r["proname"] for r in rows}
    # The ones the report names, and the rest: nothing escapes the rule.
    assert {
        "fichaitor_is_owner",
        "job_key_fits",
        "offboarding_guard",
        "usage_counter_guard",
        "google_connection_guard",
        "location_active_guard",
        "location_activation_audit",
        "account_beta_entitlement",
        "location_filter_guard",
        "proposal_location_guard",
        "fichaitor_fill_conversation_lanes",
        "account_baja_guard",
    } <= names
    wrong = [
        r["proname"]
        for r in rows
        if "search_path=pg_catalog, public, pg_temp" not in (r["proconfig"] or [])
    ]
    assert wrong == []


# =============================================================================
# M6, second layer: the proofs of concept of the report, with TEMPORARY granted back.
# =============================================================================


def test_m6_poc_a_fake_pg_roles_does_not_buy_the_instant_leave_of_another_account():
    victim, victim_sub = run(new_account("victima"))

    async def attack(conn):
        # M5 reopened in the report: the victim's own leave, confirmed 48 hours ago and
        # effective now, which also passes _COHERENT_LEAVE of the worker.
        return await conn.execute(
            "INSERT INTO offboarding (account_id, scope, google_sub, confirmed_at, delete_at)"
            " VALUES ($1::uuid, 'ficha', $2, now() - interval '48 hours', now())",
            victim,
            victim_sub,
        )

    with pytest.raises(asyncpg.CheckViolationError):
        run(as_attacker(FAKE_SUPERUSER, attack))


def test_m6_poc_a_fake_pg_roles_does_not_put_a_counter_back_to_zero():
    window = datetime.now(UTC).replace(microsecond=0)
    run(
        owner(
            "INSERT INTO usage_counter (scope, window_start, expires_at, hits)"
            " VALUES ('pytest_sc', $1, $2, 7)",
            window,
            window + timedelta(days=1),
        )
    )

    async def attack(conn):
        return await conn.execute("UPDATE usage_counter SET hits = 0 WHERE scope = 'pytest_sc'")

    with pytest.raises(asyncpg.CheckViolationError):
        run(as_attacker(FAKE_SUPERUSER, attack))


ACTIVATE_PRO = "UPDATE location SET active_since = now(), plan = 'pro' WHERE id = $1"


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param(FAKE_SUPERUSER, id="pg_roles"),
        pytest.param(
            (
                "CREATE TEMP TABLE plan_entitlement (account_id uuid, plan text, quantity int,"
                " starts_at timestamptz, ends_at timestamptz)",
                "INSERT INTO plan_entitlement SELECT a.id, 'pro', 100, now() - interval '1 day',"
                " NULL FROM account a WHERE a.google_sub LIKE 'pytest-sc-%'",
            ),
            id="plan_entitlement",
        ),
    ],
)
def test_m6_poc_no_more_pro_profiles_than_entitlements(setup):
    account, _ = run(new_account("pro"))
    locations = [run(new_location(account)) for _ in range(5)]

    async def attack(conn):
        for location in locations:
            await conn.execute(ACTIVATE_PRO, location)

    # The beta gives 3: the fourth is refused.
    with pytest.raises(asyncpg.CheckViolationError, match="limite_fichas"):
        run(as_attacker(setup, attack))


def test_m6_poc_a_temporary_log_does_not_swallow_the_activation():
    account, _ = run(new_account("registro"))
    location = run(new_location(account))

    async def attack(conn):
        await conn.execute(
            "UPDATE location SET active_since = now(), plan = 'gratis' WHERE id = $1", location
        )
        temporary = await conn.fetchval("SELECT count(*) FROM pg_temp.location_activation_log")
        await conn.execute("RESET ROLE")
        real = await conn.fetchval(
            "SELECT count(*) FROM public.location_activation_log WHERE location_id = $1",
            location,
        )
        return temporary, real

    setup = (
        "CREATE TEMP TABLE location_activation_log (written_by name, account_id uuid,"
        " location_id text, event text, plan text, reason text)",
    )
    assert run(as_attacker(setup, attack)) == (0, 1)


# =============================================================================
# B16: the reason of the activation log is only taken from the owner.
# =============================================================================


async def _activate_with_reason(url: str, location: str, reason: str | None) -> None:
    conn = await asyncpg.connect(url)
    try:
        async with conn.transaction():
            if reason is not None:
                await conn.execute(
                    "SELECT set_config('fichaitor.activation_reason', $1, true)", reason
                )
            await conn.execute(
                "UPDATE location SET active_since = now(), plan = 'gratis' WHERE id = $1",
                location,
            )
    finally:
        await conn.close()


async def _reasons(location: str) -> list[tuple[str, str]]:
    rows = await owner(
        "SELECT event, reason FROM location_activation_log WHERE location_id = $1 ORDER BY id",
        location,
    )
    return [(r["event"], r["reason"]) for r in rows]


def test_b16_the_reason_asked_for_by_the_api_is_recorded_as_untrusted():
    account, _ = run(new_account("motivo"))
    asked, plain, by_owner = (run(new_location(account)) for _ in range(3))

    run(_activate_with_reason(API_URL, asked, "migration"))
    run(owner("UPDATE location SET active_since = NULL, plan = NULL WHERE id = $1", asked))
    run(_activate_with_reason(API_URL, plain, None))
    run(owner("UPDATE location SET active_since = NULL, plan = NULL WHERE id = $1", plain))
    run(_activate_with_reason(OWNER_URL, by_owner, "migration"))

    assert run(_reasons(asked))[0] == ("activated", "untrusted")
    assert run(_reasons(plain))[0] == ("activated", "owner")
    assert run(_reasons(by_owner)) == [("activated", "migration")]


# =============================================================================
# B14: a junk connection from the API cannot make the worker delete the real one.
# =============================================================================


def test_b14_the_api_cannot_choose_the_date_of_a_connection():
    async def go():
        conn = await asyncpg.connect(API_URL)
        try:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO google_connection (connected_at, google_sub, email,"
                    " refresh_token_enc, key_version, scopes)"
                    " VALUES ('2099-01-01', $1, 'x@x', 'junk', 1, '{}')",
                    f"{PREFIX}fecha",
                )
        finally:
            await conn.close()

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        run(go())


def test_b14_a_junk_connection_that_fails_takes_only_itself_with_it():
    sub = f"{PREFIX}b14-{uuid.uuid4().hex[:8]}"

    async def go():
        real = (
            await owner(
                "INSERT INTO google_connection (connected_at, google_sub, email,"
                " refresh_token_enc, key_version, scopes)"
                " VALUES (now() - interval '1 day', $1, 'x@x', 'real', 1, '{}')"
                " RETURNING id::text",
                sub,
            )
        )[0]["id"]
        # The attack: the API stores a newer connection for the victim, with a token that
        # Google will refuse (invalid_grant).
        api = await asyncpg.connect(API_URL)
        try:
            await api.execute(
                "INSERT INTO google_connection (google_sub, email, refresh_token_enc,"
                " key_version, scopes) VALUES ($1, 'x@x', 'junk', 1, '{}')",
                sub,
            )
        finally:
            await api.close()
        pool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=2)
        try:
            store = PostgresWorkerStore(pool)
            junk = await store.latest_connection(sub)
            orphaned = await store.revoke_connection(sub, junk.connection_id, datetime.now(UTC))
            after = await store.latest_connection(sub)
        finally:
            await pool.close()
        rows = await owner(
            "SELECT id::text AS id, revoked_at IS NOT NULL AS revoked FROM google_connection"
            " WHERE google_sub = $1 ORDER BY connected_at",
            sub,
        )
        return real, junk.connection_id, orphaned, after.connection_id, rows

    real, junk, orphaned, after, rows = run(go())
    assert junk != real
    # Only the one that failed falls; the real one is used again and no countdown starts.
    assert orphaned is False
    assert after == real
    assert [(r["id"], r["revoked"]) for r in rows] == [(real, False), (junk, True)]


# =============================================================================
# I16: the API cannot take the keys of the periodic syncs.
# =============================================================================


def _enqueue(url: str, kind: str, key: str):
    async def go():
        conn = await asyncpg.connect(url)
        try:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO job (kind, payload, run_at, idempotency_key)"
                    " VALUES ($1, $2::jsonb, $3, $4)",
                    kind,
                    json.dumps({"pytest": PREFIX}),
                    FAR,
                    key,
                )
                raise _RolledBack
        finally:
            await conn.close()

    try:
        run(go())
    except _RolledBack:
        return "ok"
    except asyncpg.PostgresError as exc:
        return type(exc).__name__


class _RolledBack(Exception):
    pass


def test_i16_the_keys_of_the_planner_are_not_the_apis():
    planner_key = f"sync:{PREFIX}sub:locations/1:2100-01-01T10:00"
    assert _enqueue(API_URL, "sync_profile", planner_key) == "InsufficientPrivilegeError"
    # The keys the API does use, and the planner's own, still go in.
    assert _enqueue(API_URL, "sync_profile", f"sync_profile:{PREFIX}abc:1") == "ok"
    assert _enqueue(CONVERSATION_URL, "sync_profile", planner_key) == "ok"


# =============================================================================
# I17: a connection stored during the pass of day 30 cancels it.
# =============================================================================


def test_i17_a_reconnection_during_the_pass_of_day_30_cancels_it():
    account, sub = run(new_account("i17"))
    location = run(new_location(account))
    now = datetime.now(UTC)

    async def go():
        await owner(
            "INSERT INTO google_connection (connected_at, google_sub, email,"
            " refresh_token_enc, key_version, scopes, revoked_at)"
            " VALUES ($2, $1, 'x@x', NULL, 1, '{}', $2)",
            sub,
            now - timedelta(days=40),
        )
        # The owner connects again, as the OAuth callback does: the connection, then the
        # account (which takes the row lock). Here the account first, inside a
        # transaction that is still open while the pass runs.
        api = await asyncpg.connect(API_URL)
        pool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=2)
        try:
            tx = api.transaction()
            await tx.start()
            await api.execute(
                "INSERT INTO google_connection (google_sub, email, refresh_token_enc,"
                " key_version, scopes) VALUES ($1, 'x@x', 'new', 1, '{}')",
                sub,
            )
            await api.execute(
                "UPDATE account SET disconnected_at = NULL WHERE google_sub = $1", sub
            )
            store = PostgresWorkerStore(pool)
            purge = asyncio.create_task(store._purge_revoked(sub, now, 30))
            await asyncio.sleep(0.5)
            await tx.commit()
            counts = await asyncio.wait_for(purge, 10)
        finally:
            await pool.close()
            await api.close()
        state = await owner(
            "SELECT a.disconnected_at, l.removed_at FROM account a"
            " JOIN location l ON l.account_id = a.id WHERE a.id = $1::uuid AND l.id = $2",
            account,
            location,
        )
        return counts, state

    counts, state = run(go())
    assert counts == {}
    assert len(state) == 1
    assert state[0]["disconnected_at"] is None and state[0]["removed_at"] is None
