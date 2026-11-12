"""Integration: the schema of multinegocio (docs/multinegocio.md, sections 3, 4 and 15)
with the four roles, against the TEST database only.

Skipped unless the four URLs are set. They must point to a database whose name ends in
`_test` (tests/conftest.py stops the run otherwise). Create it with
scripts/preparar_bd_pruebas.sh.

Every statement a test expects to be refused carries `WHERE false` or runs in a
transaction that is always rolled back. Rows the tests need are written by the owner
and deleted at the end.

The backfill test applies db/schema.sql, db/schema_worker.sql and db/schema_channels.sql
twice, in that order, on this test database with rows written as the old code left
them (an account with 2 profiles, one of them selected), as a deployment does.
"""

import asyncio
import json
import os
import uuid
from pathlib import Path

import asyncpg
import pytest

API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")
WORKER_URL = os.environ.get("FICHAITOR_TEST_WORKER_URL")
CONVERSATION_URL = os.environ.get("FICHAITOR_TEST_CONVERSATION_URL")

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL and WORKER_URL and CONVERSATION_URL),
    reason="sin base de datos de pruebas configurada",
)

PREFIX = "pytest-mn-"
ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ("db/schema.sql", "db/schema_worker.sql", "db/schema_channels.sql")
UNFINISHED = "('programada','enviada','recordada','recopilando','por_confirmar','ejecutando')"


def run(coro):
    return asyncio.run(coro)


async def owner(sql: str, *args):
    conn = await asyncpg.connect(OWNER_URL)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def as_role(url: str, sql: str, *args):
    """Run and commit a statement as the role of `url` (only what must be let in)."""
    conn = await asyncpg.connect(url)
    try:
        return await conn.execute(sql, *args)
    finally:
        await conn.close()


async def attempt(url: str, sql: str, *args):
    """Run a statement as the role of `url` in a transaction that is always rolled back.
    Returns its status; raises what the database raised."""
    conn = await asyncpg.connect(url)
    try:
        tx = conn.transaction()
        await tx.start()
        try:
            return await conn.execute(sql, *args)
        finally:
            await tx.rollback()
    finally:
        await conn.close()


def refusal(url: str, sql: str, *args) -> str | None:
    """The message of the error the database raised, or None if it let it in."""
    try:
        run(attempt(url, sql, *args))
    except asyncpg.PostgresError as exc:
        return str(exc)
    return None


def refused(url: str, sql: str, *args) -> bool:
    return refusal(url, sql, *args) is not None


async def cleanup():
    mine = f"(SELECT id FROM account WHERE google_sub LIKE '{PREFIX}%')"
    for sql in (
        f"DELETE FROM conversation_lane WHERE account_id IN {mine}",
        f"DELETE FROM proposal WHERE account_id IN {mine}",
        f"DELETE FROM channel_identity WHERE account_id IN {mine}",
        f"DELETE FROM link_code WHERE account_id IN {mine}",
        f"DELETE FROM offboarding WHERE account_id IN {mine}",
        f"DELETE FROM review WHERE google_sub LIKE '{PREFIX}%'",
        f"DELETE FROM profile_summary WHERE google_sub LIKE '{PREFIX}%'",
        f"UPDATE account SET selected_location_id = NULL WHERE id IN {mine}",
        f"DELETE FROM location WHERE account_id IN {mine}",
        f"DELETE FROM location_activation_log WHERE account_id IN {mine}",
        f"DELETE FROM account WHERE id IN {mine}",
        "DELETE FROM usage_counter WHERE scope LIKE 'pytest_mn%'",
    ):
        await owner(sql)


@pytest.fixture(autouse=True)
def clean():
    run(cleanup())
    yield
    run(cleanup())


async def new_account(name: str, *, beta: bool = True) -> tuple[str, str]:
    """An account of its own (with the beta entitlement every new account gets, unless
    `beta` is False). Returns (id, google_sub)."""
    sub = f"{PREFIX}{name}-{uuid.uuid4().hex[:8]}"
    rows = await owner(
        "INSERT INTO account (google_sub, email) VALUES ($1, 'x@x') RETURNING id::text", sub
    )
    account = rows[0]["id"]
    if not beta:
        await owner("DELETE FROM plan_entitlement WHERE account_id = $1::uuid", account)
    return account, sub


