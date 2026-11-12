"""Integration: the findings of docs/seguridad/revision-2026-11-12-b.md that live in the
database (M5, B9, B10, B11, I12 and the privileges the key dates gave the API), with
the four roles, against the TEST database only.

Skipped unless the four URLs are set. They must point to a database whose name ends in
`_test` (tests/conftest.py stops the run otherwise). Create it with
scripts/preparar_bd_pruebas.sh.

Every statement a test expects to be refused runs in a transaction that is always
rolled back: nothing stays, even if it were let in. Rows the tests need are written by
the owner and deleted at the end. The jobs they write run in the year 2100, so that no
process attached to this database takes them.
"""

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from app.adapters.conversation_store.postgres import PostgresConversationStore
from app.adapters.worker_store.postgres import PostgresWorkerStore
from app.domain import channel_jobs
from app.domain.offboarding import RetentionPolicy

API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")
WORKER_URL = os.environ.get("FICHAITOR_TEST_WORKER_URL")
CONVERSATION_URL = os.environ.get("FICHAITOR_TEST_CONVERSATION_URL")

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL and WORKER_URL and CONVERSATION_URL),
    reason="sin base de datos de pruebas configurada",
)

FAR = datetime(2100, 1, 1, tzinfo=UTC)
PREFIX = "pytest-sb-"


def run(coro):
    return asyncio.run(coro)


async def owner(sql: str, *args):
    conn = await asyncpg.connect(OWNER_URL)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def attempt(url: str, sql: str, *args, setup: tuple[str, ...] = ()):
    """Run a statement as the role of `url` in a transaction that is always rolled
    back. Returns its status; raises what the database raised."""
    conn = await asyncpg.connect(url)
    try:
        tx = conn.transaction()
        await tx.start()
        try:
            for statement in setup:
                await conn.execute(statement)
            return await conn.execute(sql, *args)
        finally:
            await tx.rollback()
    finally:
        await conn.close()


def refused(url: str, sql: str, *args, setup: tuple[str, ...] = ()) -> bool:
    try:
        run(attempt(url, sql, *args, setup=setup))
    except asyncpg.PostgresError:
        return True
    return False


async def cleanup():
    mine = f"(SELECT id FROM account WHERE google_sub LIKE '{PREFIX}%')"
    for sql in (
        f"UPDATE conversation SET active_proposal_id = NULL WHERE account_id IN {mine}",
        f"DELETE FROM conversation WHERE account_id IN {mine}",
        f"DELETE FROM proposal WHERE account_id IN {mine}",
        f"DELETE FROM action WHERE account_id IN {mine}",
        f"DELETE FROM offboarding WHERE account_id IN {mine}",
        f"DELETE FROM account WHERE id IN {mine}",
        f"DELETE FROM google_connection WHERE google_sub LIKE '{PREFIX}%'",
        f"DELETE FROM job WHERE payload->>'pytest' = '{PREFIX}'",
        "DELETE FROM usage_counter WHERE scope = 'pytest_sb'",
    ):
        await owner(sql)


@pytest.fixture(autouse=True)
def clean():
    run(cleanup())
    yield
    run(cleanup())


async def new_account(name: str) -> tuple[str, str]:
    """An account of its own. Returns (id, google_sub)."""
    sub = f"{PREFIX}{name}-{uuid.uuid4().hex[:8]}"
    rows = await owner(
        "INSERT INTO account (google_sub, email) VALUES ($1, $2) RETURNING id::text", sub, "x@x"
    )
    return rows[0]["id"], sub


async def new_job(kind: str, payload: dict | None = None, key: str | None = None) -> str:
    rows = await owner(
        "INSERT INTO job (kind, payload, run_at, idempotency_key) VALUES ($1, $2::jsonb, $3, $4)"
        " RETURNING id::text",
        kind,
        json.dumps({"pytest": PREFIX, **(payload or {})}),
        FAR,
        key,
    )
    return rows[0]["id"]


# =============================================================================
# M5: the API role cannot schedule a leave that is not the account's own, nor one
# shorter than the floor of 47 hours.
# =============================================================================

LEAVE = (
    "INSERT INTO offboarding (account_id, scope, google_sub, confirmed_at, delete_at)"
    " VALUES ($1::uuid, 'ficha', $2, now(), now() + make_interval(hours => $3))"
)


