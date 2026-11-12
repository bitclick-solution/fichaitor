"""Multinegocio, step 5 (docs/multinegocio.md): the worker's coherence and retention in
PostgreSQL, with the role of the worker, against the TEST database only.

Skipped unless the URLs are set. They must point to a database whose name ends in
`_test` (tests/conftest.py stops the run otherwise). Create it with
scripts/preparar_bd_pruebas.sh.

Rows are written by the owner and deleted at the end; every one of them hangs from an
account whose google_sub starts with PREFIX. Dates the database writes itself (the
trigger dates a deactivation with now()) are overwritten by the owner where a test
needs another one, and the pass runs with the clock of the database, so the tests agree
with any policy the database adds on top.
"""

import asyncio
import os
import uuid
from datetime import datetime, timedelta

import asyncpg
import pytest

from app.adapters.worker_store.postgres import PostgresWorkerStore
from app.domain.offboarding import RetentionPolicy

API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")
WORKER_URL = os.environ.get("FICHAITOR_TEST_WORKER_URL")

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL and WORKER_URL), reason="sin base de datos de pruebas configurada"
)

# Not "pytest-...": other test modules clean every "pytest-%" row of the shared test
# database, and may run at the same time as this one.
PREFIX = "mn5prueba-"
DAY = timedelta(days=1)
POLICY = RetentionPolicy()
GOOGLE_TABLES = (
    "profile_summary",
    "review",
    "sentiment_analysis",
    "sentiment_history",
    "sentiment_usage",
)


def run(coro):
    return asyncio.run(coro)


async def owner(sql: str, *args):
    conn = await asyncpg.connect(OWNER_URL)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def db_now() -> datetime:
    return (await owner("SELECT now() AS now"))[0]["now"]


# Accounts a leave of scope "cuenta" deletes lose their google_sub (the trigger writes
# "deleted:<id>"): they are found by id.
CREATED: set[str] = set()


async def cleanup():
    ids = ",".join(f"'{i}'::uuid" for i in sorted(CREATED)) or "NULL::uuid"
    mine = f"(SELECT id FROM account WHERE google_sub LIKE '{PREFIX}%' OR id IN ({ids}))"
    subs = f"'{PREFIX}%'"
    for sql in (
        f"DELETE FROM audit_log WHERE account_id IN {mine}",
        f"DELETE FROM action WHERE account_id IN {mine}",
        f"DELETE FROM channel_message WHERE proposal_id IN"
        f" (SELECT id FROM proposal WHERE account_id IN {mine})",
        f"DELETE FROM conversation_lane WHERE account_id IN {mine}",
        f"DELETE FROM conversation WHERE account_id IN {mine}",
        f"DELETE FROM proposal WHERE account_id IN {mine}",
        f"DELETE FROM offboarding WHERE account_id IN {mine}",
        *(f"DELETE FROM {t} WHERE google_sub LIKE {subs}" for t in GOOGLE_TABLES),
        f"UPDATE account SET selected_location_id = NULL WHERE id IN {mine}",
        f"DELETE FROM location WHERE account_id IN {mine}",
        f"DELETE FROM location_activation_log WHERE account_id IN {mine}",
        f"DELETE FROM plan_entitlement WHERE account_id IN {mine}",
        f"DELETE FROM account WHERE id IN {mine}",
    ):
        await owner(sql)  # noqa: S608
    # The pass records its counts; only the tests' own rows of other tables are touched.
    await owner("DELETE FROM retention_run")
    CREATED.clear()


@pytest.fixture(autouse=True)
def clean():
    run(cleanup())
    yield
    run(cleanup())


async def new_account(name: str) -> tuple[str, str]:
    """An account of its own, with the beta entitlement every new account gets.
    Returns (id, google_sub)."""
    sub = f"{PREFIX}{name}-{uuid.uuid4().hex[:8]}"
    rows = await owner(
        "INSERT INTO account (google_sub, email) VALUES ($1, 'x@x') RETURNING id::text", sub
    )
    CREATED.add(rows[0]["id"])
    return rows[0]["id"], sub


