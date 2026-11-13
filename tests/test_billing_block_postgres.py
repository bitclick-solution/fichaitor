"""Step 7 of docs/admin-facturacion.md on PostgreSQL: an account blocked for non-payment,
with the roles of the worker and of the conversation, against the TEST database only.

Skipped unless the URLs are set. They must point to a database whose name ends in
`_test` (tests/conftest.py stops the run otherwise). Create it with
scripts/preparar_bd_pruebas.sh.

The block is a row of `account_billing_block` that only the billing process (or the
owner, here) writes. The worker and the conversation read it through
`fichaitor_account_blocked`, the same condition the API uses. Google, Telegram and the
model are fakes: nothing leaves the machine.
"""

import asyncio
import json
from datetime import timedelta

import pytest

from app.adapters.job_queue.postgres import PostgresJobQueue
from app.domain import channel_jobs
from app.domain.channel_messages import InboundKind
from app.domain.jobs import Job
from app.domain.proposals import ProposalState as S
from app.worker.channel_action_result import ChannelActionResult
from app.worker.reply_review import ReplyReview
from app.worker.sync_profile import SyncProfile
from tests.test_conversation_store_postgres import (
    API_URL,
    BAR,
    CONVERSATION_URL,
    NOW,
    OWNER_CHAT,
    OWNER_URL,
    PRIVATE,
    SUB,
    WORKER_URL,
    Scene,
    cleanup,
    denied,
    owner,
)

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL and WORKER_URL and CONVERSATION_URL),
    reason="sin base de datos de pruebas configurada",
)

PAUSED_KEY = "billing.paused"


def run(coro):
    return asyncio.run(coro)


async def clean_blocks():
    # Before the accounts go: the block hangs from them.
    await owner(
        "DELETE FROM account_billing_block WHERE account_id IN"
        " (SELECT id FROM account WHERE google_sub LIKE 'pytest-chan-%')"
    )


@pytest.fixture(autouse=True)
def clean():
    run(clean_blocks())
    run(cleanup())
    yield
    run(clean_blocks())
    run(cleanup())


async def block(account: str) -> None:
    await owner(
        "INSERT INTO account_billing_block (account_id, reason, stripe_invoice_id)"
        " VALUES ($1::uuid, 'payment_failed', 'in_pytestchan1')",
        account,
    )


async def unblock(account: str) -> None:
    await owner(
        "UPDATE account_billing_block SET unblocked_at = now(), unblocked_by = 'payment'"
        " WHERE account_id = $1::uuid AND unblocked_at IS NULL",
        account,
    )


async def linked_before_the_reviews(s: Scene, account: str) -> None:
    await s.link(account)
    await owner(
        "UPDATE channel_identity SET verified_at = $2 WHERE account_id = $1::uuid",
        account,
        NOW - timedelta(days=1),
    )


async def proposed(s: Scene) -> str:
    """A linked account with the 5-star review proposed by the channel."""
    account = await s.account()
    await s.sync()
    await linked_before_the_reviews(s, account)
    await s.propose("pytest-chan-high")
    assert s.telegram.to(OWNER_CHAT)[-1].key == "review.new.positive"
    return account


def sync_job(s: Scene) -> SyncProfile:
    return SyncProfile(s.wstore, s.google, PRIVATE, s.clock)