def test_m5_the_api_cannot_schedule_the_instant_leave_of_another_account():
    victim, victim_sub = run(new_account("victima"))
    attacker, _ = run(new_account("atacante"))
    # The attacker's account with the victim's Google user, effective at once.
    assert refused(API_URL, LEAVE, attacker, victim_sub, 0)
    # Even with a date far enough: the Google user is not the account's.
    assert refused(API_URL, LEAVE, attacker, victim_sub, 48)
    # The victim's own account, effective at once or in less than 47 hours.
    assert refused(API_URL, LEAVE, victim, victim_sub, 0)
    assert refused(API_URL, LEAVE, victim, victim_sub, 46)
    # A leave without a Google user would not close anything: refused too.
    assert refused(API_URL, LEAVE, victim, None, 48)
    # What the routes do is let in.
    assert run(attempt(API_URL, LEAVE, victim, victim_sub, 48)) == "INSERT 0 1"


def test_m5_the_floor_is_fixed_in_the_database_and_configuration_can_only_lengthen_it():
    victim, sub = run(new_account("suelo"))
    lower = ("SELECT set_config('fichaitor.offboarding_min_hours', '1', true)",)
    longer = ("SELECT set_config('fichaitor.offboarding_min_hours', '100', true)",)
    assert refused(API_URL, LEAVE, victim, sub, 46, setup=lower)
    assert refused(API_URL, LEAVE, victim, sub, 48, setup=longer)
    assert run(attempt(API_URL, LEAVE, victim, sub, 101, setup=longer)) == "INSERT 0 1"


def test_m5_once_scheduled_a_leave_does_not_change_owner_date_or_google_user():
    victim, sub = run(new_account("fila"))
    other, other_sub = run(new_account("otra"))
    rows = run(
        owner(
            "INSERT INTO offboarding (account_id, scope, google_sub, confirmed_at, delete_at)"
            " VALUES ($1::uuid, 'ficha', $2, now(), now() + interval '48 hours')"
            " RETURNING id::text",
            victim,
            sub,
        )
    )
    leave = rows[0]["id"]
    # The worker may only empty the Google user (when the data is deleted).
    assert refused(
        WORKER_URL, "UPDATE offboarding SET google_sub = $2 WHERE id = $1::uuid", leave, other_sub
    )
    assert (
        run(
            attempt(
                WORKER_URL, "UPDATE offboarding SET google_sub = NULL WHERE id = $1::uuid", leave
            )
        )
        == "UPDATE 1"
    )


def test_m5_the_worker_does_not_make_effective_a_leave_that_does_not_match_its_account():
    async def go():
        victim, victim_sub = await new_account("v")
        attacker, _ = await new_account("a")
        await owner(
            "INSERT INTO google_connection (google_sub, email, refresh_token_enc, key_version,"
            " scopes) VALUES ($1, 'x@x', 'sellado', 1, '{}')",
            victim_sub,
        )
        now = (await owner("SELECT now() AS t"))[0]["t"]
        # Written by the owner, as SQL that slipped past the API would leave it: the
        # attacker's account with the victim's Google user, and a leave of 1 hour.
        wrong_user, too_short = [
            (
                await owner(
                    "INSERT INTO offboarding (account_id, scope, google_sub, confirmed_at,"
                    " delete_at) VALUES ($1::uuid, 'ficha', $2, $3, $4) RETURNING id::text",
                    account,
                    victim_sub,
                    confirmed,
                    now - timedelta(hours=1),
                )
            )[0]["id"]
            for account, confirmed in (
                (attacker, now - timedelta(days=3)),
                (victim, now - timedelta(hours=2)),
            )
        ]
        pool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=2)
        try:
            store = PostgresWorkerStore(pool)
            begun = [await store.begin_offboarding(i, now) for i in (wrong_user, too_short)]
            due = {i for i, _ in await store.due_offboardings(now)}
        finally:
            await pool.close()
        tokens = await owner("SELECT 1 FROM google_connection WHERE google_sub = $1", victim_sub)
        accounts = await owner(
            "SELECT disconnected_at FROM account WHERE id = ANY($1::uuid[])", [victim, attacker]
        )
        effective = await owner(
            "SELECT effective_at FROM offboarding WHERE id = ANY($1::uuid[])",
            [wrong_user, too_short],
        )
        return begun, due, tokens, accounts, effective, {wrong_user, too_short}

    begun, due, tokens, accounts, effective, ids = run(go())
    assert begun == [None, None]
    assert not due & ids  # not even offered to the retention pass
    assert len(tokens) == 1
    assert all(r["disconnected_at"] is None for r in accounts)
    assert all(r["effective_at"] is None for r in effective)


# =============================================================================
# B9 and B10: who may see, take and enqueue which jobs, and with what key.
# =============================================================================

