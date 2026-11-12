"""Integration: multinegocio, step 3 (docs/multinegocio.md, 13): the panel per profile
in PostgresStore, with the API role, against the TEST database only.

Skipped unless the four URLs are set. They must point to a database whose name ends in
`_test` (tests/conftest.py stops the run otherwise). Rows are created for accounts whose
google_sub starts with 'pytest-p3-' and deleted at the end; what only the conversation
or the worker would write (proposals, lanes, summaries, reviews) is written by the owner.
"""

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from app.adapters.store.postgres import PostgresStore
from app.domain.plans import Plan
from app.domain.quota import draft_scope
from app.domain.special_hours import KIND_CLOSED, checked
from app.ports.google_oauth import ProfileLocation
from app.ports.store import (
    FreeTaken,
    LocationInactive,
    LocationLimit,
    LocationNotFound,
    LocationRemoved,
    NoLocationChosen,
    OffboardingPending,
)

API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")
WORKER_URL = os.environ.get("FICHAITOR_TEST_WORKER_URL")
CONVERSATION_URL = os.environ.get("FICHAITOR_TEST_CONVERSATION_URL")

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL and WORKER_URL and CONVERSATION_URL),
    reason="sin base de datos de pruebas configurada",
)

PREFIX = "pytest-p3-"
NOW = datetime.now(UTC)
TODAY = NOW.date()
DAY = TODAY + timedelta(days=20)
MINE = f"(SELECT id FROM account WHERE google_sub LIKE '{PREFIX}%')"


def run(coro):
    return asyncio.run(coro)


async def owner(sql: str, *args):
    conn = await asyncpg.connect(OWNER_URL)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def cleanup():
    for sql in (
        f"UPDATE conversation SET active_proposal_id = NULL WHERE account_id IN {MINE}",
        f"DELETE FROM conversation_lane WHERE account_id IN {MINE}",
        f"DELETE FROM conversation WHERE account_id IN {MINE}",
        f"DELETE FROM proposal WHERE account_id IN {MINE}",
        f"DELETE FROM audit_log WHERE account_id IN {MINE}",
        f"DELETE FROM action WHERE account_id IN {MINE}",
        f"DELETE FROM job WHERE kind LIKE '{PREFIX}%'",
        "DELETE FROM usage_counter WHERE scope IN (SELECT 'review_draft:' || id FROM location"
        f" WHERE account_id IN {MINE})",
        f"DELETE FROM review_draft WHERE review_id IN (SELECT id FROM review"
        f" WHERE google_sub LIKE '{PREFIX}%')",
        f"DELETE FROM review WHERE google_sub LIKE '{PREFIX}%'",
        f"DELETE FROM sentiment_analysis WHERE google_sub LIKE '{PREFIX}%'",
        f"DELETE FROM profile_summary WHERE google_sub LIKE '{PREFIX}%'",
        f"DELETE FROM offboarding WHERE account_id IN {MINE}",
        f"UPDATE account SET selected_location_id = NULL WHERE id IN {MINE}",
        f"DELETE FROM location_activation_log WHERE account_id IN {MINE}",
        f"DELETE FROM location WHERE account_id IN {MINE}",
        f"DELETE FROM account WHERE id IN {MINE}",
    ):
        await owner(sql)


@pytest.fixture(autouse=True)
def clean():
    run(cleanup())
    yield
    run(cleanup())


def profile(n: str, title: str | None = None) -> ProfileLocation:
    return ProfileLocation("accounts/1", f"locations/{PREFIX}{n}", title or f"Bar {n}", "", True)


