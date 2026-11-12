"""Multinegocio, step 3 (docs/multinegocio.md, 13): the panel per profile in the store
and the application, without a database (MemoryStore). The same rules against
PostgreSQL are in test_multinegocio_store_postgres.py.
"""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from app.adapters.memory_tables import ReviewRow
from app.adapters.quota.memory import MemoryQuota
from app.adapters.store.memory import MemoryStore, OffboardingRow
from app.application.key_dates import KeyDates
from app.application.profile_sync import ProfileSync
from app.application.review_replies import DraftRateLimited, ReviewReplies
from app.domain.plans import Plan
from app.domain.proposals import (
    OUTCOME_LOCATION_INACTIVE,
    Proposal,
    ProposalState,
    ProposalType,
)
from app.domain.quota import SCOPE, draft_scope
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

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
T0 = NOW.timestamp()


def run(coro):
    return asyncio.run(coro)


def profile(n: int, title: str | None = None) -> ProfileLocation:
    return ProfileLocation("accounts/1", f"locations/{n}", title or f"Bar {n}", "", True)


async def account_with(store: MemoryStore, sub: str, profiles) -> tuple[str, list[str]]:
    account_id = await store.upsert_account(sub, f"{sub}@example.com")
    await store.sync_locations(account_id, profiles, NOW)
    ids = []
    for p in profiles:
        row = next(r for r in store.locations if r.account_id == account_id and r.profile == p)
        ids.append(row.id)
    return account_id, ids


def key_date(account_id: str, location_id: str, day: str, state=ProposalState.SENT) -> Proposal:
    """A key date proposal, as the conversation creates it."""
    return Proposal(
        id=f"prop_{uuid.uuid4().hex[:16]}",
        account_id=account_id,
        type=ProposalType.KEY_DATE,
        source_ref=f"{location_id}:{day}",
        state=state,
        priority=10,
        created_at=NOW,
        scheduled_for=NOW,
        payload={"kind": "holiday", "date": day, "name": "Fiesta", "location_id": location_id},
        step="holiday.ask",
        step_token="abc123",
    )


# --- activation -------------------------------------------------------------


def test_activation_without_a_plan_takes_free_then_pro_until_the_limit():
    async def go():
        store = MemoryStore()
        account, ids = await account_with(store, "ana", [profile(n) for n in range(1, 7)])
        plans = [await store.activate(account, i) for i in ids[:4]]
        assert plans == [Plan.GRATIS, Plan.PRO, Plan.PRO, Plan.PRO]  # beta: 3 Pro
        with pytest.raises(LocationLimit):
            await store.activate(account, ids[4])
        with pytest.raises(LocationLimit):  # no entitlement of WhatsApp
            await store.activate(account, ids[4], Plan.PRO_WHATSAPP)
        # Already active: nothing changes without a plan; a full plan is refused.
        assert await store.activate(account, ids[0]) is Plan.GRATIS
        with pytest.raises(LocationLimit):
            await store.activate(account, ids[0], Plan.PRO)
        overview = await store.locations_overview(account, NOW.date())
        assert overview.active_by_plan() == {Plan.GRATIS: 1, Plan.PRO: 3}
        assert len(overview.active()) == 4

    run(go())


def test_the_free_plan_of_a_google_profile_is_taken_once_across_accounts():
    async def go():
        store = MemoryStore()
        ana, (bar,) = await account_with(store, "ana", [profile(1)])
        eva, (same,) = await account_with(store, "eva", [profile(1)])
        assert await store.activate(ana, bar, Plan.GRATIS) is Plan.GRATIS
        with pytest.raises(FreeTaken):
            await store.activate(eva, same, Plan.GRATIS)
        # Without a plan, the next plan with room.
        assert await store.activate(eva, same) is Plan.PRO

    run(go())


def test_activation_refuses_another_account_a_removed_profile_and_a_leave():
    async def go():
        store = MemoryStore()
        ana, (bar, gone) = await account_with(store, "ana", [profile(1), profile(2)])
        eva, (other,) = await account_with(store, "eva", [profile(3)])
        with pytest.raises(LocationNotFound):
            await store.activate(ana, other)
        with pytest.raises(LocationNotFound):
            await store.activate(ana, "loc_inventada")
        assert not await store.deactivate(ana, other)
        await store.sync_locations(ana, [profile(1)], NOW)
        with pytest.raises(LocationRemoved):
            await store.activate(ana, gone)
        store.offboardings.append(OffboardingRow("o1", ana, "ficha", NOW, NOW, None))
        with pytest.raises(OffboardingPending):
            await store.activate(ana, bar)

    run(go())


# --- which profile ----------------------------------------------------------