def test_a_confirmed_action_of_a_blocked_account_ends_billing_blocked_without_google():
    async def go():
        async with Scene() as s:
            account = await proposed(s)
            await s.press("Publicar")
            (action,) = await owner("SELECT id FROM action WHERE account_id = $1::uuid", account)
            (job,) = await owner("SELECT id::text AS id FROM job WHERE kind = $1", s.reply_kind)
            # The payment fails between the confirmation and the job.
            await block(account)
            queue = PostgresJobQueue(s.wpool)
            executor = ReplyReview(s.wstore, s.google, PRIVATE, s.clock, results=queue)
            await executor(Job(job["id"], s.reply_kind, {"action_id": action["id"]}, attempts=1))
            assert s.google.replies == [] and s.google.write_attempts == 0
            assert s.google.refreshed_with == []  # not even a token
            (row,) = await owner("SELECT state, error_code FROM action WHERE id = $1", action["id"])
            audit = await owner(
                "SELECT event, reason, written_by FROM audit_log WHERE action_id = $1 ORDER BY id",
                action["id"],
            )
            (told,) = await owner(
                "SELECT payload::text AS payload FROM job WHERE idempotency_key = $1",
                channel_jobs.result_key(action["id"]),
            )
            payload = json.loads(told["payload"])
            await owner(
                "DELETE FROM job WHERE idempotency_key = $1", channel_jobs.result_key(action["id"])
            )
            await ChannelActionResult(s.service)(
                Job("r", channel_jobs.CHANNEL_ACTION_RESULT, payload)
            )
            proposal = await s.store.get_proposal_by_action(action["id"])
            return row, audit, payload, s.telegram.to(OWNER_CHAT)[-1], proposal

    row, audit, payload, last, proposal = run(go())
    assert (row["state"], row["error_code"]) == ("failed", "billing_blocked")
    assert [(a["event"], a["reason"]) for a in audit] == [
        ("reply_confirmed", None),
        ("reply_failed", "billing_blocked"),
    ]
    assert audit[-1]["written_by"] == "fichaitor_worker_dev"
    assert payload == {"action_id": payload["action_id"], "ok": False, "error": "billing_blocked"}
    assert last.key == PAUSED_KEY and "https://panel.test/app" in last.text
    assert proposal.state is S.FAILED


def test_a_blocked_account_is_not_synced_nor_told_and_comes_back_once_it_pays():
    async def go():
        async with Scene() as s:
            account = await s.account()
            await s.sync()
            await linked_before_the_reviews(s, account)
            high = await s.review_id("pytest-chan-high")
            await block(account)
            seen = {
                "worker": await s.wstore.billing_blocked(SUB),
                "linked": await s.store.linked_profiles(),
                "targets": (await s.store.review_targets(high)).targets,
                "key_dates": [p.account_id for p in await s.store.key_date_profiles()],
                "conversation": await s.store.billing_blocked(account),
            }
            await sync_job(s)(
                Job("s1", "sync_profile", {"google_sub": SUB, "location": BAR.location})
            )
            seen["google_while_blocked"] = (list(s.google.refreshed_with), list(s.google.reads))
            await s.propose("pytest-chan-high")
            seen["sent_while_blocked"] = [m.key for m in s.telegram.to(OWNER_CHAT)]
            # It pays: everything is back, and the next sync of the planner reads Google.
            await unblock(account)
            back = {
                "worker": await s.wstore.billing_blocked(SUB),
                "linked": await s.store.linked_profiles(),
                "targets": [a for a, _ in (await s.store.review_targets(high)).targets],
            }
            await sync_job(s)(
                Job("s2", "sync_profile", {"google_sub": SUB, "location": BAR.location})
            )
            back["google"] = len(s.google.refreshed_with)
            back["key_dates"] = [p.account_id for p in await s.store.key_date_profiles()]
            await s.propose("pytest-chan-high")
            back["sent"] = s.telegram.to(OWNER_CHAT)[-1].key
            return account, seen, back

    account, seen, back = run(go())
    assert seen["worker"] is True and seen["conversation"] is True
    assert seen["linked"] == [] and seen["targets"] == () and account not in seen["key_dates"]
    assert seen["google_while_blocked"] == ([], [])
    assert seen["sent_while_blocked"] == ["welcome.linked"]
    assert back["worker"] is False
    assert back["linked"] == [(SUB, BAR.location)] and back["targets"] == [account]
    assert back["google"] == 1 and account in back["key_dates"]
    assert back["sent"] == "review.new.positive"