class Scene:
    """The API store, and helpers to set up accounts and what other roles write."""

    def __init__(self) -> None:
        self.kind = f"{PREFIX}{uuid.uuid4().hex[:8]}"
        self.api = PostgresStore(API_URL, reply_job_kind=self.kind, hours_job_kind=self.kind)

    async def __aenter__(self):
        await self.api.connect()
        return self

    async def __aexit__(self, *exc):
        await self.api.close()

    async def account(self, name: str, profiles) -> tuple[str, str, list[str]]:
        sub = f"{PREFIX}{name}-{uuid.uuid4().hex[:6]}"
        account_id = await self.api.upsert_account(sub, f"{name}@example.com")
        await self.api.sync_locations(account_id, profiles, NOW)
        rows = await owner(
            "SELECT id, gbp_location FROM location WHERE account_id = $1::uuid", account_id
        )
        by_gbp = {r["gbp_location"]: r["id"] for r in rows}
        return account_id, sub, [by_gbp[p.location] for p in profiles]

    async def proposal(self, account_id, location_id, *, state="enviada", day=DAY, kind="key_date"):
        pid = f"prop_{uuid.uuid4().hex[:16]}"
        payload = {"kind": "holiday", "date": day.isoformat(), "name": "Fiesta"}
        if kind == "key_date":
            payload["location_id"] = location_id
        confirmed = state in ("ejecutando", "aplicada")
        await owner(
            "INSERT INTO proposal (id, account_id, type, source_ref, state, payload, step,"
            " step_token, location_id, confirmed_at, confirmed_event_id)"
            " VALUES ($1, $2::uuid, $3, $4, $5, $6::jsonb, 'holiday.ask', 'abc123', $7,"
            " CASE WHEN $8 THEN now() END, CASE WHEN $8 THEN 'ev' END)",
            pid,
            account_id,
            kind,
            f"{location_id}:{pid}",
            state,
            json.dumps(payload),
            location_id,
            confirmed,
        )
        return pid

    @staticmethod
    async def summary(sub, gbp, unanswered=3, status="ok"):
        await owner(
            "INSERT INTO profile_summary (google_sub, location, status, unanswered_count)"
            " VALUES ($1, $2, $3, $4)",
            sub,
            gbp,
            status,
            unanswered,
        )

    @staticmethod
    async def review(sub, gbp) -> str:
        rows = await owner(
            "INSERT INTO review (google_sub, location, gbp_review_id, rating, comment)"
            " VALUES ($1, $2, $3, 5, 'Muy bien') RETURNING id",
            sub,
            gbp,
            uuid.uuid4().hex[:12],
        )
        return rows[0]["id"]


def scene(scenario):
    async def go():
        async with Scene() as s:
            return await scenario(s)

    return run(go())


async def state(location_id):
    (row,) = await owner(
        "SELECT plan, active_since, deactivated_at FROM location WHERE id = $1", location_id
    )
    return row


# --- activation -------------------------------------------------------------


def test_activation_takes_free_then_pro_and_the_database_refuses_beyond():
    async def scenario(s):
        account, _, ids = await s.account("ana", [profile(f"a{n}") for n in range(6)])
        plans = [await s.api.activate(account, i) for i in ids[:4]]
        assert plans == [Plan.GRATIS, Plan.PRO, Plan.PRO, Plan.PRO]
        with pytest.raises(LocationLimit):
            await s.api.activate(account, ids[4])
        with pytest.raises(LocationLimit):
            await s.api.activate(account, ids[4], Plan.PRO_WHATSAPP)
        assert (await state(ids[4]))["plan"] is None
        # Already active: without a plan it stays; to a full plan, refused.
        assert await s.api.activate(account, ids[0]) is Plan.GRATIS
        with pytest.raises(LocationLimit):
            await s.api.activate(account, ids[0], Plan.PRO)
        # A change of plan keeps active_since: the oldest still pays in full.
        before = (await state(ids[1]))["active_since"]
        await s.api.deactivate(account, ids[0])
        assert await s.api.activate(account, ids[1], Plan.GRATIS) is Plan.GRATIS
        assert (await state(ids[1]))["active_since"] == before
        log = await owner(
            "SELECT event, plan, reason, written_by FROM location_activation_log"
            " WHERE account_id = $1::uuid ORDER BY id",
            account,
        )
        assert [(r["event"], r["plan"]) for r in log] == [
            ("activated", "gratis"),
            ("activated", "pro"),
            ("activated", "pro"),
            ("activated", "pro"),
            ("deactivated", "gratis"),
            ("plan_changed", "gratis"),
        ]
        assert {(r["reason"], r["written_by"]) for r in log} == {("owner", "fichaitor_api_dev")}

    scene(scenario)