async def new_location(account: str, gbp: str | None = None, title: str = "Bar") -> str:
    rows = await owner(
        "INSERT INTO location (account_id, gbp_account, gbp_location, title)"
        " VALUES ($1::uuid, 'accounts/1', $2, $3) RETURNING id",
        account,
        gbp or f"locations/{PREFIX}{uuid.uuid4().hex[:12]}",
        title,
    )
    return rows[0]["id"]


ACTIVATE = "UPDATE location SET active_since = now(), plan = $2 WHERE id = $1"
DEACTIVATE = "UPDATE location SET active_since = NULL, plan = NULL WHERE id = $1"


async def state(location: str) -> asyncpg.Record:
    return (
        await owner(
            "SELECT active_since, plan, deactivated_at, removed_at FROM location WHERE id = $1",
            location,
        )
    )[0]


# =============================================================================
# The backfill: the selected profile becomes active, once.
# =============================================================================


async def apply_schemas() -> None:
    conn = await asyncpg.connect(OWNER_URL)
    try:
        for name in SCHEMAS:
            await conn.execute((ROOT / name).read_text(encoding="utf-8"))
    finally:
        await conn.close()


def _old_columns(rows) -> list[dict]:
    new = {"active_since", "plan", "deactivated_at", "location_id"}
    return [{k: v for k, v in json.loads(r["row"]).items() if k not in new} for r in rows]


async def _snapshot(accounts: list[str]) -> dict[str, list[dict]]:
    """Every old column of every row of these accounts, as the old code left them."""
    snapshot = {}
    for table, where in (
        ("account", "id = ANY($1::uuid[])"),
        ("location", "account_id = ANY($1::uuid[])"),
        ("proposal", "account_id = ANY($1::uuid[])"),
        (
            "profile_summary",
            "google_sub IN (SELECT google_sub FROM account WHERE id = ANY($1::uuid[]))",
        ),
        ("review", "google_sub IN (SELECT google_sub FROM account WHERE id = ANY($1::uuid[]))"),
    ):
        rows = await owner(
            f"SELECT to_jsonb(t)::text AS row FROM {table} t WHERE {where} ORDER BY 1",  # noqa: S608
            accounts,
        )
        snapshot[table] = _old_columns(rows)
    return snapshot


