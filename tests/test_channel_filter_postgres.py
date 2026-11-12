"""Integration: step 7 of docs/multinegocio.md (the filter of each chat) and the
«pendiente» of a summary being read again, with the API role (and the conversation's
for linking), against the TEST database only.

Skipped unless the four URLs are set. They must point to a database whose name ends in
`_test` (tests/conftest.py stops the run otherwise). Create it with
scripts/preparar_bd_pruebas.sh.

What it covers:
- a code asked for with a filter keeps it, and the conversation copies it to the chat it
  links, in the same statement that creates it;
- the API changes the filter of its own chats only, and the database refuses a profile
  of another account (location_filter_guard), also in a code;
- a sync_profile waiting or running, asked for after the last attempt, turns a failed
  summary into «pendiente» (summary and GET /me), through the view profile_sync_pending:
  the API still cannot read the queue.

Rows are created for accounts whose Google user starts with 'filtro-pytest-' and
deleted at the end, with the jobs of their profiles.
"""

import asyncio
import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from app.adapters.conversation_store.postgres import PostgresConversationStore
from app.adapters.store.postgres import PostgresStore
from app.api.summary_out import to_summary_response
from app.application.profile_sync import ProfileSync
from app.domain import link_codes
from app.domain.profile_summary import STATUS_PENDING, shown_status
from app.ports.google_oauth import ProfileLocation

API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")
WORKER_URL = os.environ.get("FICHAITOR_TEST_WORKER_URL")
CONVERSATION_URL = os.environ.get("FICHAITOR_TEST_CONVERSATION_URL")

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL and WORKER_URL and CONVERSATION_URL),
    reason="sin base de datos de pruebas configurada",
)

# Not "pytest-...": other test files clean every account whose user starts so.
PREFIX = "filtro-pytest-"
MINE = f"(SELECT id FROM account WHERE google_sub LIKE '{PREFIX}%')"
NOW = datetime.now(UTC)


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
        f"DELETE FROM job WHERE kind = 'sync_profile' AND payload->>'google_sub' LIKE '{PREFIX}%'",
        f"DELETE FROM channel_identity WHERE account_id IN {MINE}",
        f"DELETE FROM link_code WHERE account_id IN {MINE}",
        f"DELETE FROM profile_summary WHERE google_sub LIKE '{PREFIX}%'",
        f"UPDATE account SET selected_location_id = NULL WHERE id IN {MINE}",
        f"DELETE FROM location WHERE account_id IN {MINE}",
        f"DELETE FROM location_activation_log WHERE account_id IN {MINE}",
        f"DELETE FROM plan_entitlement WHERE account_id IN {MINE}",
        f"DELETE FROM account WHERE id IN {MINE}",
    ):
        await owner(sql)


@pytest.fixture(autouse=True)
def clean():
    run(cleanup())
    yield
    run(cleanup())


def place(name: str) -> ProfileLocation:
    return ProfileLocation("accounts/1", f"locations/{PREFIX}{name}", f"Bar {name}", "", True)


class Scene:
    def __init__(self) -> None:
        self.api = PostgresStore(API_URL)
        self.pool: asyncpg.Pool | None = None
        self.conversation: PostgresConversationStore | None = None

    async def __aenter__(self):
        await self.api.connect()
        self.pool = await asyncpg.create_pool(CONVERSATION_URL, min_size=1, max_size=2)
        self.conversation = PostgresConversationStore(self.pool)
        return self

    async def __aexit__(self, *exc):
        await self.api.close()
        await self.pool.close()

    async def account(self, name: str, n: int = 2) -> tuple[str, str, list[str]]:
        """An account with `n` profiles, all active (one free, the rest Pro)."""
        sub = f"{PREFIX}{name}-{uuid.uuid4().hex[:6]}"
        account_id = await self.api.upsert_account(sub, f"{name}@example.com")
        profiles = [place(f"{name}{i}-{uuid.uuid4().hex[:6]}") for i in range(n)]
        await self.api.sync_locations(account_id, profiles, NOW)
        rows = await owner(
            "SELECT id, gbp_location FROM location WHERE account_id = $1::uuid", account_id
        )
        by_gbp = {r["gbp_location"]: r["id"] for r in rows}
        ids = [by_gbp[p.location] for p in profiles]
        for location_id in ids:
            await self.api.activate(account_id, location_id)
        return account_id, sub, ids


def scene(scenario):
    async def go():
        async with Scene() as s:
            return await scenario(s)

    return run(go())


# --- the filter of each chat ------------------------------------------------------------