async def new_location(account: str, *, active: bool = True) -> tuple[str, str]:
    """A profile of that account, active in Pro (the owner skips the limits) or never
    active. Returns (id, gbp_location)."""
    gbp = f"locations/{PREFIX}{uuid.uuid4().hex[:12]}"
    rows = await owner(
        "INSERT INTO location (account_id, gbp_account, gbp_location, title)"
        " VALUES ($1::uuid, 'accounts/1', $2, 'Bar') RETURNING id",
        account,
        gbp,
    )
    location = rows[0]["id"]
    if active:
        await owner(
            "UPDATE location SET active_since = now(), plan = 'pro' WHERE id = $1", location
        )
    return location, gbp


async def deactivate(location: str, at: datetime) -> None:
    """Deactivate as the owner would through the trigger, then date it at `at`."""
    await owner("UPDATE location SET active_since = NULL, plan = NULL WHERE id = $1", location)
    await owner("UPDATE location SET deactivated_at = $2 WHERE id = $1", location, at)


async def reactivate(location: str) -> None:
    await owner("UPDATE location SET active_since = now(), plan = 'pro' WHERE id = $1", location)


async def google_data(sub: str, gbp: str) -> None:
    """Everything a sync and an analysis store of one profile, as the worker does."""
    await owner(
        "INSERT INTO profile_summary (google_sub, location, status, status_at)"
        " VALUES ($1, $2, 'ok', now())",
        sub,
        gbp,
    )
    review = await owner(
        "INSERT INTO review (google_sub, location, gbp_review_id, rating, comment)"
        " VALUES ($1, $2, 'g-low', 1, 'La comida llegó fría.') RETURNING id",
        sub,
        gbp,
    )
    await owner(
        "INSERT INTO review_sentiment (review_id, source_hash, sentiment, method)"
        " VALUES ($1, repeat('a', 64), 'negative', 'model')",
        review[0]["id"],
    )
    await owner(
        "INSERT INTO review_draft (review_id, source_hash, draft)"
        " VALUES ($1, repeat('b', 64), 'Lo sentimos.')",
        review[0]["id"],
    )
    await owner(
        "INSERT INTO sentiment_analysis (google_sub, location, status, generated_at, based_on,"
        " score, overall) VALUES ($1, $2, 'ok', now(), 1, 20, 'negativo')",
        sub,
        gbp,
    )
    await owner(
        "INSERT INTO sentiment_history (google_sub, location, month, score, based_on)"
        " VALUES ($1, $2, '2026-11-01', 20, 1)",
        sub,
        gbp,
    )
    await owner(
        "INSERT INTO sentiment_usage (google_sub, location, trigger) VALUES ($1, $2, 'first')",
        sub,
        gbp,
    )


async def stored(sub: str, gbp: str) -> dict[str, int]:
    counts = {}
    for table in GOOGLE_TABLES:
        rows = await owner(
            f"SELECT count(*) AS n FROM {table} WHERE google_sub = $1 AND location = $2",  # noqa: S608
            sub,
            gbp,
        )
        counts[table] = rows[0]["n"]
    for table in ("review_sentiment", "review_draft"):
        rows = await owner(
            f"SELECT count(*) AS n FROM {table} x JOIN review r ON r.id = x.review_id"  # noqa: S608
            " WHERE r.google_sub = $1 AND r.location = $2",
            sub,
            gbp,
        )
        counts[table] = rows[0]["n"]
    return counts


FULL = dict.fromkeys((*GOOGLE_TABLES, "review_sentiment", "review_draft"), 1)
EMPTY = dict.fromkeys(FULL, 0)


async def location_row(location: str):
    rows = await owner(
        "SELECT active_since, deactivated_at, removed_at FROM location WHERE id = $1", location
    )
    return rows[0] if rows else None


async def proposal(account: str, location: str, state: str, *, age: timedelta) -> str:
    proposal_id = f"prop_{uuid.uuid4().hex[:16]}"
    await owner(
        "INSERT INTO proposal (id, account_id, type, source_ref, state, location_id, created_at)"
        " VALUES ($1, $2::uuid, 'review_reply', $3, $4, $5, now() - $6::interval)",
        proposal_id,
        account,
        f"rev_{uuid.uuid4().hex[:16]}",
        state,
        location,
        age,
    )
    return proposal_id