def test_backfill_activates_the_selected_profile_and_does_not_touch_old_data():
    async def go():
        # An account as the old code left it: 2 profiles, one selected, both synced once
        # (the other one was selected before), no beta entitlement and nothing logged.
        account, sub = await new_account("antigua", beta=False)
        selected = await new_location(account, title="Bar La Parra")
        other = await new_location(account, title="La Parra Getafe")
        gbp_selected, gbp_other = [
            (await owner("SELECT gbp_location FROM location WHERE id = $1", i))[0][0]
            for i in (selected, other)
        ]
        await owner(
            "UPDATE account SET selected_location_id = $2 WHERE id = $1::uuid", account, selected
        )
        for gbp in (gbp_selected, gbp_other):
            await owner(
                "INSERT INTO profile_summary (google_sub, location, status) VALUES ($1, $2, 'ok')",
                sub,
                gbp,
            )
        review = (
            await owner(
                "INSERT INTO review (google_sub, location, gbp_review_id, rating)"
                " VALUES ($1, $2, 'g1', 5) RETURNING id",
                sub,
                gbp_other,
            )
        )[0]["id"]
        ids = {name: f"prop_{uuid.uuid4().hex[:16]}" for name in ("kd", "rr", "oo", "lost")}
        for name, kind, ref, st, payload in (
            ("kd", "key_date", f"{selected}:2026-12-08", "enviada", {"location_id": selected}),
            ("rr", "review_reply", review, "descartada", {}),
            ("oo", "owner_order", "pytest:orden", "programada", {}),
            ("lost", "review_reply", "rev_00000000deadbeef", "caducada", {}),
        ):
            await owner(
                "INSERT INTO proposal (id, account_id, type, source_ref, state, payload)"
                " VALUES ($1, $2::uuid, $3, $4, $5, $6::jsonb)",
                ids[name],
                account,
                kind,
                ref,
                st,
                json.dumps(payload),
            )
        # A second, newer account that selected the same Google profile: Gratis can only
        # stay in one account (decision 5), the oldest one.
        later, _ = await new_account("posterior", beta=False)
        twin = await new_location(later, gbp=gbp_selected, title="Bar La Parra")
        await owner("UPDATE account SET selected_location_id = $2 WHERE id = $1::uuid", later, twin)

        before = await _snapshot([account, later])
        await apply_schemas()
        await apply_schemas()
        after = await _snapshot([account, later])

        sel, oth, tw = [await state(i) for i in (selected, other, twin)]
        log = await owner(
            "SELECT account_id::text, location_id, event, plan, reason, written_by"
            " FROM location_activation_log WHERE account_id = ANY($1::uuid[])",
            [account, later],
        )
        beta = await owner(
            "SELECT account_id::text, plan, quantity, source, ends_at FROM plan_entitlement"
            " WHERE account_id = ANY($1::uuid[])",
            [account, later],
        )
        located = {
            r["id"]: r["location_id"]
            for r in await owner(
                "SELECT id, location_id FROM proposal WHERE account_id = $1::uuid", account
            )
        }
        # The owner deactivates it; a later deployment does not activate it again.
        await owner(DEACTIVATE, selected)
        await apply_schemas()
        again = await state(selected)
        return (
            before,
            after,
            sel,
            oth,
            tw,
            log,
            beta,
            located,
            ids,
            again,
            (account, later, selected, other),
        )

    before, after, sel, oth, tw, log, beta, located, ids, again, names = run(go())
    account, later, selected, other = names
    assert after == before  # nothing old was changed
    assert sel["plan"] == "gratis" and sel["active_since"] is not None
    assert sel["deactivated_at"] is None
    assert oth["plan"] is None and oth["active_since"] is None
    assert oth["deactivated_at"] is not None  # its data goes in 30 days
    assert tw["plan"] is None  # the Google profile is free in the older account
    assert [
        (r["account_id"], r["location_id"], r["event"], r["plan"], r["reason"]) for r in log
    ] == [(account, selected, "activated", "gratis", "migration")]
    assert log[0]["written_by"] == "fichaitor"
    assert sorted((r["account_id"], r["plan"], r["quantity"], r["source"]) for r in beta) == sorted(
        [(account, "pro", 3, "beta"), (later, "pro", 3, "beta")]
    )
    assert all(r["ends_at"] is None for r in beta)
    assert located == {
        ids["kd"]: selected,
        ids["rr"]: other,
        ids["oo"]: selected,
        ids["lost"]: None,
    }
    assert again["active_since"] is None and again["deactivated_at"] is not None


# =============================================================================
# Limits: in the database, whatever SQL the API runs.
# =============================================================================


def test_a_second_free_profile_is_refused():
    account, _ = run(new_account("gratis"))
    first, second = run(new_location(account)), run(new_location(account))
    assert run(as_role(API_URL, ACTIVATE, first, "gratis")) == "UPDATE 1"
    assert "limite_fichas" in (refusal(API_URL, ACTIVATE, second, "gratis") or "")


def test_pro_needs_an_entitlement():
    account, _ = run(new_account("sinderecho", beta=False))
    location = run(new_location(account))
    assert "limite_fichas" in (refusal(API_URL, ACTIVATE, location, "pro") or "")
    run(
        owner(
            "INSERT INTO plan_entitlement (account_id, plan, quantity, starts_at, source)"
            " VALUES ($1::uuid, 'pro', 1, now() - interval '1 day', 'manual')",
            account,
        )
    )
    assert run(attempt(API_URL, ACTIVATE, location, "pro")) == "UPDATE 1"
    # A WhatsApp profile needs its own.
    assert refused(API_URL, ACTIVATE, location, "pro_whatsapp")


def test_an_ended_entitlement_no_longer_counts():
    account, _ = run(new_account("vencido", beta=False))
    location = run(new_location(account))
    run(
        owner(
            "INSERT INTO plan_entitlement (account_id, plan, quantity, starts_at, ends_at, source)"
            " VALUES ($1::uuid, 'pro', 5, now() - interval '2 days', now() - interval '1 day',"
            " 'manual')",
            account,
        )
    )
    assert refused(API_URL, ACTIVATE, location, "pro")