def test_a_profile_of_another_account_or_inactive_is_never_served():
    async def go():
        store = MemoryStore()
        ana, (bar, shop) = await account_with(store, "ana", [profile(1), profile(2)])
        eva, (eva_bar,) = await account_with(store, "eva", [profile(3)])
        with pytest.raises(NoLocationChosen):
            await store.resolve_location(ana, None)
        await store.activate(ana, bar)
        await store.activate(eva, eva_bar)
        store.tables.reviews["rev_00000000000000e1"] = ReviewRow(
            "rev_00000000000000e1", "eva", "locations/3", "g1", 5, "Bien", NOW
        )
        store.tables.reviews["rev_00000000000000a2"] = ReviewRow(
            "rev_00000000000000a2", "ana", "locations/2", "g2", 4, "Bien", NOW
        )
        with pytest.raises(LocationNotFound):
            await store.resolve_location(ana, eva_bar)
        with pytest.raises(LocationInactive):
            await store.resolve_location(ana, shop)
        assert await store.resolve_location(ana, bar) == bar
        assert await store.profile(ana, eva_bar) is None
        assert await store.profile(ana, shop) is None
        assert (await store.profile(ana, bar)).location_id == bar
        page = await store.list_reviews(ana, unanswered_only=False, limit=10, location_id=eva_bar)
        assert page.total == 0
        page = await store.list_reviews(ana, unanswered_only=False, limit=10, location_id=shop)
        assert page.total == 0
        assert await store.get_review(ana, "rev_00000000000000e1") is None
        assert await store.get_review(ana, "rev_00000000000000a2") is None  # inactive
        assert await store.sentiment(ana, NOW, eva_bar) is None
        await store.activate(ana, shop)
        review = await store.get_review(ana, "rev_00000000000000a2")
        assert review is not None and review.location_id == shop
        page = await store.list_reviews(ana, unanswered_only=False, limit=10, location_id=shop)
        assert [r.id for r in page.reviews] == ["rev_00000000000000a2"]

    run(go())


# --- deactivation -----------------------------------------------------------


def test_deactivating_cancels_its_open_proposals_and_moves_the_preference():
    async def go():
        store = MemoryStore()
        ana, (bar, shop) = await account_with(store, "ana", [profile(1), profile(2)])
        await store.activate(ana, bar)
        await store.activate(ana, shop)
        assert await store.select_location(ana, shop)
        mine = key_date(ana, shop, "2026-12-08")
        done = key_date(ana, shop, "2026-12-06", ProposalState.APPLIED)
        other = key_date(ana, bar, "2026-12-08")
        for p in (mine, done, other):
            store.key_date_proposals[p.id] = p
        assert await store.deactivate(ana, shop)
        cancelled = store.key_date_proposals[mine.id]
        assert cancelled.state is ProposalState.CANCELLED
        assert cancelled.outcome_reason == OUTCOME_LOCATION_INACTIVE
        assert cancelled.step_token != mine.step_token
        assert store.key_date_proposals[done.id] == done
        assert store.key_date_proposals[other.id] == other
        assert store._account(ana).selected_location_id == bar
        assert await store.deactivate(ana, shop)  # again: nothing to do
        assert await store.deactivate(ana, bar)
        assert store._account(ana).selected_location_id is None
        overview = await store.locations_overview(ana, NOW.date())
        assert [loc.active for loc in overview.locations] == [False, False]
        assert overview.locations[0].pending_reviews is None

    run(go())


# --- the old web: choosing is activating ------------------------------------


def test_choosing_an_inactive_profile_activates_it_until_step_9():
    async def go():
        store = MemoryStore()
        ana, ids = await account_with(store, "ana", [profile(n) for n in range(1, 6)])
        for location_id in ids[:4]:
            assert await store.select_location(ana, location_id)
        overview = await store.locations_overview(ana, NOW.date())
        assert overview.active_by_plan() == {Plan.GRATIS: 1, Plan.PRO: 3}
        with pytest.raises(LocationLimit):
            await store.select_location(ana, ids[4])
        assert store._account(ana).selected_location_id == ids[3]

    run(go())


def test_choosing_beyond_the_limit_is_402_and_another_account_is_404():
    from tests.test_me import SELECT, World

    world = World()
    client = world.connect_as("ana", "ana@example.com", [profile(n) for n in range(1, 6)])
    ids = [loc["id"] for loc in client.get("/api/v1/me").json()["locations"]]
    headers = {"X-Requested-With": "fichaitor"}
    for location_id in ids[:4]:
        assert (
            client.post(SELECT, json={"location_id": location_id}, headers=headers).status_code
            == 200
        )
    r = client.post(SELECT, json={"location_id": ids[4]}, headers=headers)
    assert r.status_code == 402 and isinstance(r.json()["detail"], str)
    r = client.post(SELECT, json={"location_id": "loc_inventada"}, headers=headers)
    assert r.status_code == 404


# --- overview ---------------------------------------------------------------