API_INSERTS = [
    ("reply_review", "reply_review:act_0011223344556677"),
    ("set_special_hours", "set_special_hours:act_0011223344556677"),
    ("send_mail", None),
    ("apply_offboarding", f"apply_offboarding:{uuid.uuid4()}"),
    ("sync_profile", "sync_profile:abc:123"),
    ("sync_profile", "sync_profile:reconnect:abc:123"),
    (channel_jobs.CHANNEL_INBOUND, channel_jobs.inbound_key("telegram", 123456789)),
]
CONVERSATION_INSERTS = [
    ("reply_review", "reply_review:act_0011223344556677"),
    ("set_special_hours", "set_special_hours:act_0011223344556677"),
    (channel_jobs.CONVERSATION_TICK, "conversation_tick:6000000"),
    ("sync_profile", channel_jobs.sync_key("pytest-sb-x", "locations/1", "d123")),
    (channel_jobs.PLAN_KEY_DATES, channel_jobs.plan_key_dates_key("2027-01-01")),
]
INSERT_JOB = (
    "INSERT INTO job (kind, payload, run_at, idempotency_key) VALUES ($1, '{}'::jsonb, $2, $3)"
)


@pytest.mark.parametrize(("kind", "key"), API_INSERTS)
def test_b9_the_api_still_enqueues_every_job_it_enqueues(kind, key):
    assert run(attempt(API_URL, INSERT_JOB, kind, FAR, key)) == "INSERT 0 1"


@pytest.mark.parametrize(("kind", "key"), CONVERSATION_INSERTS)
def test_b9_the_conversation_still_enqueues_every_job_it_enqueues(kind, key):
    assert run(attempt(CONVERSATION_URL, INSERT_JOB, kind, FAR, key)) == "INSERT 0 1"


@pytest.mark.parametrize(
    ("url", "kind", "key"),
    [
        # Kinds the role never enqueues.
        (CONVERSATION_URL, "send_mail", None),
        (CONVERSATION_URL, "apply_offboarding", None),
        (CONVERSATION_URL, "retention", None),
        (CONVERSATION_URL, channel_jobs.CHANNEL_ACTION_RESULT, None),
        (CONVERSATION_URL, channel_jobs.PROPOSE_REVIEW, None),
        (API_URL, "retention", None),
        (API_URL, channel_jobs.CONVERSATION_TICK, None),
        (API_URL, channel_jobs.PROPOSE_REVIEW, None),
        (API_URL, channel_jobs.CHANNEL_ACTION_RESULT, None),
        (API_URL, "analyze_sentiment", None),
        (API_URL, "nada", None),
        # B10: the daily keys of the system, under a kind the role may enqueue.
        (API_URL, "send_mail", "retention:2027-01-01"),
        (API_URL, "reply_review", "retention:2027-01-01"),
        (API_URL, "sync_profile", "plan_key_dates:2027-01-01"),
        (API_URL, channel_jobs.CHANNEL_INBOUND, "conversation_tick:6000000"),
        (CONVERSATION_URL, "reply_review", "retention:2027-01-01"),
        (CONVERSATION_URL, "sync_profile", "retention:2027-01-01"),
        (CONVERSATION_URL, channel_jobs.CONVERSATION_TICK, "retention:2027-01-01"),
    ],
)
def test_b9_b10_a_role_cannot_enqueue_kinds_or_keys_that_are_not_its_own(url, kind, key):
    assert refused(url, INSERT_JOB, kind, FAR, key)


def test_b10_the_worker_enqueues_its_daily_job():
    assert run(attempt(WORKER_URL, INSERT_JOB, "retention", FAR, "retention:2100-01-01")) == (
        "INSERT 0 1"
    )


def test_b9_the_conversation_never_reads_nor_changes_an_email_job():
    async def go():
        mail = await new_job("send_mail", {"to": "dueno@example.com", "code": "123456"})
        inbound = await new_job(channel_jobs.CHANNEL_INBOUND, {"update": {}})
        conn = await asyncpg.connect(CONVERSATION_URL)
        try:
            visible = await conn.fetchval(
                "SELECT count(*) FROM job WHERE id = ANY($1::uuid[])", [mail, inbound]
            )
            emptied = await conn.execute(
                "UPDATE job SET payload = '{}'::jsonb WHERE id = $1::uuid", mail
            )
            closed = await conn.execute(
                "UPDATE job SET state = 'done' WHERE kind = 'send_mail' AND id = $1::uuid", mail
            )
        finally:
            await conn.close()
        payload = (
            await owner("SELECT payload->>'code' AS c, state FROM job WHERE id = $1::uuid", mail)
        )[0]
        return visible, emptied, closed, payload

    visible, emptied, closed, payload = run(go())
    assert visible == 1  # its own job; not the email
    assert emptied == closed == "UPDATE 0"
    assert payload["c"] == "123456" and payload["state"] == "pending"