def test_every_new_account_has_the_beta_of_three_pro_profiles():
    account, _ = run(new_account("beta"))
    rows = run(
        owner(
            "SELECT plan, quantity, source FROM plan_entitlement WHERE account_id = $1::uuid",
            account,
        )
    )
    assert [tuple(r) for r in rows] == [("pro", 3, "beta")]
    locations = [run(new_location(account)) for _ in range(4)]
    for location in locations[:3]:
        assert run(as_role(API_URL, ACTIVATE, location, "pro")) == "UPDATE 1"
    assert "limite_fichas" in (refusal(API_URL, ACTIVATE, locations[3], "pro") or "")
    # Free is apart from the beta.
    assert run(as_role(API_URL, ACTIVATE, locations[3], "gratis")) == "UPDATE 1"


def test_two_activations_at_once_only_one_wins():
    async def go():
        account, _ = await new_account("carrera")
        first, second = await new_location(account), await new_location(account)
        one = await asyncpg.connect(API_URL)
        two = await asyncpg.connect(API_URL)
        try:
            tx_one, tx_two = one.transaction(), two.transaction()
            await tx_one.start()
            await tx_two.start()
            await one.execute(ACTIVATE, first, "gratis")
            racing = asyncio.create_task(two.execute(ACTIVATE, second, "gratis"))
            await asyncio.sleep(0.5)
            waited = not racing.done()  # held by the lock on the account
            await tx_one.commit()
            try:
                await racing
                outcome = "won"
            except asyncpg.PostgresError as exc:
                outcome = str(exc)
            await tx_two.rollback()
        finally:
            await one.close()
            await two.close()
        active = await owner(
            "SELECT count(*) AS n FROM location WHERE account_id = $1::uuid"
            " AND active_since IS NOT NULL",
            account,
        )
        return waited, outcome, active[0]["n"]

    waited, outcome, active = run(go())
    assert waited
    assert "limite_fichas" in outcome
    assert active == 1


def test_changing_the_plan_of_an_active_profile_is_counted_without_itself():
    account, _ = run(new_account("cambio"))
    location = run(new_location(account))
    run(as_role(API_URL, ACTIVATE, location, "gratis"))
    change = "UPDATE location SET plan = $2 WHERE id = $1"
    assert run(as_role(API_URL, change, location, "pro")) == "UPDATE 1"
    rows = run(
        owner(
            "SELECT event, plan FROM location_activation_log WHERE location_id = $1 ORDER BY id",
            location,
        )
    )
    assert [tuple(r) for r in rows] == [("activated", "gratis"), ("plan_changed", "pro")]


def test_a_google_profile_is_free_in_one_account_only():
    first, _ = run(new_account("una"))
    second, _ = run(new_account("otra"))
    gbp = f"locations/{PREFIX}{uuid.uuid4().hex[:12]}"
    mine, theirs = run(new_location(first, gbp)), run(new_location(second, gbp))
    assert run(as_role(API_URL, ACTIVATE, mine, "gratis")) == "UPDATE 1"
    assert "gratis_ocupada" in (refusal(API_URL, ACTIVATE, theirs, "gratis") or "")
    # With a paid plan it can be active in the other account too.
    assert run(attempt(API_URL, ACTIVATE, theirs, "pro")) == "UPDATE 1"


def test_the_free_rule_has_its_own_index_as_the_last_barrier():
    first, _ = run(new_account("idx1"))
    second, _ = run(new_account("idx2"))
    gbp = f"locations/{PREFIX}{uuid.uuid4().hex[:12]}"
    mine, theirs = run(new_location(first, gbp)), run(new_location(second, gbp))
    run(owner(ACTIVATE, mine, "gratis"))
    # Not even the owner, who skips the trigger's checks, gets past the index.
    with pytest.raises(asyncpg.UniqueViolationError) as caught:
        run(owner(ACTIVATE, theirs, "gratis"))
    assert caught.value.constraint_name == "location_gratis_one_account"


def test_no_activation_with_a_scheduled_leave():
    account, sub = run(new_account("baja"))
    location = run(new_location(account))
    run(
        owner(
            "INSERT INTO offboarding (account_id, scope, google_sub, confirmed_at, delete_at)"
            " VALUES ($1::uuid, 'ficha', $2, now(), now() + interval '48 hours')",
            account,
            sub,
        )
    )
    assert "baja_programada" in (refusal(API_URL, ACTIVATE, location, "gratis") or "")


def test_an_activation_cannot_be_dated_back():
    account, _ = run(new_account("fecha"))
    location = run(new_location(account))
    back = (
        "UPDATE location SET active_since = now() - interval '1 year', plan = 'gratis'"
        " WHERE id = $1"
    )
    assert "fecha_activacion" in (refusal(API_URL, back, location) or "")