def test_the_pause_is_told_once_a_day_even_to_messages_at_once():
    async def go():
        async with Scene() as s:
            account = await s.account()
            await s.link(account)
            await block(account)
            claims = await asyncio.gather(
                *(s.store.claim_paused_notice(account, s.now) for _ in range(5))
            )
            await owner(
                "UPDATE conversation SET billing_notice_at = NULL WHERE account_id = $1::uuid",
                account,
            )
            await s.event(InboundKind.TEXT, OWNER_CHAT, "¿hay algo nuevo?")
            await s.event(InboundKind.TEXT, OWNER_CHAT, "¿hola?")
            first = [m.key for m in s.telegram.to(OWNER_CHAT)]
            s.now += timedelta(hours=24)
            await s.event(InboundKind.TEXT, OWNER_CHAT, "hola")
            second = [m.key for m in s.telegram.to(OWNER_CHAT)]
            await s.event(InboundKind.COMMAND, OWNER_CHAT, "ayuda")
            third = [m.key for m in s.telegram.to(OWNER_CHAT)]
            return claims, first, second, third

    claims, first, second, third = run(go())
    assert sorted(claims) == [False, False, False, False, True]
    assert first == ["welcome.linked", PAUSED_KEY]
    assert second == [*first, PAUSED_KEY]
    assert third == [*second, "command.help"]


def test_open_proposals_wait_while_blocked_and_resume_once_it_pays():
    async def go():
        async with Scene() as s:
            account = await proposed(s)
            await block(account)
            await s.press("Publicar")
            actions_blocked = await owner(
                "SELECT id FROM action WHERE account_id = $1::uuid", account
            )
            lanes_blocked = await s.store.lanes_with_open_work()
            s.now += timedelta(days=3)
            await s.service.tick()
            keys_blocked = [m.key for m in s.telegram.to(OWNER_CHAT)]
            notices = list(s.telegram.notices)
            await unblock(account)
            lanes_back = await s.store.lanes_with_open_work()
            await s.press("Publicar")
            actions_back = await owner(
                "SELECT state FROM action WHERE account_id = $1::uuid", account
            )
            return actions_blocked, lanes_blocked, keys_blocked, notices, lanes_back, actions_back

    actions_blocked, lanes_blocked, keys_blocked, notices, lanes_back, actions_back = run(go())
    assert actions_blocked == [] and lanes_blocked == []
    # The press got the pause (once a day, as a message), and no reminder nor expiry ran.
    assert keys_blocked[-1] == PAUSED_KEY and keys_blocked.count(PAUSED_KEY) == 1
    assert "reminder.pending" not in keys_blocked
    assert notices[-1][1] == ""
    assert len(lanes_back) == 1
    assert [a["state"] for a in actions_back] == ["pending"]


def test_only_the_conversation_writes_the_date_of_the_pause_notice():
    async def go():
        assert not await denied(
            CONVERSATION_URL,
            "UPDATE conversation SET billing_notice_at = now() WHERE false",
        )
        assert await denied(
            API_URL, "UPDATE conversation SET billing_notice_at = now() WHERE false"
        )
        # Both read the block through the shared condition, never its other columns.
        for url in (WORKER_URL, CONVERSATION_URL):
            assert not await denied(
                url, "SELECT fichaitor_account_blocked(gen_random_uuid()) WHERE false"
            )
            assert await denied(url, "SELECT reason FROM account_billing_block WHERE false")
            assert await denied(
                url,
                "INSERT INTO account_billing_block (account_id, reason)"
                " SELECT id, 'payment_failed' FROM account WHERE false",
            )
        column = await owner(
            "SELECT data_type FROM information_schema.columns"
            " WHERE table_name = 'conversation' AND column_name = 'billing_notice_at'"
        )
        assert [c["data_type"] for c in column] == ["timestamp with time zone"]

    run(go())
