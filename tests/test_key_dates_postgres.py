"""Integration: key dates on PostgreSQL, with the four roles (owner, API, worker and
conversation), against the TEST database only.

Skipped unless the four URLs are set; they must point to a database whose name ends in
`_test` (tests/conftest.py). Google is the fake adapter: nothing leaves the machine.
Every statement a test expects to be denied carries `WHERE false` or runs in a
transaction that is rolled back.
"""

import asyncio
import json
import os
import uuid
from datetime import UTC, date, datetime, timedelta

import asyncpg
import pytest

from app.adapters.conversation_store.postgres import PostgresConversationStore
from app.adapters.crypto.sealed import generate_keypair, seal
from app.adapters.google_profile.fake import FakeGoogleProfile
from app.adapters.store.postgres import PostgresStore
from app.adapters.worker_store.postgres import PostgresWorkerStore
from app.domain.jobs import Job, PermanentJobError
from app.domain.key_dates import PanelRefused
from app.domain.offboarding import RetentionPolicy
from app.domain.profile_summary import ProfileFacts, facts_to_json
from app.domain.proposals import Proposal, ProposalType
from app.domain.proposals import ProposalState as S
from app.domain.special_hours import KIND_CLOSED, KIND_PERIODS, checked
from app.ports.google_oauth import ProfileLocation
from app.ports.key_dates import KeyDateNotFound, SpecialHoursInProgress
from app.ports.store import GoogleConnection
from app.worker.set_special_hours import SetSpecialHours

API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")
WORKER_URL = os.environ.get("FICHAITOR_TEST_WORKER_URL")
CONVERSATION_URL = os.environ.get("FICHAITOR_TEST_CONVERSATION_URL")

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL and WORKER_URL and CONVERSATION_URL),
    reason="sin base de datos de pruebas configurada",
)

PUBLIC, PRIVATE = generate_keypair()
NOW = datetime(2026, 12, 3, 10, 0, tzinfo=UTC)
DAY = date(2026, 12, 8)
SUB = "pytest-kd-1"
BAR = ProfileLocation("accounts/pytest-kd", "locations/pytest-kd", "Bar Fechas", "", True)
OTHER = ProfileLocation("accounts/pytest-kd", "locations/pytest-kd2", "Otra", "", True)
DENIED = asyncpg.InsufficientPrivilegeError
OLD_DAY = {"startDate": {"year": 2027, "month": 8, "day": 15}, "closed": True}


def run(coro):
    return asyncio.run(coro)


async def _as(url, sql, *args):
    conn = await asyncpg.connect(url)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def owner(sql, *args):
    return await _as(OWNER_URL, sql, *args)


async def denied(url, sql, *args) -> bool:
    """Whether the role is refused; always inside a transaction that is rolled back."""
    conn = await asyncpg.connect(url)
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute(sql, *args)
    except DENIED:
        return True
    except asyncpg.PostgresError as exc:
        return "row-level security" in str(exc)
    finally:
        await tx.rollback()
        await conn.close()
    return False


async def cleanup():
    mine = "(SELECT id FROM account WHERE google_sub LIKE 'pytest-kd-%')"
    for sql in (
        f"UPDATE conversation SET active_proposal_id = NULL WHERE account_id IN {mine}",
        f"DELETE FROM conversation WHERE account_id IN {mine}",
        f"DELETE FROM proposal WHERE account_id IN {mine}",
        f"DELETE FROM audit_log WHERE account_id IN {mine}",
        f"DELETE FROM action WHERE account_id IN {mine}",
        "DELETE FROM job WHERE kind LIKE 'pytest-kd-%'",
        "DELETE FROM profile_summary WHERE google_sub LIKE 'pytest-kd-%'",
        "DELETE FROM google_connection WHERE google_sub LIKE 'pytest-kd-%'",
        f"UPDATE account SET selected_location_id = NULL WHERE id IN {mine}",
        f"DELETE FROM offboarding WHERE account_id IN {mine}",
        f"DELETE FROM session WHERE account_id IN {mine}",
        f"DELETE FROM location WHERE account_id IN {mine}",
        f"DELETE FROM account WHERE id IN {mine}",
    ):
        await owner(sql)


@pytest.fixture(autouse=True)
def clean():
    run(cleanup())
    yield
    run(cleanup())


