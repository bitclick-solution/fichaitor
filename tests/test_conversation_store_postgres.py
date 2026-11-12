"""Integration: the conversation on PostgreSQL, with the four roles (owner, API, worker
and conversation), against the TEST database only.

Skipped unless the four URLs are set. They must point to a database whose name ends in
`_test` (tests/conftest.py stops the run otherwise). Create it with
scripts/preparar_bd_pruebas.sh:

    FICHAITOR_TEST_OWNER_URL=postgresql://fichaitor:dev@127.0.0.1:5433/fichaitor_test
    FICHAITOR_TEST_DATABASE_URL=postgresql://fichaitor_api_dev:dev@127.0.0.1:5433/fichaitor_test
    FICHAITOR_TEST_WORKER_URL=postgresql://fichaitor_worker_dev:dev@127.0.0.1:5433/fichaitor_test
    FICHAITOR_TEST_CONVERSATION_URL=
        postgresql://fichaitor_conversation_dev:dev@127.0.0.1:5433/fichaitor_test

(the four in the environment), then `uv run pytest -q tests/test_conversation_store_postgres.py`.

Telegram, Google and the model are fakes: nothing leaves the machine. Every statement a
test expects to be denied carries `WHERE false` or runs in a transaction rolled back.
"""

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from app.adapters.conversation_store.postgres import PostgresConversationStore
from app.adapters.crypto.sealed import generate_keypair, seal
from app.adapters.google_profile.fake import FakeGoogleProfile
from app.adapters.job_queue.postgres import PostgresJobQueue
from app.adapters.messaging.fake import FakeChannel
from app.adapters.proposal_actions.review_replies import ReviewReplyActions
from app.adapters.screenshot_reader.fake import FakeScreenshotReader
from app.adapters.store.postgres import PostgresStore
from app.adapters.worker_store.postgres import PostgresWorkerStore
from app.application.conversation_service import ConversationService, ServiceConfig
from app.application.review_replies import ReviewReplies
from app.domain import channel_jobs
from app.domain import link_codes as lc
from app.domain.channel_messages import InboundEvent, InboundKind
from app.domain.conversation import QuietHours
from app.domain.jobs import Job
from app.domain.offboarding import RetentionPolicy
from app.domain.profile_summary import Review, ReviewSet
from app.domain.proposals import ProposalState as S
from app.ports.channel_links import LinkCodeTaken
from app.ports.google_oauth import ProfileLocation
from app.ports.store import GoogleConnection
from app.worker.channel_action_result import ChannelActionResult
from app.worker.channel_planner import ChannelPlanner
from app.worker.propose_review import ProposeReview
from app.worker.reply_review import ReplyReview

API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")
WORKER_URL = os.environ.get("FICHAITOR_TEST_WORKER_URL")
CONVERSATION_URL = os.environ.get("FICHAITOR_TEST_CONVERSATION_URL")

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL and WORKER_URL and CONVERSATION_URL),
    reason="sin base de datos de pruebas configurada",
)

PUBLIC, PRIVATE = generate_keypair()
NOW = datetime(2027, 3, 10, 11, 0, tzinfo=UTC)  # 12:00 in Madrid
SUB = "pytest-chan-1"
OTHER_SUB = "pytest-chan-2"
BAR = ProfileLocation("accounts/pytest-c", "locations/pytest-chan", "Bar Canal", "place-c", True)
CHATS = ("880001", "880002", "880003", "880004", "880099")
OWNER_CHAT, MANAGER_CHAT, STRANGER = CHATS[0], CHATS[1], CHATS[4]
PEPPER = "pimienta-de-pruebas"
DENIED = asyncpg.InsufficientPrivilegeError

REVIEWS = ReviewSet(
    (
        Review(
            rating=1,
            created_at=NOW - timedelta(hours=2),
            replied=False,
            text="La comida llegó fría.",
            review_id="pytest-chan-low",
        ),
        Review(
            rating=5,
            created_at=NOW - timedelta(hours=1),
            replied=False,
            text="Todo perfecto.",
            review_id="pytest-chan-high",
        ),
    )
)


def run(coro):
    return asyncio.run(coro)


async def _as(url: str, sql: str, *args):
    conn = await asyncpg.connect(url)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def owner(sql: str, *args):
    return await _as(OWNER_URL, sql, *args)