def test_b9_the_conversation_cannot_move_its_jobs_to_another_kind():
    inbound = run(new_job(channel_jobs.CHANNEL_INBOUND))
    assert refused(
        CONVERSATION_URL, "UPDATE job SET kind = 'send_mail' WHERE id = $1::uuid", inbound
    )


def test_b9_the_worker_still_sees_and_takes_every_kind():
    async def go():
        mail = await new_job("send_mail")
        inbound = await new_job(channel_jobs.CHANNEL_INBOUND)
        conn = await asyncpg.connect(WORKER_URL)
        try:
            return await conn.fetchval(
                "SELECT count(*) FROM job WHERE id = ANY($1::uuid[])", [mail, inbound]
            )
        finally:
            await conn.close()

    assert run(go()) == 2


# =============================================================================
# B10: a warning when the daily pass has not run for 36 hours.
# =============================================================================


def test_b10_the_worker_reads_when_the_last_retention_pass_ran():
    async def go():
        pool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=1)
        try:
            return await PostgresWorkerStore(pool).last_retention_at()
        finally:
            await pool.close()

    last = run(go())
    assert last is None or isinstance(last, datetime)


# =============================================================================
# B11: the caps on paid calls only ever go up by one.
# =============================================================================


@pytest.fixture
def counter():
    run(
        owner(
            "INSERT INTO usage_counter (scope, window_start, expires_at, hits)"
            " VALUES ('pytest_sb', now(), now() + interval '1 day', 3)"
        )
    )


@pytest.mark.parametrize("url", [API_URL, CONVERSATION_URL])
@pytest.mark.parametrize(
    "change",
    ["hits = 0", "hits = hits - 1", "hits = hits + 2", "hits = 1000000"],
)
def test_b11_nobody_sets_a_cap_back_or_jumps_it(counter, url, change):
    assert refused(url, f"UPDATE usage_counter SET {change} WHERE scope = 'pytest_sb'")  # noqa: S608


@pytest.mark.parametrize("url", [API_URL, CONVERSATION_URL])
def test_b11_counting_one_more_still_works(counter, url):
    assert (
        run(attempt(url, "UPDATE usage_counter SET hits = hits + 1 WHERE scope = 'pytest_sb'"))
        == "UPDATE 1"
    )


@pytest.mark.parametrize("url", [API_URL, CONVERSATION_URL])
def test_b11_a_new_counter_starts_at_one(url):
    sql = (
        "INSERT INTO usage_counter (scope, window_start, expires_at, hits)"
        " VALUES ('pytest_sb', now(), now() + interval '1 day', $1)"
    )
    assert refused(url, sql, 0)
    assert run(attempt(url, sql, 1)) == "INSERT 0 1"


# =============================================================================
# Key dates: what the API may change in proposals and threads.
# =============================================================================


async def _proposals(account: str) -> dict[str, str]:
    ids = {}
    for kind in ("review_reply", "key_date"):
        pid = f"prop_{uuid.uuid4().hex[:16]}"
        await owner(
            "INSERT INTO proposal (id, account_id, type, source_ref, state)"
            " VALUES ($1, $2::uuid, $3, $4, 'enviada')",
            pid,
            account,
            kind,
            f"pytest:{kind}",
        )
        ids[kind] = pid
    await owner(
        "INSERT INTO conversation (account_id, active_proposal_id) VALUES ($1::uuid, $2)",
        account,
        ids["review_reply"],
    )
    return ids


def test_key_dates_the_api_cannot_touch_a_review_proposal_of_any_account():
    account, _ = run(new_account("prop"))
    ids = run(_proposals(account))
    for change in (
        'slots = \'{"text": "otro"}\'::jsonb',
        "state = 'por_confirmar'",
        "action_id = 'act_0011223344556677'",
    ):
        assert refused(
            API_URL,
            f"UPDATE proposal SET {change} WHERE id = $1",  # noqa: S608
            ids["review_reply"],
        )
    # A key date is what the panel answers.
    assert (
        run(
            attempt(
                API_URL, "UPDATE proposal SET state = 'descartada' WHERE id = $1", ids["key_date"]
            )
        )
        == "UPDATE 1"
    )