async def lane(account: str, location: str, proposal_id: str | None) -> None:
    await owner(
        "INSERT INTO conversation_lane (account_id, location_id, active_proposal_id)"
        " VALUES ($1::uuid, $2, $3)",
        account,
        location,
        proposal_id,
    )


class Worker:
    """The worker's store on its own role."""

    async def __aenter__(self) -> PostgresWorkerStore:
        self.pool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=2)
        return PostgresWorkerStore(self.pool)

    async def __aexit__(self, *exc) -> None:
        await self.pool.close()


async def retain(at: datetime | None = None) -> dict[str, int]:
    async with Worker() as store:
        return await store.run_retention(at or await db_now(), POLICY)


# =============================================================================
# Coherence: an action on a profile that is not active never reaches Google.
# =============================================================================


async def reply_action(account: str, sub: str, gbp: str) -> str:
    review = await owner(
        "INSERT INTO review (google_sub, location, gbp_review_id, rating)"
        " VALUES ($1, $2, $3, 2) RETURNING id",
        sub,
        gbp,
        f"g-{uuid.uuid4().hex[:8]}",
    )
    rows = await owner(
        "INSERT INTO action (account_id, type, google_sub, location, review_id, text)"
        " VALUES ($1::uuid, 'reply_review', $2, $3, $4, 'Gracias.') RETURNING id",
        account,
        sub,
        gbp,
        review[0]["id"],
    )
    return rows[0]["id"]


async def claim(action_id: str):
    async with Worker() as store:
        return await store.claim_action(action_id, "mn5prueba-job", await db_now())


def test_an_action_on_an_active_profile_is_coherent_and_active():
    async def go():
        account, sub = await new_account("activa")
        _, gbp = await new_location(account)
        return await claim(await reply_action(account, sub, gbp))

    claimed = run(go())
    assert claimed.coherent and claimed.active


def test_an_action_on_a_profile_deactivated_after_it_was_confirmed_is_not_active():
    async def go():
        account, sub = await new_account("desactivada")
        location, gbp = await new_location(account)
        action_id = await reply_action(account, sub, gbp)
        await deactivate(location, await db_now())
        return await claim(action_id)

    claimed = run(go())
    # Coherent (the row is what the API writes), but its profile is not active.
    assert claimed.coherent and not claimed.active


def test_an_action_on_a_profile_never_activated_and_not_selected_is_not_active():
    async def go():
        account, sub = await new_account("nunca")
        _, gbp = await new_location(account, active=False)
        return await claim(await reply_action(account, sub, gbp))

    claimed = run(go())
    assert claimed.coherent and not claimed.active


def test_until_step_9_the_selected_profile_still_counts_as_in_use():
    # docs/multinegocio.md, 5.5: until the API activates on selecting (step 4), the old
    # panel works on the selected profile even if it is not active. The trigger moves
    # the preference away from a profile the owner deactivates, so this never keeps a
    # deactivated profile in use.
    async def go():
        account, sub = await new_account("elegida")
        location, gbp = await new_location(account, active=False)
        await owner(
            "UPDATE account SET selected_location_id = $2 WHERE id = $1::uuid", account, location
        )
        return await claim(await reply_action(account, sub, gbp))

    claimed = run(go())
    assert claimed.coherent and claimed.active


def test_a_retired_profile_stays_incoherent_as_before():
    async def go():
        account, sub = await new_account("retirada")
        location, gbp = await new_location(account)
        action_id = await reply_action(account, sub, gbp)
        await owner("UPDATE location SET removed_at = now() WHERE id = $1", location)
        return await claim(action_id)

    claimed = run(go())
    assert not claimed.coherent


# =============================================================================
# Retention of a deactivated profile: 30 days after deactivated_at, unless reactivated.
# =============================================================================