def test_the_free_plan_of_a_google_profile_is_taken_once_and_the_default_moves_on():
    async def scenario(s):
        shared = profile("shared")
        ana, _, (bar,) = await s.account("ana", [shared])
        eva, _, (same,) = await s.account("eva", [shared])
        assert await s.api.activate(ana, bar, Plan.GRATIS) is Plan.GRATIS
        with pytest.raises(FreeTaken):
            await s.api.activate(eva, same, Plan.GRATIS)
        assert await s.api.activate(eva, same) is Plan.PRO
        assert (await state(same))["plan"] == "pro"

    scene(scenario)


def test_two_activations_at_once_take_the_last_slot_only_once():
    async def scenario(s):
        account, _, (one, two) = await s.account("ana", [profile("c1"), profile("c2")])
        other = PostgresStore(API_URL)
        await other.connect()
        try:
            results = await asyncio.gather(
                s.api.activate(account, one, Plan.GRATIS),
                other.activate(account, two, Plan.GRATIS),
                return_exceptions=True,
            )
        finally:
            await other.close()
        assert sorted(type(r).__name__ for r in results) == ["LocationLimit", "Plan"]

    scene(scenario)


def test_activation_refuses_another_account_a_removed_profile_and_a_scheduled_leave():
    async def scenario(s):
        ana, sub, (bar, gone) = await s.account("ana", [profile("r1"), profile("r2")])
        eva, _, (eva_bar,) = await s.account("eva", [profile("r3")])
        with pytest.raises(LocationNotFound):
            await s.api.activate(ana, eva_bar)
        with pytest.raises(LocationNotFound):
            await s.api.activate(ana, "loc_000000000000")
        assert not await s.api.deactivate(ana, eva_bar)
        assert (await state(eva_bar))["plan"] is None
        await s.api.sync_locations(ana, [profile("r1")], NOW)
        with pytest.raises(LocationRemoved):
            await s.api.activate(ana, gone)
        await owner(
            "INSERT INTO offboarding (account_id, scope, google_sub, confirmed_at, delete_at)"
            " VALUES ($1::uuid, 'ficha', $2, now(), now() + interval '48 hours')",
            ana,
            sub,
        )
        with pytest.raises(OffboardingPending):
            await s.api.activate(ana, bar)

    scene(scenario)


# --- which profile ----------------------------------------------------------