def test_a_code_with_a_filter_links_a_chat_with_that_filter_from_the_first_moment():
    async def scenario(s):
        account, _, (a, b) = await s.account("ana")
        issued = link_codes.issue_link(account, NOW, "", (b,))
        await s.api.save_link_code(issued.record)
        hashed = link_codes.code_hash(issued.token)
        record = await s.conversation.get_link_code(hashed)
        assert record.location_filter == (b,)
        assert await s.conversation.consume_link_code(hashed, NOW) == account
        result = await s.conversation.link_identity(
            account, "telegram", "4242424242", NOW, record.location_filter
        )
        assert result.identity.location_filter == (b,)
        assert [i.id for i in await s.conversation.identities_for(account, b)] == [
            result.identity.id
        ]
        assert await s.conversation.identities_for(account, a) == []
        # The API lists it with its filter.
        (listed,) = await s.api.list_channels(account)
        assert listed.location_filter == (b,)
        # A code without a filter links a chat that sees everything.
        plain = link_codes.issue_link(account, NOW, "")
        await s.api.save_link_code(plain.record)
        assert (await s.conversation.get_link_code(plain.record.code_hash)).location_filter is None

    scene(scenario)


def test_the_api_changes_the_filter_of_its_own_chats_only():
    async def scenario(s):
        account, _, (a, b) = await s.account("ana")
        other, _, (theirs, _x) = await s.account("eva")
        linked = await s.conversation.link_identity(account, "telegram", "4242424243", NOW)
        identity = linked.identity
        changed = await s.api.set_channel_filter(account, identity.id, (a,))
        assert changed is not None and changed.location_filter == (a,)
        assert await s.conversation.identities_for(account, b) == []
        # A profile of another account: the database refuses it, nothing changes.
        assert await s.api.set_channel_filter(account, identity.id, (theirs,)) is None
        assert await s.api.set_channel_filter(account, identity.id, (a, "loc_nada0000")) is None
        # A chat of another account: nothing.
        assert await s.api.set_channel_filter(other, identity.id, None) is None
        (row,) = await owner(
            "SELECT location_filter FROM channel_identity WHERE id = $1", identity.id
        )
        assert row["location_filter"] == [a]
        # null: every profile again.
        changed = await s.api.set_channel_filter(account, identity.id, None)
        assert changed.location_filter is None
        assert len(await s.conversation.identities_for(account, b)) == 1
        # Unlinked: nothing.
        assert await s.api.unlink_channel(account, identity.id, NOW)
        assert await s.api.set_channel_filter(account, identity.id, (a,)) is None

    scene(scenario)


def test_a_code_with_a_profile_of_another_account_is_refused_by_the_database():
    async def scenario(s):
        account, _, _ = await s.account("ana")
        _, _, (theirs, _x) = await s.account("eva")
        issued = link_codes.issue_link(account, NOW, "", (theirs,))
        with pytest.raises(asyncpg.CheckViolationError):
            await s.api.save_link_code(issued.record)

    scene(scenario)


def test_deactivating_a_profile_keeps_the_filter_and_the_chat_gets_nothing_of_it():
    async def scenario(s):
        account, _, (a, b) = await s.account("ana")
        linked = await s.conversation.link_identity(account, "telegram", "4242424244", NOW, (a,))
        assert await s.api.deactivate(account, a)
        (row,) = await owner(
            "SELECT location_filter FROM channel_identity WHERE id = $1", linked.identity.id
        )
        assert row["location_filter"] == [a]
        # Nothing of the inactive profile is proposed, so the chat filtered to it is
        # quiet; activated again, it sees it again.
        assert a not in [loc for loc in await s.conversation.active_locations(account)]
        await s.api.activate(account, a)
        assert len(await s.conversation.identities_for(account, a)) == 1
        assert b

    scene(scenario)


# --- «pendiente» while a sync asked for after the last attempt runs ------------------


async def failed_summary(sub: str, gbp_location: str, status: str, at: datetime) -> None:
    await owner(
        "INSERT INTO profile_summary (google_sub, location, status, status_at)"
        " VALUES ($1, $2, $3, $4)",
        sub,
        gbp_location,
        status,
        at,
    )


async def gbp_of(location_id: str) -> str:
    (row,) = await owner("SELECT gbp_location FROM location WHERE id = $1", location_id)
    return row["gbp_location"]