def test_the_data_of_a_deactivated_profile_goes_30_days_later_and_not_a_day_before():
    async def go():
        now = await db_now()
        account, sub = await new_account("plazo")
        location, gbp = await new_location(account)
        kept_location, kept_gbp = await new_location(account)
        await google_data(sub, gbp)
        await google_data(sub, kept_gbp)
        await deactivate(location, now - 29 * DAY)
        await retain(now)
        seen_before = await stored(sub, gbp)
        await owner(
            "UPDATE location SET deactivated_at = $2 WHERE id = $1", location, now - 30 * DAY
        )
        counts = await retain(now)
        again = await retain(now)
        return (
            seen_before,
            counts,
            again,
            await stored(sub, gbp),
            await stored(sub, kept_gbp),
            await location_row(location),
            await location_row(kept_location),
        )

    seen_before, counts, again, gone, kept, row, kept_row = run(go())
    assert seen_before == FULL
    assert gone == EMPTY
    assert all(counts[t] >= 1 for t in GOOGLE_TABLES)
    # A second pass (again) changes nothing and does not fail.
    assert isinstance(again, dict)
    # The other, active profile of the same account is untouched.
    assert kept == FULL
    # The row of the profile stays while Google lists it, still inactive.
    assert row is not None and row["active_since"] is None and row["removed_at"] is None
    assert kept_row["active_since"] is not None


def test_a_reactivated_profile_keeps_its_data_whenever_the_pass_runs():
    async def go():
        now = await db_now()
        account, sub = await new_account("reactivada")
        location, gbp = await new_location(account)
        await google_data(sub, gbp)
        await deactivate(location, now - 40 * DAY)
        # Reactivated: the date of the deactivation goes, so no term counts any more.
        await reactivate(location)
        await retain(now)
        return await stored(sub, gbp), await location_row(location)

    data, row = run(go())
    assert data == FULL
    assert row["deactivated_at"] is None and row["active_since"] is not None


def test_the_selected_profile_keeps_its_data_until_step_9():
    # The same transitional rule as the coherence: the old panel still shows it.
    async def go():
        now = await db_now()
        account, sub = await new_account("elegida-datos")
        location, gbp = await new_location(account, active=False)
        await google_data(sub, gbp)
        await owner(
            "UPDATE location SET deactivated_at = $2 WHERE id = $1", location, now - 60 * DAY
        )
        await owner(
            "UPDATE account SET selected_location_id = $2 WHERE id = $1::uuid", account, location
        )
        await retain(now)
        return await stored(sub, gbp)

    assert run(go()) == FULL


def test_another_account_with_the_same_google_profile_keeps_its_data():
    # Data are stored per Google user and profile: the deactivation in one account does
    # not touch the copy of another account that manages the same Google profile.
    async def go():
        now = await db_now()
        ana, ana_sub = await new_account("ana")
        luis, luis_sub = await new_account("luis")
        ana_location, gbp = await new_location(ana)
        await owner(
            "INSERT INTO location (account_id, gbp_account, gbp_location, title,"
            " active_since, plan) VALUES ($1::uuid, 'accounts/1', $2, 'Bar', now(), 'pro')",
            luis,
            gbp,
        )
        await google_data(ana_sub, gbp)
        await google_data(luis_sub, gbp)
        await deactivate(ana_location, now - 31 * DAY)
        await retain(now)
        return await stored(ana_sub, gbp), await stored(luis_sub, gbp)

    ana, luis = run(go())
    assert ana == EMPTY and luis == FULL


def test_a_never_active_profile_without_a_date_is_left_alone():
    async def go():
        now = await db_now()
        account, sub = await new_account("sin-fecha")
        _, gbp = await new_location(account, active=False)
        await google_data(sub, gbp)
        await retain(now)
        return await stored(sub, gbp)

    assert run(go()) == FULL


# =============================================================================
# Lanes: no proposal named by a lane is deleted; a leave clears its lanes first.
# =============================================================================


def test_the_pass_does_not_delete_a_finished_proposal_a_lane_still_names():
    async def go():
        account, _ = await new_account("carril")
        location, _ = await new_location(account)
        other, _ = await new_location(account)
        named = await proposal(account, location, "caducada", age=100 * DAY)
        loose = await proposal(account, other, "caducada", age=100 * DAY)
        young = await proposal(account, other, "descartada", age=10 * DAY)
        await lane(account, location, named)
        await lane(account, other, None)
        await retain()
        rows = await owner(
            "SELECT id FROM proposal WHERE account_id = $1::uuid ORDER BY id", account
        )
        lanes = await owner(
            "SELECT active_proposal_id FROM conversation_lane WHERE account_id = $1::uuid"
            " ORDER BY location_id",
            account,
        )
        return named, loose, young, {r["id"] for r in rows}, lanes

    named, loose, young, left, lanes = run(go())
    assert left == {named, young}
    assert loose not in left
    assert len(lanes) == 2  # the pass never deletes or empties a lane