def test_a_profile_of_another_account_or_inactive_is_never_served():
    async def scenario(s):
        ana, ana_sub, (bar, shop) = await s.account("ana", [profile("i1"), profile("i2")])
        eva, eva_sub, (eva_bar,) = await s.account("eva", [profile("i3")])
        with pytest.raises(NoLocationChosen):
            await s.api.resolve_location(ana, None)
        await s.api.activate(ana, bar)
        await s.api.activate(eva, eva_bar)
        for sub, gbp in ((ana_sub, profile("i1").location), (eva_sub, profile("i3").location)):
            await s.summary(sub, gbp)
            await owner(
                "INSERT INTO sentiment_analysis (google_sub, location, status, generated_at)"
                " VALUES ($1, $2, 'error', now())",
                sub,
                gbp,
            )
        eva_review = await s.review(eva_sub, profile("i3").location)
        shop_review = await s.review(ana_sub, profile("i2").location)
        eva_date = await s.proposal(eva, eva_bar)
        shop_date = await s.proposal(ana, shop)

        with pytest.raises(LocationNotFound):
            await s.api.resolve_location(ana, eva_bar)
        with pytest.raises(LocationNotFound):
            await s.api.resolve_location(ana, "loc_000000000000")
        with pytest.raises(LocationInactive):
            await s.api.resolve_location(ana, shop)
        assert await s.api.resolve_location(ana, bar) == bar
        for location_id in (eva_bar, shop, "loc_000000000000"):
            assert await s.api.profile(ana, location_id) is None
            assert await s.api.sentiment(ana, NOW, location_id) is None
            page = await s.api.list_reviews(
                ana, unanswered_only=False, limit=10, location_id=location_id
            )
            assert page.total == 0
            assert await s.api.list_key_dates(ana, TODAY, location_id) == []
            change = checked(DAY, KIND_CLOSED)
            assert await s.api.create_special_hours_action(ana, location_id, change, NOW) is None
        for review_id in (eva_review, shop_review):
            assert await s.api.get_review(ana, review_id) is None
        for proposal_id in (eva_date, shop_date):
            assert await s.api.get_key_date(ana, proposal_id) is None
        mine = await s.api.profile(ana, bar)
        assert mine.location_id == bar and mine.summary.unanswered_count == 3
        assert await s.api.sentiment(ana, NOW, bar) is not None

        # Once active, the other profile is served by its id, and its reviews by theirs.
        await s.api.activate(ana, shop)
        review = await s.api.get_review(ana, shop_review)
        assert review is not None and review.location_id == shop
        page = await s.api.list_reviews(ana, unanswered_only=False, limit=10, location_id=shop)
        assert [r.id for r in page.reviews] == [shop_review]
        assert [v.id for v in await s.api.list_key_dates(ana, TODAY, shop)] == [shop_date]
        assert (await s.api.get_key_date(ana, shop_date)) is not None
        # Without an id, the preference: still the first one chosen.
        assert await s.api.select_location(ana, bar)
        assert (await s.api.selected_profile(ana)).location_id == bar
        page = await s.api.list_reviews(ana, unanswered_only=False, limit=10)
        assert page.total == 0

    scene(scenario)


def test_a_retired_preference_still_says_it_lost_access():
    async def scenario(s):
        ana, _, (bar, shop) = await s.account("ana", [profile("l1"), profile("l2")])
        assert await s.api.select_location(ana, bar)
        await owner(
            "UPDATE location SET removed_at = now() WHERE id = $1", bar
        )  # as the worker (B5) does
        assert await s.api.resolve_location(ana, None) == bar
        lost = await s.api.profile(ana)
        assert lost.access_lost and lost.location_id == bar
        assert (await state(bar))["plan"] is None
        with pytest.raises(LocationInactive):
            await s.api.resolve_location(ana, shop)
        await owner("UPDATE location SET removed_at = now() WHERE id = $1", shop)
        with pytest.raises(LocationNotFound):  # named, and not the preference
            await s.api.resolve_location(ana, shop)

    scene(scenario)


# --- deactivation -----------------------------------------------------------