async def denied(url: str, sql: str, *args, rollback: bool = False) -> bool:
    """Whether the role of `url` is refused that statement. With `rollback`, it runs in a
    transaction that is always rolled back, so that nothing stays even if it were let in."""
    conn = await asyncpg.connect(url)
    try:
        if rollback:
            tx = conn.transaction()
            await tx.start()
            try:
                await conn.execute(sql, *args)
            except DENIED:
                return True
            except asyncpg.PostgresError as exc:
                # Row level security refuses with its own error.
                return "row-level security" in str(exc)
            finally:
                await tx.rollback()
            return False
        try:
            await conn.execute(sql, *args)
        except DENIED:
            return True
        return False
    finally:
        await conn.close()


async def cleanup():
    mine = (
        "(SELECT id FROM account WHERE google_sub LIKE 'pytest-chan-%'"
        " OR (google_sub LIKE 'deleted:%' AND email = ''))"
    )
    for sql in (
        f"DELETE FROM channel_message WHERE conversation_id IN"
        f" (SELECT id FROM conversation WHERE account_id IN {mine})",
        f"UPDATE conversation SET active_proposal_id = NULL WHERE account_id IN {mine}",
        f"DELETE FROM conversation WHERE account_id IN {mine}",
        f"DELETE FROM proposal WHERE account_id IN {mine}",
        f"DELETE FROM channel_message WHERE identity_id IN"
        f" (SELECT id FROM channel_identity WHERE account_id IN {mine})",
        f"DELETE FROM channel_identity WHERE account_id IN {mine}",
        f"DELETE FROM link_code WHERE account_id IN {mine}",
        "DELETE FROM channel_inbound_event WHERE event_id LIKE 'pytest-%'",
        "DELETE FROM channel_sender WHERE address_hash = ANY($1::text[])",
        "DELETE FROM link_failure_window",
        f"DELETE FROM audit_log WHERE account_id IN {mine}",
        f"DELETE FROM action WHERE account_id IN {mine}",
        "DELETE FROM review WHERE google_sub LIKE 'pytest-chan-%'",
        "DELETE FROM profile_summary WHERE google_sub LIKE 'pytest-chan-%'",
        "DELETE FROM job WHERE kind LIKE 'pytest-chan-%'",
        "DELETE FROM job WHERE payload->>'google_sub' LIKE 'pytest-chan-%'",
        # The draft cap counts per account, or per profile since multinegocio.
        "DELETE FROM usage_counter WHERE scope LIKE 'review_draft:%'"
        f" AND (split_part(scope, ':', 2) IN (SELECT id::text FROM account WHERE id IN {mine})"
        f" OR split_part(scope, ':', 2) IN (SELECT id FROM location WHERE account_id IN {mine}))",
        "DELETE FROM google_connection WHERE google_sub LIKE 'pytest-chan-%'",
        f"UPDATE account SET selected_location_id = NULL WHERE id IN {mine}",
        f"DELETE FROM offboarding WHERE account_id IN {mine}",
        f"DELETE FROM session WHERE account_id IN {mine}",
        f"DELETE FROM location WHERE account_id IN {mine}",
        f"DELETE FROM account WHERE id IN {mine}",
    ):
        args = ([lc.address_hash("telegram", chat) for chat in CHATS],) if "$1" in sql else ()
        await owner(sql, *args)


@pytest.fixture(autouse=True)
def clean():
    run(cleanup())
    yield
    run(cleanup())