@pytest.mark.parametrize("scope", ["ficha", "cuenta"])
def test_an_effective_leave_clears_the_lanes_before_the_proposals(scope):
    # The lane's foreign key to the proposal has no ON DELETE: deleting the proposal
    # first would fail and the whole leave with it.
    async def go():
        now = await db_now()
        account, sub = await new_account(f"baja-{scope}")
        location, _ = await new_location(account)
        open_one = await proposal(account, location, "enviada", age=DAY)
        await lane(account, location, open_one)
        rows = await owner(
            "INSERT INTO offboarding (account_id, google_sub, scope, confirmed_at, delete_at)"
            " VALUES ($1::uuid, $2, $3, $4::timestamptz, $4::timestamptz + interval '48 hours')"
            " RETURNING id::text",
            account,
            sub,
            scope,
            now - 3 * DAY,
        )
        async with Worker() as store:
            leave = await store.begin_offboarding(rows[0]["id"], now)
        left = await owner(
            "SELECT (SELECT count(*) FROM proposal WHERE account_id = $1::uuid) AS proposals,"
            " (SELECT count(*) FROM conversation_lane WHERE account_id = $1::uuid) AS lanes,"
            " (SELECT count(*) FROM location WHERE account_id = $1::uuid"
            "  AND removed_at IS NOT NULL) AS retired",
            account,
        )
        return leave, left[0]

    leave, left = run(go())
    assert leave is not None
    assert (left["proposals"], left["lanes"], left["retired"]) == (0, 0, 1)


# =============================================================================
# The activation log and the plan entitlements: 12 months after the leave.
# =============================================================================


async def billing_rows(account: str) -> tuple[int, int]:
    rows = await owner(
        "SELECT (SELECT count(*) FROM location_activation_log WHERE account_id = $1::uuid) AS log,"
        " (SELECT count(*) FROM plan_entitlement WHERE account_id = $1::uuid) AS rights",
        account,
    )
    return rows[0]["log"], rows[0]["rights"]


@pytest.mark.parametrize(
    ("months", "deleted"),
    [(None, False), (11, False), (13, True)],
    ids=["conectada", "11-meses", "13-meses"],
)
def test_the_activation_log_and_the_entitlements_go_12_months_after_the_leave(months, deleted):
    async def go():
        now = await db_now()
        account, _ = await new_account("registro")
        await new_location(account)  # one activation logged by the trigger
        if months is not None:
            await owner(
                "UPDATE account SET disconnected_at = $2 WHERE id = $1::uuid",
                account,
                now - timedelta(days=31 * months),
            )
        before = await billing_rows(account)
        counts = await retain(now)
        return before, await billing_rows(account), counts

    before, after, counts = run(go())
    assert before == (1, 1)
    assert after == ((0, 0) if deleted else (1, 1))
    if deleted:
        assert counts["location_activation_log"] >= 1 and counts["plan_entitlement"] >= 1


def test_a_longer_audit_term_also_keeps_the_log_longer():
    async def go():
        now = await db_now()
        account, _ = await new_account("largo")
        await new_location(account)
        await owner(
            "UPDATE account SET disconnected_at = $2 WHERE id = $1::uuid",
            account,
            now - timedelta(days=31 * 13),
        )
        async with Worker() as store:
            await store.run_retention(now, RetentionPolicy(audit_months=24))
        return await billing_rows(account)

    assert run(go()) == (1, 1)


def test_a_retired_profile_follows_the_term_of_its_retirement_not_of_its_deactivation():
    # Retiring an active profile also deactivates it (trigger of location). Its data
    # then go 30 days after removed_at, or with the leave, never earlier.
    async def go():
        now = await db_now()
        account, sub = await new_account("retirada-datos")
        location, gbp = await new_location(account)
        await google_data(sub, gbp)
        await owner("UPDATE location SET removed_at = $2 WHERE id = $1", location, now - 5 * DAY)
        await owner(
            "UPDATE location SET deactivated_at = $2 WHERE id = $1", location, now - 60 * DAY
        )
        await retain(now)
        return await stored(sub, gbp)

    assert run(go()) == FULL