def test_deactivation_cancels_open_proposals_and_frees_lane_and_thread_together():
    async def scenario(s):
        ana, _, (bar, shop) = await s.account("ana", [profile("d1"), profile("d2")])
        eva, _, (eva_bar,) = await s.account("eva", [profile("d3")])
        await s.api.activate(ana, bar)
        await s.api.activate(ana, shop)
        await s.api.activate(eva, eva_bar)
        assert await s.api.select_location(ana, shop)
        open_one = await s.proposal(ana, shop)
        collecting = await s.proposal(ana, shop, state="recopilando", kind="review_reply")
        done = await s.proposal(ana, shop, state="aplicada")
        running = await s.proposal(ana, shop, state="ejecutando")
        other = await s.proposal(ana, bar)
        foreign = await s.proposal(eva, eva_bar)
        await owner(
            "INSERT INTO conversation (account_id, active_proposal_id) VALUES ($1::uuid, $2)",
            ana,
            open_one,
        )
        await owner(
            "INSERT INTO conversation_lane (account_id, location_id, active_proposal_id)"
            " VALUES ($1::uuid, $2, $3), ($1::uuid, $4, $5)",
            ana,
            shop,
            collecting,
            bar,
            other,
        )
        await owner(
            "INSERT INTO conversation_lane (account_id, location_id, active_proposal_id)"
            " VALUES ($1::uuid, $2, $3)",
            eva,
            eva_bar,
            foreign,
        )

        assert await s.api.deactivate(ana, shop)

        rows = await owner(
            "SELECT id, state, outcome_reason, step_token, message_refs::text AS refs"
            " FROM proposal WHERE account_id IN ($1::uuid, $2::uuid)",
            ana,
            eva,
        )
        by_id = {r["id"]: r for r in rows}
        for pid in (open_one, collecting):
            row = by_id[pid]
            assert (row["state"], row["outcome_reason"]) == ("cancelada", "location_inactive")
            assert row["step_token"] != "abc123" and row["refs"] == "{}"
        assert by_id[done]["state"] == "aplicada"
        assert by_id[running]["state"] == "ejecutando"  # its action decides (step 5)
        assert by_id[other]["state"] == "enviada" and by_id[foreign]["state"] == "enviada"
        lanes = await owner(
            "SELECT location_id, active_proposal_id FROM conversation_lane"
            " WHERE account_id IN ($1::uuid, $2::uuid)",
            ana,
            eva,
        )
        assert {r["location_id"]: r["active_proposal_id"] for r in lanes} == {
            shop: None,
            bar: other,
            eva_bar: foreign,
        }
        (thread,) = await owner(
            "SELECT active_proposal_id FROM conversation WHERE account_id = $1::uuid", ana
        )
        assert thread["active_proposal_id"] is None
        shop_state = await state(shop)
        assert shop_state["plan"] is None and shop_state["deactivated_at"] is not None
        # The preference moved to the oldest active profile.
        (account,) = await owner(
            "SELECT selected_location_id FROM account WHERE id = $1::uuid", ana
        )
        assert account["selected_location_id"] == bar
        # Again: nothing to do, not an error. The last one: no preference left.
        assert await s.api.deactivate(ana, shop)
        assert await s.api.deactivate(ana, bar)
        (account,) = await owner(
            "SELECT selected_location_id FROM account WHERE id = $1::uuid", ana
        )
        assert account["selected_location_id"] is None
        with pytest.raises(NoLocationChosen):
            await s.api.resolve_location(ana, None)

    scene(scenario)


def test_deactivation_is_all_or_nothing():
    async def scenario(s):
        ana, _, (bar,) = await s.account("ana", [profile("t1")])
        await s.api.activate(ana, bar)
        pid = await s.proposal(ana, bar)
        # The last statement (releasing the thread) is made to fail with a trigger of
        # this test, only for this account: the profile and its proposals stay as they were.
        await owner(
            "INSERT INTO conversation (account_id, active_proposal_id) VALUES ($1::uuid, $2)",
            ana,
            pid,
        )
        await owner(
            "CREATE OR REPLACE FUNCTION pytest_p3_fail() RETURNS trigger LANGUAGE plpgsql AS"
            " $$ BEGIN RAISE EXCEPTION 'pytest_p3'; END $$"
        )
        await owner(
            "CREATE TRIGGER pytest_p3_fail BEFORE UPDATE ON conversation FOR EACH ROW"
            f" WHEN (NEW.account_id = '{ana}'::uuid) EXECUTE FUNCTION pytest_p3_fail()"
        )
        try:
            with pytest.raises(asyncpg.RaiseError):
                await s.api.deactivate(ana, bar)
        finally:
            await owner("DROP TRIGGER IF EXISTS pytest_p3_fail ON conversation")
            await owner("DROP FUNCTION IF EXISTS pytest_p3_fail()")
        assert (await state(bar))["plan"] == "gratis"
        (row,) = await owner("SELECT state FROM proposal WHERE id = $1", pid)
        assert row["state"] == "enviada"

    scene(scenario)