def test_the_overview_counts_what_waits_in_each_active_profile():
    async def go():
        store = MemoryStore()
        ana, (bar, shop) = await account_with(store, "ana", [profile(1, "A"), profile(2, "B")])
        await store.activate(ana, bar)
        for p in (
            key_date(ana, bar, "2026-12-08"),
            key_date(ana, bar, "2026-11-01"),  # past
            key_date(ana, bar, "2026-12-24", ProposalState.APPLIED),
        ):
            store.key_date_proposals[p.id] = p
        overview = await store.locations_overview(ana, NOW.date())
        first, second = overview.locations
        assert (first.id, first.plan, first.pending_key_dates, first.pending_reviews) == (
            bar,
            Plan.GRATIS,
            1,
            0,
        )
        assert first.summary_status is None  # nothing synced yet
        assert (second.active, second.plan, second.pending_key_dates) == (False, None, None)
        assert [e.plan for e in overview.entitlements] == [Plan.PRO]

    run(go())


# --- application ------------------------------------------------------------


class Jobs:
    def __init__(self) -> None:
        self.keys: list[str] = []
        self.payloads: list[dict] = []

    async def enqueue(self, kind, payload, *, idempotency_key=None, run_at=None, max_attempts=5):
        if idempotency_key in self.keys:
            return False
        self.keys.append(idempotency_key)
        self.payloads.append(payload)
        return True


def test_a_reconnection_syncs_every_active_profile_and_a_view_names_its_profile():
    async def go():
        store = MemoryStore()
        jobs = Jobs()
        sync = ProfileSync(
            store, jobs, window_seconds=900, stale_after_seconds=3600, clock=lambda: T0
        )
        ana, (bar, shop, off) = await account_with(
            store, "ana", [profile(1), profile(2), profile(3)]
        )
        await store.activate(ana, bar)
        await store.activate(ana, shop)
        await sync.request_for_account(ana, reconnected=True)
        assert sorted(p["location"] for p in jobs.payloads) == ["locations/1", "locations/2"]
        view = await sync.view(ana, shop)
        assert view is not None and view.location_id == shop
        assert await sync.view(ana, off) is None
        await sync.request_for_location(ana, off)
        assert len(jobs.payloads) == 3  # only the view of shop asked for one more

    run(go())


class Writer:
    async def write_reply(self, stars: int, review_text: str) -> str:
        return "¡Muchas gracias por tu visita! Nos alegra que te gustara. Te esperamos pronto."


def test_the_cap_of_drafts_is_per_profile():
    async def go():
        store = MemoryStore()
        quota = MemoryQuota()
        ana, (bar, shop) = await account_with(store, "ana", [profile(1), profile(2)])
        await store.activate(ana, bar)
        await store.activate(ana, shop)
        for rid, loc in (
            ("rev_00000000000000b1", "locations/1"),
            ("rev_00000000000000b2", "locations/1"),
            ("rev_00000000000000c1", "locations/2"),
        ):
            store.tables.reviews[rid] = ReviewRow(
                rid, "ana", loc, rid[-2:], 5, f"Genial {rid}", NOW
            )
        replies = ReviewReplies(
            store,
            Writer(),
            draft_limit=1,
            draft_window_seconds=86400,
            draft_timeout_seconds=5,
            clock=lambda: T0,
            quota=quota,
        )
        await replies.draft(ana, "rev_00000000000000b1")
        with pytest.raises(DraftRateLimited):
            await replies.draft(ana, "rev_00000000000000b2")
        assert (await replies.draft(ana, "rev_00000000000000c1")).text  # its own cap

    run(go())


def test_a_draft_scope_names_the_profile_and_fits_the_counter():
    assert draft_scope("loc_3f2a9b1c0d4e") == "review_draft:loc_3f2a9b1c0d4e"
    assert SCOPE.fullmatch(draft_scope("loc_3f2a9b1c0d4e"))
    assert not SCOPE.fullmatch("review_draft:LOC 1")


def test_key_dates_are_listed_per_profile():
    async def go():
        store = MemoryStore()
        ana, (bar, shop) = await account_with(store, "ana", [profile(1), profile(2)])
        await store.select_location(ana, bar)
        await store.activate(ana, shop)
        a = key_date(ana, bar, "2026-12-08")
        b = key_date(ana, shop, "2026-12-08")
        store.key_date_proposals.update({a.id: a, b.id: b})
        key_dates = KeyDates(store, clock=lambda: T0)
        assert [v.id for v in await key_dates.list(ana, pending_only=True)] == [a.id]
        assert [v.id for v in await key_dates.list(ana, pending_only=True, location_id=shop)] == [
            b.id
        ]
        assert (await store.get_key_date(ana, b.id)) is not None  # any active profile
        await store.deactivate(ana, shop)
        assert await key_dates.list(ana, pending_only=False, location_id=shop) == []
        assert await store.get_key_date(ana, b.id) is None

    run(go())