class Scene:
    """The API, the worker and the conversation on the test database, each with its
    own role, one clock, a fake Telegram, a fake Google and the fake model."""

    def __init__(self) -> None:
        kind = f"pytest-chan-{uuid.uuid4().hex[:10]}"
        self.reply_kind = f"{kind}-reply"
        self.now = NOW
        self.api = PostgresStore(API_URL, reply_job_kind=self.reply_kind)
        # The conversation creates actions through the very store of the panel, with
        # its own role.
        self.conv_api = PostgresStore(CONVERSATION_URL, reply_job_kind=self.reply_kind)
        self.telegram = FakeChannel("telegram")
        self.google = FakeGoogleProfile(reviews=REVIEWS)
        self._events = iter(range(1, 10_000))

    def clock(self) -> datetime:
        return self.now

    async def __aenter__(self) -> "Scene":
        await self.api.connect()
        await self.conv_api.connect()
        self.wpool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=4)
        self.cpool = await asyncpg.create_pool(CONVERSATION_URL, min_size=1, max_size=6)
        self.wstore = PostgresWorkerStore(self.wpool)
        self.store = PostgresConversationStore(self.cpool)
        self.replies = ReviewReplies(
            self.conv_api,
            FakeScreenshotReader(),
            draft_limit=30,
            draft_window_seconds=86400,
            draft_timeout_seconds=5,
            clock=lambda: self.now.timestamp(),
            quota=self.conv_api,
        )
        self.service = ConversationService(
            self.store,
            {"telegram": self.telegram},
            ReviewReplyActions(self.replies),
            ServiceConfig(
                panel_url="https://panel.test/app",
                signup_url="https://alta.test",
                link_code_pepper=PEPPER,
                quiet=QuietHours(),
            ),
            clock=self.clock,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        await self.cpool.close()
        await self.wpool.close()
        await self.conv_api.close()
        await self.api.close()

    # --- the stage --------------------------------------------------------------------

    async def account(self, sub: str = SUB) -> str:
        """Connected, with its profile selected and its reviews synced by the worker."""
        account_id = await self.api.upsert_account(sub, f"{sub}@example.com")
        await self.api.sync_locations(account_id, [BAR], self.now)
        rows = await owner(
            "SELECT id FROM location WHERE account_id = $1::uuid AND gbp_location = $2",
            account_id,
            BAR.location,
        )
        assert await self.api.select_location(account_id, rows[0]["id"])
        # Active, as the backfill of multinegocio leaves the selected profile: proposals
        # are only made for active profiles (docs/multinegocio.md, 3.6). Pro, because the
        # same Google profile is free in one account only.
        await owner(
            "UPDATE location SET active_since = now(), plan = 'pro' WHERE id = $1", rows[0]["id"]
        )
        await self.api.add_google_connection(
            GoogleConnection(
                sub,
                f"{sub}@example.com",
                seal(PUBLIC, f"refresh-{sub}"),
                1,
                ("scope",),
                ({"account": BAR.account, "location": BAR.location},),
            )
        )
        return account_id

    async def link(self, account_id: str, chat: str = OWNER_CHAT) -> None:
        issued = lc.issue_link(account_id, self.now, PEPPER)
        await self.api.save_link_code(issued.record)
        await self.event(InboundKind.START, chat, issued.token)

    async def event(self, kind, chat, text="", **fields) -> InboundEvent:
        event = InboundEvent(
            "telegram", chat, f"pytest-{next(self._events)}", kind, text, sender_name="Marta",
            **fields,
        )  # fmt: skip
        await self.service.handle_inbound(event)
        return event

    async def press(self, title: str, chat: str = OWNER_CHAT) -> InboundEvent:
        sent = [m for m in self.telegram.to(chat) if m.message.buttons][-1]
        return await self.event(
            InboundKind.BUTTON, chat, button_id=sent.button(title).id, callback_id="cb"
        )

    async def sync(self, sub: str = SUB) -> list:
        return await self.wstore.save_reviews(sub, BAR.location, REVIEWS.reviews, self.now)

    async def review_id(self, gbp_id: str, sub: str = SUB) -> str:
        rows = await owner(
            "SELECT id FROM review WHERE google_sub = $1 AND gbp_review_id = $2", sub, gbp_id
        )
        return rows[0]["id"]

    async def propose(self, gbp_id: str) -> None:
        job = ProposeReview(self.store, self.replies, self.service, self.clock)
        await job(
            Job("p", channel_jobs.PROPOSE_REVIEW, {"review_id": await self.review_id(gbp_id)})
        )


# --- end to end ------------------------------------------------------------------------


def test_link_propose_publish_execute_and_tell_end_to_end():
    async def go():
        async with Scene() as s:
            account = await s.account()
            new = await s.sync()
            assert {r.created_at for r in new} == {r.created_at for r in REVIEWS.reviews}
            assert await s.sync() == []  # stored already: nothing new
            await s.link(account)
            assert s.telegram.to(OWNER_CHAT)[-1].key == "welcome.linked"
            assert "Bar Canal" in s.telegram.to(OWNER_CHAT)[-1].text
            # Linked after the reviews were written: they are history, not proposed.
            await s.propose("pytest-chan-high")
            assert len(s.telegram.to(OWNER_CHAT)) == 1
            await owner(
                "UPDATE channel_identity SET verified_at = $2 WHERE account_id = $1::uuid",
                account,
                NOW - timedelta(days=1),
            )
            await s.propose("pytest-chan-high")
            proposal_message = s.telegram.to(OWNER_CHAT)[-1]
            assert proposal_message.key == "review.new.positive"
            await s.propose("pytest-chan-high")  # once per account and review
            assert len(s.telegram.to(OWNER_CHAT)) == 2
            press = await s.press("Publicar")
            action = (
                await owner(
                    "SELECT id, account_id::text AS account_id, google_sub, location, text,"
                    " state FROM action WHERE account_id = $1::uuid",
                    account,
                )
            )[0]
            draft = (
                await owner(
                    "SELECT draft FROM review_draft WHERE review_id = $1",
                    await s.review_id("pytest-chan-high"),
                )
            )[0]["draft"]
            assert action["text"] == draft and draft in proposal_message.text
            assert (action["google_sub"], action["location"], action["state"]) == (
                SUB,
                BAR.location,
                "pending",
            )
            audit = await owner(
                "SELECT event, written_by FROM audit_log WHERE action_id = $1", action["id"]
            )
            assert [(a["event"], a["written_by"]) for a in audit] == [
                ("reply_confirmed", "fichaitor_conversation_dev")
            ]
            (job,) = await owner(
                "SELECT id::text AS id, payload::text AS payload FROM job WHERE kind = $1",
                s.reply_kind,
            )
            proposal = await s.store.get_proposal_by_action(action["id"])
            assert proposal.state is S.EXECUTING
            assert proposal.confirmed_event_id == press.event_id
            assert proposal.confirmed_summary == draft
            # Only the executor writes to Google, after the coherence check (M1).
            queue = PostgresJobQueue(s.wpool)
            executor = ReplyReview(s.wstore, s.google, PRIVATE, s.clock, results=queue)
            await executor(Job(job["id"], s.reply_kind, {"action_id": action["id"]}, attempts=1))
            assert [r[-1] for r in s.google.replies] == [draft]
            (told,) = await owner(
                "SELECT payload::text AS payload FROM job WHERE kind = $1 AND idempotency_key = $2",
                channel_jobs.CHANNEL_ACTION_RESULT,
                channel_jobs.result_key(action["id"]),
            )
            payload = json.loads(told["payload"])
            assert payload == {"action_id": action["id"], "ok": True, "error": None}
            await owner(
                "DELETE FROM job WHERE idempotency_key = $1", channel_jobs.result_key(action["id"])
            )
            await ChannelActionResult(s.service)(
                Job("r", channel_jobs.CHANNEL_ACTION_RESULT, payload)
            )
            assert s.telegram.to(OWNER_CHAT)[-1].key == "review.sent"
            assert (await s.store.get_proposal(proposal.id)).state is S.APPLIED

    run(go())


def test_presses_at_once_create_one_action():
    async def go():
        async with Scene() as s:
            account = await s.account()
            await s.sync()
            await owner(
                "UPDATE review SET created_at = $2 WHERE google_sub = $1", SUB, NOW + timedelta(1)
            )
            await s.link(account)
            await s.link(account, MANAGER_CHAT)
            await s.propose("pytest-chan-low")
            sent = [m for m in s.telegram.to(OWNER_CHAT) if m.message.buttons][-1]
            button = sent.button("Publicar").id

            def press(n: int, chat: str) -> InboundEvent:
                return InboundEvent(
                    "telegram", chat, f"pytest-race-{n}", InboundKind.BUTTON,
                    button_id=button, callback_id=f"cb{n}",
                )  # fmt: skip

            await asyncio.gather(
                *(s.service.handle_inbound(press(n, CHATS[n % 2])) for n in range(6))
            )
            rows = await owner("SELECT id FROM action WHERE account_id = $1::uuid", account)
            assert len(rows) == 1

    run(go())


# --- B3: atomic counts ----------------------------------------------------------------


def test_attempts_of_one_chat_at_once_never_slip_under_the_limit():
    async def go():
        async with Scene() as s:
            key = lc.address_hash("telegram", STRANGER)
            taken = await asyncio.gather(*(s.store.take_link_attempt(key, NOW) for _ in range(12)))
            allowed = [t for t in taken if t is not None]
            assert len(allowed) == lc.MAX_FAILED_ATTEMPTS
            assert sorted(t.failed_attempts for t in allowed) == [1, 2, 3]
            assert sum(lc.is_blocked(t, NOW) for t in allowed) == 1
            assert await s.store.take_link_attempt(key, NOW + timedelta(minutes=59)) is None
            again = await s.store.take_link_attempt(key, NOW + timedelta(hours=1))
            assert again.failed_attempts == 1 and again.blocked_until is None
            await s.store.clear_link_attempts(key)
            assert (await s.store.get_sender_state(key)).failed_attempts == 0

    run(go())


def test_the_global_count_of_wrong_codes_is_atomic_and_per_window():
    async def go():
        async with Scene() as s:
            counts = await asyncio.gather(*(s.store.count_link_failure(NOW) for _ in range(20)))
            assert sorted(counts) == list(range(1, 21))
            assert await s.store.link_failures(NOW) == 20
            assert await s.store.link_failures(NOW + lc.GLOBAL_FAILURE_WINDOW) == 0

    run(go())


def test_the_generic_answer_to_an_unknown_chat_is_given_once_a_day_even_at_once():
    async def go():
        async with Scene() as s:
            key = lc.address_hash("telegram", STRANGER)
            claims = await asyncio.gather(
                *(s.store.claim_unlinked_reply(key, NOW) for _ in range(6))
            )
            assert sum(claims) == 1
            assert not await s.store.claim_unlinked_reply(key, NOW + timedelta(hours=23))
            assert await s.store.claim_unlinked_reply(key, NOW + timedelta(hours=24))

    run(go())


def test_wrong_codes_by_the_channel_block_and_a_used_token_does_not_link_again():
    async def go():
        async with Scene() as s:
            account = await s.account()
            issued = lc.issue_link(account, NOW, PEPPER)
            await s.api.save_link_code(issued.record)
            for _ in range(3):
                await s.event(InboundKind.TEXT, STRANGER, "000000")
            assert [m.key for m in s.telegram.to(STRANGER)] == [
                "link.code_invalid",
                "link.code_invalid",
                "link.blocked",
            ]
            await s.event(InboundKind.START, STRANGER, issued.token)  # blocked: ignored
            assert await s.store.get_identity("telegram", STRANGER) is None
            await s.event(InboundKind.START, OWNER_CHAT, issued.token)
            assert (await s.store.get_identity("telegram", OWNER_CHAT)).account_id == account
            await s.event(InboundKind.TEXT, MANAGER_CHAT, issued.code)  # same code, spent
            assert s.telegram.to(MANAGER_CHAT)[-1].key == "link.code_invalid"
            assert await s.store.link_failures(NOW) == 4
            # Nothing of the codes is stored in clear.
            stored = await owner("SELECT code_hash, token_hash FROM link_code")
            flat = " ".join(f"{r['code_hash']} {r['token_hash']}" for r in stored)
            assert issued.code not in flat and issued.token not in flat
            bodies = await owner("SELECT body FROM channel_message")
            assert all(issued.token not in b["body"] for b in bodies)

    run(go())


def test_a_new_code_retires_the_previous_and_a_taken_hash_is_refused():
    async def go():
        async with Scene() as s:
            account = await s.account()
            first = lc.issue_link(account, NOW, PEPPER)
            await s.api.save_link_code(first.record)
            second = lc.issue_link(account, NOW, PEPPER)
            await s.api.save_link_code(second.record)
            assert await s.store.consume_link_code(first.record.token_hash, NOW) is None
            assert await s.store.consume_link_code(second.record.code_hash, NOW) == account
            with pytest.raises(LinkCodeTaken):
                await s.api.save_link_code(second.record)

    run(go())


# --- what the planner and the jobs read --------------------------------------------------


def test_review_targets_and_linked_profiles():
    async def go():
        async with Scene() as s:
            account = await s.account()
            await s.account(OTHER_SUB)  # same profile, another Google user, not linked
            await s.sync()
            high = await s.review_id("pytest-chan-high")
            target = await s.store.review_targets(high)
            assert target.stars == 5 and target.text == "Todo perfecto."
            assert target.unanswered and target.live and target.accounts == ()
            assert await s.store.linked_profiles() == []
            await s.link(account)
            # Linked after the review was written: history.
            assert (await s.store.review_targets(high)).accounts == ()
            await owner(
                "UPDATE channel_identity SET verified_at = $2 WHERE account_id = $1::uuid",
                account,
                NOW - timedelta(days=1),
            )
            assert (await s.store.review_targets(high)).accounts == (account,)
            assert await s.store.linked_profiles() == [(SUB, BAR.location)]
            assert await s.store.review_targets("rev_00000000ffffffff") is None
            kind = "pytest-chan-sync"
            planner = ChannelPlanner(
                PostgresJobQueue(s.cpool), s.store, s.clock, sync_kind=kind, tick_kind=kind + "-t"
            )
            assert await planner.plan_once() == {"conversation_tick": 1, "sync_profile": 1}
            assert await planner.plan_once() == {"conversation_tick": 0, "sync_profile": 0}

    run(go())


# --- privileges ---------------------------------------------------------------------------


def test_the_conversation_role_cannot_reach_tokens_emails_or_delete_anything():
    async def go():
        assert await denied(CONVERSATION_URL, "SELECT id FROM google_connection WHERE false")
        assert await denied(CONVERSATION_URL, "SELECT email FROM account WHERE false")
        assert await denied(CONVERSATION_URL, "SELECT id FROM session WHERE false")
        for table in ("channel_message", "channel_identity", "proposal", "action", "job"):
            assert await denied(CONVERSATION_URL, f"DELETE FROM {table} WHERE false"), table
        assert await denied(CONVERSATION_URL, "SELECT body FROM channel_message WHERE false")
        assert await denied(
            CONVERSATION_URL, "UPDATE proposal SET payload = '{}'::jsonb WHERE false"
        )
        assert await denied(CONVERSATION_URL, "UPDATE proposal SET account_id = NULL WHERE false")
        assert await denied(CONVERSATION_URL, "UPDATE action SET text = 'x' WHERE false")
        assert await denied(CONVERSATION_URL, "UPDATE action SET state = 'done' WHERE false")
        assert await denied(
            CONVERSATION_URL,
            "INSERT INTO link_code (code_hash, account_id, expires_at)"
            " VALUES (repeat('a', 64), gen_random_uuid(), now())",
            rollback=True,
        )
        # The audit log: only the event of a confirmation.
        assert await denied(
            CONVERSATION_URL,
            "INSERT INTO audit_log (event, account_id, action_id, action_type, location,"
            " review_id, gbp_review_id, text, confirmed_at) VALUES ('reply_sent',"
            " gen_random_uuid(), 'act_x', 'reply_review', 'l', 'r', 'g', 't', now())",
            rollback=True,
        )
        assert await denied(CONVERSATION_URL, "SELECT id FROM audit_log WHERE false")

    run(go())


def test_the_api_and_the_worker_cannot_link_an_address_nor_talk_for_the_conversation():
    async def go():
        insert = (
            "INSERT INTO channel_identity (account_id, channel, address)"
            " VALUES (gen_random_uuid(), 'telegram', '1')"
        )
        assert await denied(API_URL, insert, rollback=True)
        assert await denied(WORKER_URL, insert, rollback=True)
        assert await denied(API_URL, "SELECT code_hash FROM link_code WHERE false")
        assert await denied(API_URL, "SELECT token_hash FROM link_code WHERE false")
        assert await denied(API_URL, "SELECT body FROM channel_message WHERE false")
        assert await denied(API_URL, "DELETE FROM channel_identity WHERE false")
        assert await denied(API_URL, "UPDATE channel_identity SET account_id = NULL WHERE false")
        assert await denied(WORKER_URL, "SELECT address FROM channel_identity WHERE false")
        assert await denied(WORKER_URL, "SELECT body FROM channel_message WHERE false")
        assert await denied(WORKER_URL, "SELECT payload FROM proposal WHERE false")
        assert await denied(WORKER_URL, "UPDATE proposal SET state = 'aplicada' WHERE false")
        assert await denied(WORKER_URL, "SELECT title FROM location WHERE false")
        assert await denied(
            WORKER_URL,
            "INSERT INTO action (account_id, type, google_sub, location, review_id, text)"
            " VALUES (gen_random_uuid(), 'reply_review', 's', 'l', 'r', 't')",
            rollback=True,
        )

    run(go())


# --- leaving and retention -------------------------------------------------------------------


async def _furnished(s: Scene, sub: str = SUB) -> str:
    account = await s.account(sub)
    await s.sync(sub)
    await owner("UPDATE review SET created_at = $2 WHERE google_sub = $1", sub, NOW + timedelta(1))
    await s.link(account)
    await s.propose("pytest-chan-low")
    await s.event(InboundKind.TEXT, OWNER_CHAT, "gracias")
    issued = lc.issue_link(account, NOW, PEPPER)
    await s.api.save_link_code(issued.record)
    return account


async def _counts(account: str) -> dict[str, int]:
    tables = {
        "channel_identity": "account_id = $1::uuid",
        "link_code": "account_id = $1::uuid",
        "proposal": "account_id = $1::uuid",
        "conversation": "account_id = $1::uuid",
        "channel_message": "conversation_id IN (SELECT id FROM conversation"
        " WHERE account_id = $1::uuid) OR identity_id IN (SELECT id FROM channel_identity"
        " WHERE account_id = $1::uuid)",
    }
    return {
        table: (await owner(f"SELECT count(*) AS n FROM {table} WHERE {where}", account))[0]["n"]  # noqa: S608
        for table, where in tables.items()
    }


async def _leave(s: Scene, account: str, scope: str) -> None:
    rows = await owner(
        "INSERT INTO offboarding (account_id, scope, google_sub, confirmed_at, delete_at)"
        " SELECT id, $2, google_sub, $3, $4 FROM account WHERE id = $1::uuid RETURNING id::text",
        account,
        scope,
        NOW - timedelta(hours=48),
        NOW,
    )
    assert await s.wstore.begin_offboarding(rows[0]["id"], NOW + timedelta(hours=1)) is not None


def test_the_effective_leave_of_the_account_deletes_its_channels_and_messages():
    async def go():
        async with Scene() as s:
            account = await _furnished(s)
            before = await _counts(account)
            assert all(before.values()), before
            await _leave(s, account, "cuenta")
            assert await _counts(account) == dict.fromkeys(before, 0)
            # The account stays as a tombstone, without its email.
            (row,) = await owner("SELECT email, deleted_at FROM account WHERE id = $1", account)
            assert row["email"] == "" and row["deleted_at"] is not None

    run(go())


def test_the_effective_leave_of_the_profile_keeps_the_linked_chat_but_not_the_conversation():
    async def go():
        async with Scene() as s:
            account = await _furnished(s)
            await _leave(s, account, "ficha")
            after = await _counts(account)
            assert after["channel_identity"] == 1 and after["link_code"] == 2
            assert after["proposal"] == after["conversation"] == after["channel_message"] == 0

    run(go())


def test_retention_deletes_channel_messages_after_90_days_and_keeps_the_rest():
    async def go():
        async with Scene() as s:
            account = await _furnished(s)
            conversation = (
                await owner("SELECT id FROM conversation WHERE account_id = $1", account)
            )[0]["id"]
            await owner(
                "UPDATE channel_message SET at = $2 WHERE conversation_id = $1"
                " AND id = (SELECT min(id) FROM channel_message WHERE conversation_id = $1)",
                conversation,
                NOW - timedelta(days=91),
            )
            await owner(
                "INSERT INTO channel_inbound_event (channel, event_id, received_at)"
                " VALUES ('telegram', 'pytest-old', $1)",
                NOW - timedelta(days=91),
            )
            unlinked = await owner(
                "INSERT INTO channel_identity (account_id, channel, address, verified_at,"
                " unlinked_at) VALUES ($1, 'telegram', '880004', $2, $2) RETURNING id",
                account,
                NOW - timedelta(days=100),
            )
            messages = (await _counts(account))["channel_message"]
            counts = await s.wstore.run_retention(NOW, RetentionPolicy())
            assert counts["channel_message"] >= 1 and counts["channel_inbound_event"] >= 1
            assert (await _counts(account))["channel_message"] == messages - 1
            gone = await owner("SELECT 1 FROM channel_identity WHERE id = $1", unlinked[0]["id"])
            assert gone == []
            # The linked chat, the live proposal and the newer messages stay.
            assert (await s.store.get_identity("telegram", OWNER_CHAT)) is not None
            assert await s.store.list_unfinished_proposals(account)
            (run_row,) = await owner("SELECT deleted FROM retention_run ORDER BY id DESC LIMIT 1")
            assert "channel_message" in run_row["deleted"]

    run(go())