# --- the old web: choosing is activating ------------------------------------


def test_choosing_an_inactive_profile_activates_it_until_step_9():
    async def scenario(s):
        ana, _, ids = await s.account("ana", [profile(f"s{n}") for n in range(5)])
        for location_id in ids[:4]:
            assert await s.api.select_location(ana, location_id)
        plans = [(await state(i))["plan"] for i in ids]
        assert plans == ["gratis", "pro", "pro", "pro", None]
        with pytest.raises(LocationLimit):
            await s.api.select_location(ana, ids[4])
        (account,) = await owner(
            "SELECT selected_location_id FROM account WHERE id = $1::uuid", ana
        )
        assert account["selected_location_id"] == ids[3]  # unchanged
        eva, _, _ = await s.account("eva", [profile("s9")])
        assert not await s.api.select_location(eva, ids[4])

    scene(scenario)


# --- overview and the rest --------------------------------------------------


def test_the_overview_counts_what_waits_in_each_active_profile():
    async def scenario(s):
        ana, sub, (bar, shop) = await s.account("ana", [profile("o1", "A"), profile("o2", "B")])
        await s.api.activate(ana, bar)
        await s.summary(sub, profile("o1").location, unanswered=4, status="needs_reconnect")
        await s.summary(sub, profile("o2").location, unanswered=9)
        await s.proposal(ana, bar)
        await s.proposal(ana, bar, state="caducada")  # still answerable from the panel
        await s.proposal(ana, bar, day=TODAY - timedelta(days=1))
        await s.proposal(ana, bar, state="aplicada")
        await s.proposal(ana, shop)
        overview = await s.api.locations_overview(ana, TODAY)
        first, second = overview.locations
        assert (first.id, first.plan, first.summary_status) == (bar, Plan.GRATIS, "needs_reconnect")
        assert (first.pending_reviews, first.pending_key_dates) == (4, 2)
        assert first.active_since is not None
        assert (second.id, second.active, second.summary_status) == (shop, False, None)
        assert (second.pending_reviews, second.pending_key_dates) == (None, None)
        assert [(e.plan, e.quantity) for e in overview.entitlements] == [(Plan.PRO, 3)]
        assert overview.active_by_plan() == {Plan.GRATIS: 1}
        assert [p.location_id for p in await s.api.active_profiles(ana)] == [bar]

    scene(scenario)


def test_a_reply_goes_to_any_active_profile_and_its_cap_is_per_profile():
    async def scenario(s):
        ana, sub, (bar, shop) = await s.account("ana", [profile("p1"), profile("p2")])
        await s.api.activate(ana, bar)
        review = await s.review(sub, profile("p2").location)
        assert await s.api.create_reply_action(ana, review, "Gracias", NOW) is None
        await s.api.activate(ana, shop)
        action_id = await s.api.create_reply_action(ana, review, "Gracias", NOW)
        assert action_id is not None
        await s.api.save_draft(ana, review, "h" * 64, "Gracias de nuevo", NOW)
        stored = await s.api.get_review(ana, review)
        assert stored.draft is not None and stored.location_id == shop
        # The counter of drafts of a profile fits the column (usage_counter.scope).
        assert await s.api.take_quota(draft_scope(shop), 1, 86400, NOW)
        assert not await s.api.take_quota(draft_scope(shop), 1, 86400, NOW)
        assert await s.api.take_quota(draft_scope(bar), 1, 86400, NOW)
        change = checked(DAY, KIND_CLOSED)
        assert await s.api.create_special_hours_action(ana, shop, change, NOW) is not None

    scene(scenario)