def test_a_removed_profile_cannot_be_activated():
    account, _ = run(new_account("retirada"))
    location = run(new_location(account))
    run(owner("UPDATE location SET removed_at = now() WHERE id = $1", location))
    assert "ficha_retirada" in (refusal(API_URL, ACTIVATE, location, "gratis") or "")
    assert run(state(location))["active_since"] is None


def test_the_api_cannot_insert_an_active_profile_past_the_limit():
    account, _ = run(new_account("insert"))
    run(as_role(API_URL, ACTIVATE, run(new_location(account)), "gratis"))
    insert = (
        "INSERT INTO location (account_id, gbp_account, gbp_location, title, active_since, plan,"
        " deactivated_at) VALUES ($1::uuid, 'a', $2, 't', now(), 'gratis', NULL)"
    )
    assert "limite_fichas" in (
        refusal(API_URL, insert, account, f"locations/{PREFIX}{uuid.uuid4().hex[:8]}") or ""
    )


# =============================================================================
# Deactivating and removing.
# =============================================================================


def test_removing_a_profile_deactivates_it_with_the_api_and_with_the_worker():
    account, _ = run(new_account("quitar"))
    by_api, by_worker = run(new_location(account)), run(new_location(account))
    run(as_role(API_URL, ACTIVATE, by_api, "gratis"))
    run(as_role(API_URL, ACTIVATE, by_worker, "pro"))
    remove = "UPDATE location SET removed_at = now() WHERE id = $1"
    assert run(as_role(API_URL, remove, by_api)) == "UPDATE 1"
    assert run(as_role(WORKER_URL, remove, by_worker)) == "UPDATE 1"
    for location in (by_api, by_worker):
        row = run(state(location))
        assert row["active_since"] is None and row["plan"] is None
        assert row["deactivated_at"] is not None and row["removed_at"] is not None
    log = run(
        owner(
            "SELECT location_id, event, reason, written_by FROM location_activation_log"
            " WHERE account_id = $1::uuid AND event = 'deactivated'",
            account,
        )
    )
    assert {(r["location_id"], r["reason"], r["written_by"]) for r in log} == {
        (by_api, "removed", "fichaitor_api_dev"),
        (by_worker, "removed", "fichaitor_worker_dev"),
    }


def test_deactivating_sets_the_date_and_activating_again_clears_it():
    account, _ = run(new_account("desactivar"))
    location = run(new_location(account))
    run(as_role(API_URL, ACTIVATE, location, "gratis"))
    # Only active_since: the trigger empties the plan too.
    run(as_role(API_URL, "UPDATE location SET active_since = NULL WHERE id = $1", location))
    off = run(state(location))
    assert off["plan"] is None and off["deactivated_at"] is not None
    run(as_role(API_URL, ACTIVATE, location, "gratis"))
    assert run(state(location))["deactivated_at"] is None


def test_deactivating_the_selected_profile_moves_the_preference_to_the_oldest_active():
    account, _ = run(new_account("preferencia"))
    oldest, newer, selected = [run(new_location(account)) for _ in range(3)]
    run(as_role(API_URL, ACTIVATE, oldest, "pro"))
    run(as_role(API_URL, ACTIVATE, newer, "pro"))
    run(as_role(API_URL, ACTIVATE, selected, "gratis"))
    select = "UPDATE account SET selected_location_id = $2 WHERE id = $1::uuid"
    pick = "SELECT selected_location_id FROM account WHERE id = $1::uuid"
    run(as_role(API_URL, select, account, selected))
    run(as_role(API_URL, DEACTIVATE, selected))
    assert run(owner(pick, account))[0][0] == oldest
    # Deactivating one that is not selected leaves the preference alone.
    run(as_role(API_URL, DEACTIVATE, newer))
    assert run(owner(pick, account))[0][0] == oldest
    # The last active one may be deactivated: no preference is left.
    run(as_role(API_URL, DEACTIVATE, oldest))
    assert run(owner(pick, account))[0][0] is None