class Scene:
    def __init__(self) -> None:
        self.kind = f"pytest-kd-{uuid.uuid4().hex[:10]}"
        self.api = PostgresStore(API_URL, hours_job_kind=self.kind)
        self.conv = PostgresStore(CONVERSATION_URL, hours_job_kind=self.kind)
        self.google = FakeGoogleProfile(special_hours=(OLD_DAY,))

    async def __aenter__(self):
        await self.api.connect()
        await self.conv.connect()
        self.worker_pool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=2)
        self.conv_pool = await asyncpg.create_pool(CONVERSATION_URL, min_size=1, max_size=2)
        self.worker_store = PostgresWorkerStore(self.worker_pool)
        self.conv_store = PostgresConversationStore(self.conv_pool)
        self.account_id = await self.api.upsert_account(SUB, f"{SUB}@example.com")
        await self.api.sync_locations(self.account_id, [BAR, OTHER], NOW)
        await self.api.add_google_connection(
            GoogleConnection(
                SUB,
                f"{SUB}@example.com",
                seal(PUBLIC, "refresh-kd"),
                1,
                ("scope",),
                ({"account": BAR.account, "location": BAR.location},),
            )
        )
        self.bar = await self.location_id(BAR)
        assert await self.api.select_location(self.account_id, self.bar)
        facts = ProfileFacts(
            address="Calle Elvira 1, 18010 Granada",
            region="AN",
            special_dates=(date(2026, 12, 24),),
        )
        await owner(
            "INSERT INTO profile_summary (google_sub, location, status, facts)"
            " VALUES ($1, $2, 'ok', $3::jsonb)",
            SUB,
            BAR.location,
            json.dumps(facts_to_json(facts)),
        )
        return self

    async def __aexit__(self, *exc):
        await self.worker_pool.close()
        await self.conv_pool.close()
        await self.api.close()
        await self.conv.close()

    async def location_id(self, profile):
        rows = await owner(
            "SELECT id FROM location WHERE account_id = $1::uuid AND gbp_location = $2",
            self.account_id,
            profile.location,
        )
        return rows[0]["id"]

    async def proposal(self, state=S.SENT, day=DAY, location_id=None) -> str:
        """A key date proposal, as the conversation creates it (with its own role)."""
        location_id = location_id or self.bar
        proposal = Proposal(
            id=f"prop_{uuid.uuid4().hex[:16]}",
            account_id=self.account_id,
            type=ProposalType.KEY_DATE,
            source_ref=f"{location_id}:{day}",
            state=state,
            priority=10,
            created_at=NOW,
            scheduled_for=NOW,
            payload={
                "kind": "holiday",
                "date": day.isoformat(),
                "name": "Inmaculada",
                "location_id": location_id,
            },
            step="holiday.ask",
            step_token="abc123",
        )
        async with self.conv_store.lock(self.account_id):
            assert await self.conv_store.add_proposal(proposal)
            conversation = await self.conv_store.get_conversation(self.account_id)
            from dataclasses import replace

            await self.conv_store.save_conversation(
                replace(conversation, active_proposal_id=proposal.id)
            )
        return proposal.id

    def executor(self, now=NOW):
        return SetSpecialHours(self.worker_store, self.google, PRIVATE, lambda: now)

    async def run_job(self, action_id, now=NOW):
        rows = await owner(
            "SELECT id::text AS id FROM job WHERE kind = $1 AND payload->>'action_id' = $2",
            self.kind,
            action_id,
        )
        job = Job(rows[0]["id"], self.kind, {"action_id": action_id})
        try:
            await self.executor(now)(job)
        except PermanentJobError:
            pass


def scene(scenario):
    async def go():
        async with Scene() as s:
            return await scenario(s)

    return run(go())


def test_the_conversation_creates_the_action_its_audit_row_and_its_job():
    async def scenario(s):
        change = checked(DAY, KIND_PERIODS, [("10:00", "14:00")])
        action_id = await s.conv.create_special_hours_action(s.account_id, s.bar, change, NOW)
        (action,) = await owner("SELECT * FROM action WHERE id = $1", action_id)
        assert action["type"] == "set_special_hours" and action["review_id"] is None
        assert action["special_date"] == DAY and action["text"] == change.to_text()
        assert action["location"] == BAR.location and action["state"] == "pending"
        audit = await owner("SELECT * FROM audit_log WHERE action_id = $1", action_id)
        assert [(a["event"], a["written_by"]) for a in audit] == [
            ("special_hours_confirmed", "fichaitor_conversation_dev")
        ]
        jobs = await owner("SELECT max_attempts, idempotency_key FROM job WHERE kind = $1", s.kind)
        assert [(j["max_attempts"], j["idempotency_key"]) for j in jobs] == [
            (6, f"{s.kind}:{action_id}")
        ]
        with pytest.raises(SpecialHoursInProgress):
            await s.conv.create_special_hours_action(s.account_id, s.bar, change, NOW)
        other = await s.location_id(OTHER)
        assert await s.conv.create_special_hours_action(s.account_id, other, change, NOW) is None

    scene(scenario)