@pytest.mark.parametrize("status", ["needs_reconnect", "error"])
def test_a_sync_asked_for_after_a_failed_summary_shows_pendiente_until_it_ends(status):
    async def scenario(s):
        account, sub, (a, b) = await s.account("ana")
        await failed_summary(sub, await gbp_of(a), status, NOW - timedelta(hours=1))
        await failed_summary(sub, await gbp_of(b), "ok", NOW - timedelta(hours=1))

        profile = await s.api.profile(account, a)
        assert profile.summary.sync_requested_at is None
        assert shown_status(status, profile.summary.status_at, None) == status

        # Coming back from connecting with Google: the API enqueues a sync of each
        # active profile (the real kind, in the real queue).
        sync = ProfileSync(
            s.api, s.api, window_seconds=900, stale_after_seconds=6 * 3600, clock=time.time
        )
        await sync.request_for_account(account, reconnected=True)

        view = await sync.view(account, a)
        assert view.summary.sync_requested_at is not None
        body = to_summary_response(view)
        assert body.status == STATUS_PENDING
        assert body.synced_at is None and body.review_count is None
        assert body.stale is False and body.location_id == a
        overview = await s.api.locations_overview(account, NOW.date())
        shown = {
            loc.id: shown_status(loc.summary_status, loc.summary_status_at, loc.sync_requested_at)
            for loc in overview.locations
        }
        # An ok summary is never hidden by a sync.
        assert shown == {a: STATUS_PENDING, b: "ok"}

        # The worker takes it: still pendiente while it runs.
        await owner(
            "UPDATE job SET state = 'running', locked_at = now()"
            " WHERE kind = 'sync_profile' AND payload->>'google_sub' = $1",
            sub,
        )
        assert to_summary_response(await sync.view(account, a)).status == STATUS_PENDING
        # It ends (and writes the summary, here again a failure): its own status.
        await owner(
            "UPDATE job SET state = 'done', finished_at = now()"
            " WHERE kind = 'sync_profile' AND payload->>'google_sub' = $1",
            sub,
        )
        await owner(
            "UPDATE profile_summary SET status_at = now() WHERE google_sub = $1 AND location = $2",
            sub,
            await gbp_of(a),
        )
        assert to_summary_response(await sync.view(account, a)).status == status

    scene(scenario)


def test_only_due_syncs_newer_than_the_last_attempt_count():
    async def scenario(s):
        account, sub, (a, _b) = await s.account("ana")
        gbp = await gbp_of(a)
        await failed_summary(sub, gbp, "needs_reconnect", NOW - timedelta(minutes=5))

        async def job(created: datetime, run_at: datetime, state: str = "pending") -> None:
            await owner(
                "INSERT INTO job (kind, payload, state, created_at, run_at)"
                " VALUES ('sync_profile', jsonb_build_object('google_sub', $1::text,"
                " 'location', $2::text), $3, $4, $5)",
                sub,
                gbp,
                state,
                created,
                run_at,
            )

        # Asked for before the last attempt, a later slot of the planner, and a finished
        # one: none of them is a read in course.
        await job(NOW - timedelta(minutes=10), NOW - timedelta(minutes=10))
        await job(NOW, NOW + timedelta(minutes=20))
        await job(NOW, NOW - timedelta(seconds=5), "done")
        await job(NOW, NOW - timedelta(seconds=5), "failed")
        profile = await s.api.profile(account, a)
        assert profile.summary.sync_requested_at is not None  # the old one is still due
        assert (
            shown_status(
                profile.summary.status, profile.summary.status_at, profile.summary.sync_requested_at
            )
            == "needs_reconnect"
        )
        await job(NOW, NOW - timedelta(seconds=1))
        profile = await s.api.profile(account, a)
        assert (
            shown_status(
                profile.summary.status, profile.summary.status_at, profile.summary.sync_requested_at
            )
            == STATUS_PENDING
        )

    scene(scenario)


def test_the_api_reads_the_view_and_still_not_the_queue():
    async def go():
        conn = await asyncpg.connect(API_URL)
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.fetch("SELECT 1 FROM job WHERE false")
            columns = await conn.fetch("SELECT * FROM profile_sync_pending WHERE false")
            assert columns == []
            names = await conn.fetch(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'profile_sync_pending' ORDER BY ordinal_position"
            )
            assert [r["column_name"] for r in names] == ["google_sub", "location", "requested_at"]
        finally:
            await conn.close()
        for url in (WORKER_URL, CONVERSATION_URL):
            conn = await asyncpg.connect(url)
            try:
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await conn.fetch("SELECT 1 FROM profile_sync_pending WHERE false")
            finally:
                await conn.close()
        # Nothing but sync_profile shows through it: a mail job is not there.
        await owner(
            "INSERT INTO job (kind, payload) VALUES ('send_mail',"
            f" jsonb_build_object('google_sub', '{PREFIX}x', 'location', 'locations/x'))"
        )
        try:
            conn = await asyncpg.connect(API_URL)
            try:
                rows = await conn.fetch(
                    "SELECT * FROM profile_sync_pending WHERE google_sub = $1", f"{PREFIX}x"
                )
            finally:
                await conn.close()
            assert rows == []
        finally:
            await owner(
                "DELETE FROM job WHERE kind = 'send_mail' AND payload->>'google_sub' = $1",
                f"{PREFIX}x",
            )

    run(go())