def test_removing_the_selected_profile_leaves_the_preference_as_the_old_code_expects():
    account, _ = run(new_account("sinacceso"))
    location = run(new_location(account))
    run(as_role(API_URL, ACTIVATE, location, "gratis"))
    run(
        owner("UPDATE account SET selected_location_id = $2 WHERE id = $1::uuid", account, location)
    )
    run(as_role(WORKER_URL, "UPDATE location SET removed_at = now() WHERE id = $1", location))
    pick = run(owner("SELECT selected_location_id FROM account WHERE id = $1::uuid", account))
    assert pick[0][0] == location  # the panel still shows "sin acceso"


def test_the_log_records_who_wrote_it_and_nobody_writes_it_by_hand():
    account, _ = run(new_account("registro"))
    location = run(new_location(account))
    run(as_role(API_URL, ACTIVATE, location, "gratis"))
    rows = run(
        owner(
            "SELECT event, plan, reason, written_by FROM location_activation_log"
            " WHERE location_id = $1",
            location,
        )
    )
    assert [tuple(r) for r in rows] == [("activated", "gratis", "owner", "fichaitor_api_dev")]
    forge = (
        "INSERT INTO location_activation_log (account_id, location_id, event)"
        " VALUES ($1::uuid, $2, 'activated')"
    )
    for url in (API_URL, WORKER_URL, CONVERSATION_URL):
        assert refused(url, forge, account, location)
    assert refused(API_URL, "UPDATE location_activation_log SET reason = 'x' WHERE false")
    assert refused(API_URL, "DELETE FROM location_activation_log WHERE false")


# =============================================================================
# Privileges.
# =============================================================================


def test_the_api_reads_entitlements_and_cannot_write_them():
    account, _ = run(new_account("derechos"))
    assert run(attempt(API_URL, "SELECT 1 FROM plan_entitlement WHERE false")) == "SELECT 0"
    insert = (
        "INSERT INTO plan_entitlement (account_id, plan, quantity, starts_at, source)"
        " VALUES ($1::uuid, 'pro', 100, now(), 'manual')"
    )
    for url in (API_URL, CONVERSATION_URL, WORKER_URL):
        assert refused(url, insert, account)
    assert refused(API_URL, "UPDATE plan_entitlement SET quantity = 100 WHERE false")
    assert refused(API_URL, "DELETE FROM plan_entitlement WHERE false")
    assert refused(CONVERSATION_URL, "SELECT 1 FROM plan_entitlement WHERE false")


def test_nobody_but_the_trigger_sets_deactivated_at():
    for url in (API_URL, WORKER_URL, CONVERSATION_URL):
        assert refused(url, "UPDATE location SET deactivated_at = now() WHERE false")
    account, _ = run(new_account("insertfecha"))
    insert = (
        "INSERT INTO location (account_id, gbp_account, gbp_location, title, deactivated_at)"
        " VALUES ($1::uuid, 'a', $2, 't', now() - interval '40 days')"
    )
    gbp = f"locations/{PREFIX}{uuid.uuid4().hex[:8]}"
    run(as_role(API_URL, insert, account, gbp))
    row = run(owner("SELECT deactivated_at FROM location WHERE gbp_location = $1", gbp))
    assert row[0][0] is None


def test_only_the_api_activates():
    for url in (WORKER_URL, CONVERSATION_URL):
        assert refused(url, "UPDATE location SET active_since = now(), plan = 'pro' WHERE false")


def test_the_worker_and_the_conversation_read_the_new_columns():
    sql = "SELECT active_since, plan, deactivated_at FROM location WHERE false"
    for url in (API_URL, WORKER_URL, CONVERSATION_URL):
        assert run(attempt(url, sql)) == "SELECT 0"
    assert run(attempt(WORKER_URL, "SELECT account_id FROM location_activation_log WHERE false"))
    assert run(attempt(WORKER_URL, "DELETE FROM location_activation_log WHERE false"))
    assert run(attempt(WORKER_URL, "DELETE FROM plan_entitlement WHERE false"))
    assert run(attempt(WORKER_URL, "DELETE FROM conversation_lane WHERE false"))


# =============================================================================
# The second barrier between accounts: proposals, lanes and filters.
# =============================================================================


async def _two_accounts() -> tuple[str, str, str, str]:
    mine, _ = await new_account("mia")
    theirs, _ = await new_account("ajena")
    return mine, await new_location(mine), theirs, await new_location(theirs)


PROPOSAL = (
    "INSERT INTO proposal (id, account_id, type, source_ref, location_id)"
    " VALUES ($1, $2::uuid, 'owner_order', $3, $4)"
)