def test_the_executor_merges_and_closes_with_its_audit_row_and_the_facts():
    async def scenario(s):
        change = checked(DAY, KIND_CLOSED)
        action_id = await s.api.create_special_hours_action(s.account_id, s.bar, change, NOW)
        await s.run_job(action_id)
        (action,) = await owner("SELECT state, error_code FROM action WHERE id = $1", action_id)
        assert action["state"] == "done"
        (location, sent) = s.google.special_writes[0]
        assert location == BAR.location and OLD_DAY in sent
        events = await owner(
            "SELECT event, written_by FROM audit_log WHERE action_id = $1 ORDER BY id", action_id
        )
        assert [e["event"] for e in events] == ["special_hours_confirmed", "special_hours_sent"]
        assert events[1]["written_by"] == "fichaitor_worker_dev"
        (row,) = await owner("SELECT facts FROM profile_summary WHERE google_sub = $1", SUB)
        assert json.loads(row["facts"])["special_dates"] == ["2026-12-08", "2026-12-24"]

    scene(scenario)


def test_the_executor_refuses_an_action_forged_for_a_profile_not_selected():
    async def scenario(s):
        change = checked(DAY, KIND_CLOSED)
        other = await s.location_id(OTHER)
        # Inserted by the API role straight into the table, outside its own route.
        rows = await _as(
            API_URL,
            "INSERT INTO action (account_id, type, google_sub, location, special_date, text,"
            " confirmed_at) VALUES ($1::uuid, 'set_special_hours', $2, $3, $4, $5, $6)"
            " RETURNING id",
            s.account_id,
            SUB,
            "locations/pytest-ajena",
            DAY,
            change.to_text(),
            NOW,
        )
        action_id = rows[0]["id"]
        claimed = await s.worker_store.claim_action(action_id, "pytest-job", NOW)
        assert claimed is not None and not claimed.coherent
        assert other  # the profile exists but is not that one

    scene(scenario)


def test_the_panel_answers_holding_the_thread_and_releases_it():
    async def scenario(s):
        proposal_id = await s.proposal()
        change = checked(DAY, KIND_CLOSED)
        action_id = await s.api.answer_key_date(s.account_id, proposal_id, change, NOW, NOW.date())
        (p,) = await owner("SELECT * FROM proposal WHERE id = $1", proposal_id)
        assert p["state"] == "ejecutando" and p["action_id"] == action_id
        assert p["confirmed_event_id"].startswith("panel:") and p["step_token"] != "abc123"
        assert json.loads(p["slots"])["confirmed"] == {"kind": "closed", "periods": []}
        (c,) = await owner(
            "SELECT active_proposal_id FROM conversation WHERE account_id = $1::uuid",
            s.account_id,
        )
        assert c["active_proposal_id"] is None
        audit = await owner(
            "SELECT event, written_by FROM audit_log WHERE action_id = $1", action_id
        )
        assert [(a["event"], a["written_by"]) for a in audit] == [
            ("special_hours_confirmed", "fichaitor_api_dev")
        ]
        view = await s.api.get_key_date(s.account_id, proposal_id)
        assert view.action is not None and view.action.state == "pending"
        listed = await s.api.list_key_dates(s.account_id, NOW.date())
        assert [v.id for v in listed] == [proposal_id]
        with pytest.raises(PanelRefused):
            await s.api.answer_key_date(s.account_id, proposal_id, change, NOW, NOW.date())
        # What the chat sees afterwards: the proposal is resolved.
        proposal = await s.conv_store.get_proposal(proposal_id)
        assert proposal.state is S.EXECUTING and not proposal.is_open

    scene(scenario)