def test_key_dates_the_api_only_releases_a_thread_and_never_points_it_elsewhere():
    account, _ = run(new_account("hilo"))
    ids = run(_proposals(account))
    point = "UPDATE conversation SET active_proposal_id = $2 WHERE account_id = $1::uuid"
    assert refused(API_URL, point, account, ids["key_date"])
    release = "UPDATE conversation SET active_proposal_id = NULL WHERE account_id = $1::uuid"
    assert run(attempt(API_URL, release, account)) == "UPDATE 1"


def test_b12_the_api_cancels_the_open_review_proposals_of_a_leave_and_nothing_else():
    account, _ = run(new_account("baja"))
    ids = run(_proposals(account))
    cancel = "UPDATE proposal SET state = 'cancelada', outcome_reason = 'offboarding' WHERE id = $1"
    assert run(attempt(API_URL, cancel, ids["review_reply"])) == "UPDATE 1"
    other = "UPDATE proposal SET state = 'cancelada', outcome_reason = 'expired' WHERE id = $1"
    assert refused(API_URL, other, ids["review_reply"])


def test_b12_the_conversation_knows_an_account_on_leave():
    async def go():
        account, sub = await new_account("conv")
        pool = await asyncpg.create_pool(CONVERSATION_URL, min_size=1, max_size=1)
        try:
            store = PostgresConversationStore(pool)
            before = await store.on_leave(account)
            leave = (
                await owner(
                    "INSERT INTO offboarding (account_id, scope, google_sub, confirmed_at,"
                    " delete_at) VALUES ($1::uuid, 'ficha', $2, now(), now() + interval '2 days')"
                    " RETURNING id",
                    account,
                    sub,
                )
            )[0]["id"]
            await owner(
                "INSERT INTO proposal (id, account_id, type, source_ref, state)"
                " VALUES ($1, $2::uuid, 'key_date', 'pytest:x', 'programada')",
                f"prop_{uuid.uuid4().hex[:16]}",
                account,
            )
            during = await store.on_leave(account)
            working = account in await store.accounts_with_open_work()
            await owner("UPDATE offboarding SET restored_at = now() WHERE id = $1", leave)
            after = await store.on_leave(account)
            back = account in await store.accounts_with_open_work()
        finally:
            await pool.close()
        return before, during, working, after, back

    assert run(go()) == (False, True, False, False, True)


# =============================================================================
# I12: finished jobs do not stay for ever.
# =============================================================================


def test_i12_retention_deletes_finished_jobs_older_than_the_term():
    async def go():
        now = (await owner("SELECT now() AS t"))[0]["t"]
        rows = {}
        for name, state, age in (
            ("old_done", "done", 15),
            ("old_failed", "failed", 20),
            ("recent_done", "done", 13),
            ("old_pending", "pending", 30),
            ("old_running", "running", 30),
        ):
            rows[name] = (
                await owner(
                    "INSERT INTO job (kind, payload, state, created_at, finished_at, run_at)"
                    " VALUES ('pytest-sb-old', $1::jsonb, $2, $3, $4, $5) RETURNING id::text",
                    json.dumps({"pytest": PREFIX}),
                    state,
                    now - timedelta(days=age),
                    now - timedelta(days=age) if state in ("done", "failed") else None,
                    FAR,
                )
            )[0]["id"]
        # An old job that carried out an action is the trail of who ran it: it stays
        # while the action stays.
        rows["action_job"] = (
            await owner(
                "INSERT INTO job (kind, payload, state, created_at, finished_at)"
                " VALUES ('pytest-sb-old', $1::jsonb, 'done', $2, $2) RETURNING id::text",
                json.dumps({"pytest": PREFIX}),
                now - timedelta(days=40),
            )
        )[0]["id"]
        account, sub = await new_account("trail")
        await owner(
            "INSERT INTO action (account_id, type, google_sub, location, review_id, text, state,"
            " job_id) VALUES ($1::uuid, 'reply_review', $2, 'locations/1', 'rev_00112233',"
            " 'Gracias.', 'done', $3)",
            account,
            sub,
            rows["action_job"],
        )
        pool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=2)
        try:
            counts = await PostgresWorkerStore(pool).run_retention(now, RetentionPolicy())
        finally:
            await pool.close()
        left = {
            r["id"]
            for r in await owner(
                "SELECT id::text AS id FROM job WHERE id = ANY($1::uuid[])", list(rows.values())
            )
        }
        await owner("DELETE FROM action WHERE account_id = $1::uuid", account)
        return counts, {name for name, i in rows.items() if i in left}

    counts, kept = run(go())
    assert kept == {"recent_done", "old_pending", "old_running", "action_job"}
    assert counts["job_finished"] >= 2