def test_a_proposal_cannot_name_a_profile_of_another_account():
    mine, my_loc, _, their_loc = run(_two_accounts())
    run(owner(ACTIVATE, my_loc, "pro"))
    run(owner(ACTIVATE, their_loc, "pro"))
    pid = f"prop_{uuid.uuid4().hex[:16]}"
    assert refused(CONVERSATION_URL, PROPOSAL, pid, mine, "pytest:x", their_loc)
    assert run(attempt(CONVERSATION_URL, PROPOSAL, pid, mine, "pytest:x", my_loc)) == "INSERT 0 1"
    # Without a profile, and no selected one to fill it in with: refused since step 6a
    # (tests/test_conversation_lanes_postgres.py).
    assert refused(CONVERSATION_URL, PROPOSAL, pid, mine, "pytest:x", None)


def test_a_key_date_is_of_the_profile_its_payload_names():
    mine, my_loc, _, _ = run(_two_accounts())
    other = run(new_location(mine))
    sql = (
        "INSERT INTO proposal (id, account_id, type, source_ref, location_id, payload)"
        " VALUES ($1, $2::uuid, 'key_date', 'k', $3, $4::jsonb)"
    )
    run(owner(ACTIVATE, my_loc, "pro"))
    run(owner(ACTIVATE, other, "pro"))
    pid = f"prop_{uuid.uuid4().hex[:16]}"
    payload = json.dumps({"location_id": my_loc})
    assert refused(CONVERSATION_URL, sql, pid, mine, other, payload)
    assert run(attempt(CONVERSATION_URL, sql, pid, mine, my_loc, payload)) == "INSERT 0 1"


async def _proposal(account: str, location: str | None, state: str = "enviada") -> str:
    pid = f"prop_{uuid.uuid4().hex[:16]}"
    await owner(
        "INSERT INTO proposal (id, account_id, type, source_ref, location_id, state)"
        " VALUES ($1, $2::uuid, 'review_reply', $3, $4, $5)",
        pid,
        account,
        f"pytest:{pid}",
        location,
        state,
    )
    return pid


LANE = (
    "INSERT INTO conversation_lane (account_id, location_id, active_proposal_id)"
    " VALUES ($1::uuid, $2, $3)"
)


def test_a_lane_cannot_name_a_profile_or_a_proposal_of_another_profile():
    mine, my_loc, theirs, their_loc = run(_two_accounts())
    second = run(new_location(mine))
    mine_prop = run(_proposal(mine, my_loc))
    their_prop = run(_proposal(theirs, their_loc))
    other_lane_prop = run(_proposal(mine, second))
    assert refused(CONVERSATION_URL, LANE, mine, their_loc, None)
    assert refused(CONVERSATION_URL, LANE, mine, my_loc, their_prop)
    assert refused(CONVERSATION_URL, LANE, mine, my_loc, other_lane_prop)
    assert run(attempt(CONVERSATION_URL, LANE, mine, my_loc, mine_prop)) == "INSERT 0 1"
    assert refused(API_URL, LANE, mine, my_loc, None)  # the API never creates one


def test_the_api_only_releases_a_lane_and_cancels_proposals_of_an_inactive_profile():
    mine, my_loc, _, _ = run(_two_accounts())
    pid = run(_proposal(mine, my_loc))
    run(owner(LANE, mine, my_loc, pid))
    point = "UPDATE conversation_lane SET active_proposal_id = $2 WHERE account_id = $1::uuid"
    assert refused(API_URL, point, mine, pid)
    release = (
        "UPDATE conversation_lane SET active_proposal_id = NULL, updated_at = now()"
        " WHERE account_id = $1::uuid"
    )
    assert run(attempt(API_URL, release, mine)) == "UPDATE 1"
    cancel = "UPDATE proposal SET state = 'cancelada', outcome_reason = $2 WHERE id = $1"
    assert run(attempt(API_URL, cancel, pid, "location_inactive")) == "UPDATE 1"
    assert refused(API_URL, cancel, pid, "expired")