def test_the_panel_dismisses_and_refuses_what_is_not_its_own():
    async def scenario(s):
        proposal_id = await s.proposal()
        await s.api.dismiss_key_date(s.account_id, proposal_id, NOW)
        (p,) = await owner("SELECT state, outcome_reason FROM proposal WHERE id = $1", proposal_id)
        assert (p["state"], p["outcome_reason"]) == ("descartada", "dismissed")
        with pytest.raises(PanelRefused):
            await s.api.dismiss_key_date(s.account_id, proposal_id, NOW)
        with pytest.raises(KeyDateNotFound):
            await s.api.dismiss_key_date("00000000-0000-0000-0000-000000000000", proposal_id, NOW)
        past = await s.proposal(day=date(2026, 12, 1))
        with pytest.raises(PanelRefused):
            await s.api.answer_key_date(
                s.account_id, past, checked(date(2026, 12, 1), KIND_CLOSED), NOW, NOW.date()
            )
        stale = await s.proposal(day=date(2026, 12, 9), location_id=await s.location_id(OTHER))
        assert await s.api.get_key_date(s.account_id, stale) is None  # another profile

    scene(scenario)


def test_the_planner_reads_the_profiles_with_the_conversation_role():
    async def scenario(s):
        (profile,) = [
            p for p in await s.conv_store.key_date_profiles() if p.account_id == s.account_id
        ]
        assert profile.location_id == s.bar and profile.region == "AN"
        assert profile.special_dates == (date(2026, 12, 24),)
        await owner(
            "UPDATE profile_summary SET status = 'needs_reconnect' WHERE google_sub = $1", SUB
        )
        assert all(p.account_id != s.account_id for p in await s.conv_store.key_date_profiles())

    scene(scenario)


def test_privileges_of_the_roles():
    async def scenario(s):
        proposal_id = await s.proposal()
        # The API moves a proposal, never what it was created with nor whose it is.
        assert await denied(API_URL, "UPDATE proposal SET payload = '{}'::jsonb WHERE false")
        assert await denied(API_URL, "UPDATE proposal SET account_id = account_id WHERE false")
        assert not await denied(
            API_URL, "UPDATE proposal SET state = state WHERE id = $1", proposal_id
        )
        # Nobody but the worker records the outcome; nobody changes the confirmed hours.
        for url in (API_URL, CONVERSATION_URL):
            assert await denied(url, "UPDATE action SET text = text WHERE false")
            assert await denied(url, "UPDATE action SET special_date = special_date WHERE false")
            assert await denied(
                url,
                "INSERT INTO audit_log (event, account_id, action_id, action_type, location,"
                " review_id, gbp_review_id, text, confirmed_at) VALUES"
                " ('special_hours_sent', $1::uuid, 'act_x', 'set_special_hours', '', '', '',"
                " 'x', now())",
                s.account_id,
            )
        assert await denied(WORKER_URL, "UPDATE action SET special_date = special_date WHERE false")
        assert await denied(
            WORKER_URL,
            "INSERT INTO audit_log (event, account_id, action_id, action_type, location,"
            " review_id, gbp_review_id, text, confirmed_at) VALUES"
            " ('special_hours_confirmed', $1::uuid, 'act_x', 'set_special_hours', '', '', '',"
            " 'x', now())",
            s.account_id,
        )
        # The conversation reads only what the planner needs of the summary.
        assert await denied(
            CONVERSATION_URL, "SELECT review_count FROM profile_summary WHERE false"
        )
        assert await denied(
            CONVERSATION_URL, "UPDATE profile_summary SET facts = facts WHERE false"
        )

    scene(scenario)


def test_the_table_refuses_an_action_without_its_day():
    async def scenario(s):
        with pytest.raises(asyncpg.CheckViolationError):
            await owner(
                "INSERT INTO action (account_id, type, google_sub, location, text, confirmed_at)"
                " VALUES ($1::uuid, 'set_special_hours', $2, $3, 'x', now())",
                s.account_id,
                SUB,
                BAR.location,
            )

    scene(scenario)


def test_retention_deletes_the_changes_of_hours_twelve_months_after_the_leave():
    async def scenario(s):
        change = checked(DAY, KIND_CLOSED)
        action_id = await s.api.create_special_hours_action(s.account_id, s.bar, change, NOW)
        # The 12 months of audit_log are a floor counted by the database's own clock.
        later = datetime.now(UTC)
        await owner(
            "UPDATE account SET disconnected_at = $2 WHERE id = $1::uuid",
            s.account_id,
            later - timedelta(days=400),
        )
        await s.worker_store.run_retention(later, RetentionPolicy())
        assert await owner("SELECT 1 FROM action WHERE id = $1", action_id) == []
        assert await owner("SELECT 1 FROM audit_log WHERE action_id = $1", action_id) == []

    scene(scenario)