def test_deleting_a_profile_empties_its_proposals_and_its_lane_despite_the_policies():
    async def go():
        account, _ = await new_account("borrar")
        location = await new_location(account)
        pid = await _proposal(account, location)
        await owner(LANE, account, location, pid)
        status = await as_role(WORKER_URL, "DELETE FROM location WHERE id = $1", location)
        proposal = await owner("SELECT location_id FROM proposal WHERE id = $1", pid)
        lanes = await owner("SELECT 1 FROM conversation_lane WHERE account_id = $1::uuid", account)
        return status, proposal[0]["location_id"], lanes

    status, location_id, lanes = run(go())
    assert status == "DELETE 1"
    assert location_id is None
    assert lanes == []


FILTER = "UPDATE channel_identity SET location_filter = $2 WHERE id = $1"


async def _identity(account: str) -> str:
    rows = await owner(
        "INSERT INTO channel_identity (account_id, channel, address) VALUES ($1::uuid,"
        " 'telegram', $2) RETURNING id",
        account,
        str(uuid.uuid4().int % 10**12),
    )
    return rows[0]["id"]


def test_a_chat_cannot_be_filtered_to_a_profile_of_another_account():
    mine, my_loc, _, their_loc = run(_two_accounts())
    chat = run(_identity(mine))
    assert "filtro_ajeno" in (refusal(API_URL, FILTER, chat, [their_loc]) or "")
    assert refused(API_URL, FILTER, chat, [my_loc, their_loc])
    assert refused(API_URL, FILTER, chat, [my_loc, None])
    assert refused(API_URL, FILTER, chat, [])
    assert run(attempt(API_URL, FILTER, chat, [my_loc])) == "UPDATE 1"
    assert run(attempt(API_URL, FILTER, chat, None)) == "UPDATE 1"
    # The conversation copies the filter when it links, and only its own profiles.
    link = (
        "INSERT INTO channel_identity (account_id, channel, address, location_filter)"
        " VALUES ($1::uuid, 'telegram', '424242', $2)"
    )
    assert refused(CONVERSATION_URL, link, mine, [their_loc])
    assert run(attempt(CONVERSATION_URL, link, mine, [my_loc])) == "INSERT 0 1"
    # ... and cannot change it afterwards.
    assert refused(
        CONVERSATION_URL, "UPDATE channel_identity SET location_filter = NULL WHERE false"
    )


def test_a_link_code_cannot_carry_a_profile_of_another_account():
    mine, my_loc, _, their_loc = run(_two_accounts())
    sql = (
        "INSERT INTO link_code (code_hash, account_id, expires_at, location_filter)"
        " VALUES ($1, $2::uuid, now() + interval '10 minutes', $3)"
    )
    code = uuid.uuid4().hex * 2
    assert refused(API_URL, sql, code, mine, [their_loc])
    assert run(attempt(API_URL, sql, code, mine, [my_loc])) == "INSERT 0 1"


def test_a_profile_deleted_later_does_not_block_the_chat():
    mine, my_loc, _, _ = run(_two_accounts())
    chat = run(_identity(mine))
    run(owner(FILTER, chat, [my_loc]))
    run(owner("DELETE FROM location WHERE id = $1", my_loc))
    touch = "UPDATE channel_identity SET last_inbound_at = now() WHERE id = $1"
    assert run(attempt(CONVERSATION_URL, touch, chat)) == "UPDATE 1"


def test_the_conversation_writes_the_focus_only():
    mine, _, _, _ = run(_two_accounts())
    chat = run(_identity(mine))
    focus = "UPDATE channel_identity SET focus_proposal_id = $2, focus_at = now() WHERE id = $1"
    assert run(attempt(CONVERSATION_URL, focus, chat, "prop_0011223344556677")) == "UPDATE 1"
    assert refused(CONVERSATION_URL, focus, chat, "nada")
    assert refused(API_URL, "UPDATE channel_identity SET focus_at = now() WHERE false")


# =============================================================================
# The scope of the caps, per profile.
# =============================================================================


@pytest.mark.parametrize("url", [API_URL, CONVERSATION_URL])
def test_a_cap_per_profile_fits_the_scope(url):
    sql = (
        "INSERT INTO usage_counter (scope, window_start, expires_at, hits)"
        " VALUES ($1, now(), now() + interval '1 day', 1)"
    )
    assert run(attempt(url, sql, "pytest_mn:loc_3f2a9b1c0d4e")) == "INSERT 0 1"
    assert run(attempt(url, sql, f"pytest_mn:{uuid.uuid4()}")) == "INSERT 0 1"
    assert refused(url, sql, "pytest_mn:LOC 1")
