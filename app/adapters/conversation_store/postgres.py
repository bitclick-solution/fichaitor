"""Conversation store on PostgreSQL: the tables of `db/schema_channels.sql`, with the
role of the conversation (`fichaitor_conversation`). Explicit SQL, no ORM.

Same rules as `MemoryConversationStore`, plus what only a database gives:

- `lock(account_id)` is a transaction that holds the row of the thread of that account
  (`SELECT ... FOR UPDATE`). Everything the service does inside it goes on that same
  connection and in that same transaction: two presses of the same button, handled by
  two processes at once, are handled one after the other.
- Every count that guards linking is one statement (B3 of
  docs/seguridad/revision-2026-11-12.md): the attempt of a chat is counted before its
  code is looked at, and the wrong codes of every chat together are counted per window.
- No row is deleted here: the role has no DELETE. Leaving and retention are the worker's.
"""

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Any

import asyncpg

from app.domain.conversation import (
    DEFAULT_TIMEZONE,
    AccountInfo,
    ChannelIdentity,
    Conversation,
    LinkOutcome,
    normalize_address,
    plan_link,
)
from app.domain.link_codes import (
    BLOCK_DURATION,
    MAX_FAILED_ATTEMPTS,
    UNLINKED_REPLY_EVERY,
    LinkCode,
    SenderState,
    failure_window,
)
from app.domain.profile_summary import facts_from_json
from app.domain.proposals import Proposal, ProposalState, ProposalType
from app.ports.conversation_store import (
    KeyDateProfile,
    LinkResult,
    MessageRecord,
    ReviewTarget,
)

# The connection of the lock this task holds, if any, and the store it belongs to.
_HELD: ContextVar[tuple[object, asyncpg.Connection] | None] = ContextVar(
    "fichaitor_conversation_lock", default=None
)

UNFINISHED = (
    ProposalState.SCHEDULED,
    ProposalState.SENT,
    ProposalState.REMINDED,
    ProposalState.COLLECTING,
    ProposalState.TO_CONFIRM,
    ProposalState.EXECUTING,
)
_UNFINISHED = [state.value for state in UNFINISHED]

_IDENTITY = "id, account_id::text AS account_id, channel, address, verified_at, last_inbound_at"
_PROPOSAL = """
    id, account_id::text AS account_id, type, source_ref, state, priority,
    payload::text AS payload, slots::text AS slots, step, step_token,
    message_refs::text AS message_refs, created_at, scheduled_for, sent_at, remind_at,
    reminder_sent_at, expires_at, parked_at, confirmed_at, confirmed_event_id,
    confirmed_summary, action_id, outcome_reason
"""


def _identity(row: asyncpg.Record) -> ChannelIdentity:
    return ChannelIdentity(
        id=row["id"],
        account_id=row["account_id"],
        channel=row["channel"],
        address=row["address"],
        verified_at=row["verified_at"],
        last_inbound_at=row["last_inbound_at"],
    )


def _proposal(row: asyncpg.Record) -> Proposal:
    return Proposal(
        id=row["id"],
        account_id=row["account_id"],
        type=ProposalType(row["type"]),
        source_ref=row["source_ref"],
        state=ProposalState(row["state"]),
        priority=row["priority"],
        created_at=row["created_at"],
        scheduled_for=row["scheduled_for"],
        payload=json.loads(row["payload"]),
        slots=json.loads(row["slots"]),
        step=row["step"],
        step_token=row["step_token"],
        message_refs=json.loads(row["message_refs"]),
        sent_at=row["sent_at"],
        remind_at=row["remind_at"],
        reminder_sent_at=row["reminder_sent_at"],
        expires_at=row["expires_at"],
        parked_at=row["parked_at"],
        confirmed_at=row["confirmed_at"],
        confirmed_event_id=row["confirmed_event_id"],
        confirmed_summary=row["confirmed_summary"],
        action_id=row["action_id"],
        outcome_reason=row["outcome_reason"],
    )


# Shared with the API's store, which reads and moves key dates for the panel.
PROPOSAL_COLUMNS = _PROPOSAL
proposal_from_row = _proposal


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=list)


def _sender(row: asyncpg.Record | None) -> SenderState:
    if row is None:
        return SenderState()
    return SenderState(
        failed_attempts=row["failed_attempts"],
        blocked_until=row["blocked_until"],
        last_unlinked_reply_at=row["last_unlinked_reply_at"],
    )


async def insert_link_code(conn: asyncpg.Connection, code: LinkCode) -> None:
    """Retire the account's unused codes and store the new one, in one transaction.
    Shared with the API's store (`PostgresStore.save_link_code`): only the API role may
    run it. Raises asyncpg.UniqueViolationError if the hash is already taken."""
    async with conn.transaction():
        await conn.execute(
            """
            UPDATE link_code SET used_at = $2
            WHERE account_id = $1::uuid AND used_at IS NULL
            """,
            code.account_id,
            code.created_at,
        )
        await conn.execute(
            """
            INSERT INTO link_code (code_hash, token_hash, account_id, created_at, expires_at)
            VALUES ($1, $2, $3::uuid, $4, $5)
            """,
            code.code_hash,
            code.token_hash,
            code.account_id,
            code.created_at,
            code.expires_at,
        )


class PostgresConversationStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # --- connections --------------------------------------------------------------

    def _held(self) -> asyncpg.Connection | None:
        held = _HELD.get()
        return held[1] if held is not None and held[0] is self else None

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[asyncpg.Connection]:
        """The connection of the lock this task holds, or one of the pool."""
        held = self._held()
        if held is not None:
            yield held
            return
        async with self._pool.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def lock(self, account_id: str) -> AsyncIterator[None]:
        held = self._held()
        if held is not None:
            # Already inside a lock (of this or another account): same transaction.
            await self._lock_row(held, account_id)
            yield
            return
        async with self._pool.acquire() as conn, conn.transaction():
            await self._lock_row(conn, account_id)
            token = _HELD.set((self, conn))
            try:
                yield
            finally:
                _HELD.reset(token)

    @staticmethod
    async def _lock_row(conn: asyncpg.Connection, account_id: str) -> None:
        await conn.execute(
            "INSERT INTO conversation (account_id) VALUES ($1::uuid) ON CONFLICT DO NOTHING",
            account_id,
        )
        await conn.execute(
            "SELECT id FROM conversation WHERE account_id = $1::uuid FOR UPDATE", account_id
        )

    # --- linked addresses -----------------------------------------------------------

    async def get_identity(self, channel: str, address: str) -> ChannelIdentity | None:
        async with self._conn() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {_IDENTITY} FROM channel_identity
                WHERE channel = $1 AND address = $2 AND unlinked_at IS NULL
                """,  # noqa: S608
                channel,
                address,
            )
        return _identity(row) if row else None

    async def list_identities(self, account_id: str) -> Sequence[ChannelIdentity]:
        async with self._conn() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_IDENTITY} FROM channel_identity
                WHERE account_id = $1::uuid AND unlinked_at IS NULL
                ORDER BY verified_at, id
                """,  # noqa: S608
                account_id,
            )
        return [_identity(r) for r in rows]

    async def link_identity(
        self, account_id: str, channel: str, address: str, now: datetime
    ) -> LinkResult:
        address = normalize_address(channel, address)
        try:
            return await self._link(account_id, channel, address, now)
        except asyncpg.UniqueViolationError:
            # The same address linked by another code at the same moment: once more,
            # now that the other link is visible.
            return await self._link(account_id, channel, address, now)

    async def _link(self, account_id: str, channel: str, address: str, now: datetime) -> LinkResult:
        async with self._conn() as conn, conn.transaction():
            current = await conn.fetchrow(
                f"""
                SELECT {_IDENTITY} FROM channel_identity
                WHERE channel = $1 AND address = $2 AND unlinked_at IS NULL
                FOR UPDATE
                """,  # noqa: S608
                channel,
                address,
            )
            existing = _identity(current) if current else None
            outcome = plan_link(existing, account_id)
            if outcome is LinkOutcome.SAME:
                assert existing is not None
                return LinkResult(existing, outcome)
            previous = None
            if outcome is LinkOutcome.MOVED:
                assert existing is not None
                previous = existing.account_id
                await conn.execute(
                    "UPDATE channel_identity SET unlinked_at = $2 WHERE id = $1",
                    existing.id,
                    now,
                )
            row = await conn.fetchrow(
                f"""
                INSERT INTO channel_identity
                    (account_id, channel, address, verified_at, last_inbound_at)
                VALUES ($1::uuid, $2, $3, $4, $4)
                RETURNING {_IDENTITY}
                """,  # noqa: S608
                account_id,
                channel,
                address,
                now,
            )
        return LinkResult(_identity(row), outcome, previous)

    async def unlink_identity(self, identity_id: str, now: datetime) -> None:
        async with self._conn() as conn:
            await conn.execute(
                """
                UPDATE channel_identity SET unlinked_at = $2
                WHERE id = $1 AND unlinked_at IS NULL
                """,
                identity_id,
                now,
            )

    async def touch_inbound(self, identity_id: str, now: datetime) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE channel_identity SET last_inbound_at = $2 WHERE id = $1",
                identity_id,
                now,
            )

    # --- link codes -------------------------------------------------------------------

    async def save_link_code(self, code: LinkCode) -> None:
        """Codes are created by the web session (the API role): the conversation role
        has no INSERT on link_code and gets a privilege error here."""
        async with self._conn() as conn:
            await insert_link_code(conn, code)

    async def get_link_code(self, code_hash: str) -> LinkCode | None:
        async with self._conn() as conn:
            row = await conn.fetchrow(
                """
                SELECT code_hash, token_hash, account_id::text AS account_id, created_at,
                       expires_at, used_at
                FROM link_code WHERE code_hash = $1 OR token_hash = $1
                LIMIT 1
                """,
                code_hash,
            )
        if row is None:
            return None
        return LinkCode(
            code_hash=row["code_hash"],
            account_id=row["account_id"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            used_at=row["used_at"],
            token_hash=row["token_hash"],
        )

    async def consume_link_code(self, code_hash: str, now: datetime) -> str | None:
        async with self._conn() as conn:
            return await conn.fetchval(
                """
                UPDATE link_code SET used_at = $2
                WHERE (code_hash = $1 OR token_hash = $1)
                  AND used_at IS NULL AND expires_at > $2
                RETURNING account_id::text
                """,
                code_hash,
                now,
            )

    async def get_sender_state(self, address_hash: str) -> SenderState:
        async with self._conn() as conn:
            row = await conn.fetchrow(
                """
                SELECT failed_attempts, blocked_until, last_unlinked_reply_at
                FROM channel_sender WHERE address_hash = $1
                """,
                address_hash,
            )
        return _sender(row)

    async def save_sender_state(self, address_hash: str, state: SenderState) -> None:
        async with self._conn() as conn:
            await conn.execute(
                """
                INSERT INTO channel_sender
                    (address_hash, failed_attempts, blocked_until, last_unlinked_reply_at,
                     updated_at)
                VALUES ($1, $2, $3, $4, now())
                ON CONFLICT (address_hash) DO UPDATE SET
                    failed_attempts = EXCLUDED.failed_attempts,
                    blocked_until = EXCLUDED.blocked_until,
                    last_unlinked_reply_at = EXCLUDED.last_unlinked_reply_at,
                    updated_at = now()
                """,
                address_hash,
                state.failed_attempts,
                state.blocked_until,
                state.last_unlinked_reply_at,
            )

    async def take_link_attempt(self, address_hash: str, now: datetime) -> SenderState | None:
        # One statement: a blocked chat gets no row back (the WHERE of the update), and
        # two attempts of the same chat at once are counted one after the other. A block
        # that is over starts the count again.
        async with self._conn() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO channel_sender AS s
                    (address_hash, failed_attempts, blocked_until, updated_at)
                VALUES ($1, 1, CASE WHEN 1 >= $3 THEN $2::timestamptz + $4::interval END, $2)
                ON CONFLICT (address_hash) DO UPDATE SET
                    failed_attempts = (CASE WHEN s.blocked_until IS NOT NULL
                                            THEN 0 ELSE s.failed_attempts END) + 1,
                    blocked_until = CASE
                        WHEN (CASE WHEN s.blocked_until IS NOT NULL
                                   THEN 0 ELSE s.failed_attempts END) + 1 >= $3
                        THEN $2::timestamptz + $4::interval END,
                    updated_at = $2
                WHERE s.blocked_until IS NULL OR s.blocked_until <= $2
                RETURNING failed_attempts, blocked_until, last_unlinked_reply_at
                """,
                address_hash,
                now,
                MAX_FAILED_ATTEMPTS,
                BLOCK_DURATION,
            )
        return _sender(row) if row is not None else None

    async def clear_link_attempts(self, address_hash: str) -> None:
        async with self._conn() as conn:
            await conn.execute(
                """
                UPDATE channel_sender SET failed_attempts = 0, blocked_until = NULL,
                                          updated_at = now()
                WHERE address_hash = $1
                """,
                address_hash,
            )

    async def claim_unlinked_reply(self, address_hash: str, now: datetime) -> bool:
        async with self._conn() as conn:
            claimed = await conn.fetchval(
                """
                INSERT INTO channel_sender AS s
                    (address_hash, last_unlinked_reply_at, updated_at)
                VALUES ($1, $2, $2)
                ON CONFLICT (address_hash) DO UPDATE SET
                    last_unlinked_reply_at = $2, updated_at = $2
                WHERE (s.blocked_until IS NULL OR s.blocked_until <= $2)
                  AND (s.last_unlinked_reply_at IS NULL
                       OR s.last_unlinked_reply_at <= $2::timestamptz - $3::interval)
                RETURNING 1
                """,
                address_hash,
                now,
                UNLINKED_REPLY_EVERY,
            )
        return claimed is not None

    async def link_failures(self, now: datetime) -> int:
        async with self._conn() as conn:
            failures = await conn.fetchval(
                "SELECT failures FROM link_failure_window WHERE window_start = $1",
                failure_window(now),
            )
        return failures or 0

    async def count_link_failure(self, now: datetime) -> int:
        async with self._conn() as conn:
            return await conn.fetchval(
                """
                INSERT INTO link_failure_window AS w (window_start, failures) VALUES ($1, 1)
                ON CONFLICT (window_start) DO UPDATE SET failures = w.failures + 1
                RETURNING failures
                """,
                failure_window(now),
            )

    # --- accounts -----------------------------------------------------------------------

    async def get_account_info(self, account_id: str) -> AccountInfo:
        # The time zone is not in `account` yet: the peninsula until it is.
        async with self._conn() as conn:
            title = await conn.fetchval(
                """
                SELECT l.title FROM account a
                JOIN location l ON l.id = a.selected_location_id AND l.account_id = a.id
                WHERE a.id = $1::uuid AND a.deleted_at IS NULL AND l.removed_at IS NULL
                """,
                account_id,
            )
        return AccountInfo(business_name=title or "", timezone=DEFAULT_TIMEZONE)

    # --- conversations ------------------------------------------------------------------

    async def get_conversation(self, account_id: str) -> Conversation:
        async with self._conn() as conn:
            await conn.execute(
                "INSERT INTO conversation (account_id) VALUES ($1::uuid) ON CONFLICT DO NOTHING",
                account_id,
            )
            row = await conn.fetchrow(
                """
                SELECT id, account_id::text AS account_id, active_proposal_id, unknown_streak
                FROM conversation WHERE account_id = $1::uuid
                """,
                account_id,
            )
        return Conversation(
            id=row["id"],
            account_id=row["account_id"],
            active_proposal_id=row["active_proposal_id"],
            unknown_streak=row["unknown_streak"],
        )

    async def save_conversation(self, conversation: Conversation) -> None:
        async with self._conn() as conn:
            await conn.execute(
                """
                UPDATE conversation
                SET active_proposal_id = $2, unknown_streak = $3, updated_at = now()
                WHERE account_id = $1::uuid
                """,
                conversation.account_id,
                conversation.active_proposal_id,
                min(conversation.unknown_streak, 1000),
            )

    async def accounts_with_open_work(self) -> Sequence[str]:
        async with self._conn() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT account_id::text AS account_id FROM proposal
                WHERE state = ANY($1::text[]) ORDER BY 1
                """,
                _UNFINISHED,
            )
        return [r["account_id"] for r in rows]

    # --- messages -----------------------------------------------------------------------

    async def record_inbound(self, channel: str, event_id: str, now: datetime) -> bool:
        async with self._conn() as conn:
            status = await conn.execute(
                """
                INSERT INTO channel_inbound_event (channel, event_id, received_at)
                VALUES ($1, $2, $3) ON CONFLICT DO NOTHING
                """,
                channel,
                event_id,
                now,
            )
        return status.endswith(" 1")

    async def record_message(self, record: MessageRecord) -> None:
        if record.conversation_id is None:
            return  # nothing of an address that is not linked is stored
        async with self._conn() as conn:
            await conn.execute(
                """
                INSERT INTO channel_message
                    (at, conversation_id, identity_id, direction, channel, channel_message_id,
                     kind, key, body, proposal_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                record.at,
                record.conversation_id,
                record.identity_id,
                record.direction,
                record.channel,
                record.channel_message_id[:120],
                record.kind,
                record.key,
                record.body[:4096],
                record.proposal_id,
            )

    # --- proposals ----------------------------------------------------------------------

    async def add_proposal(self, proposal: Proposal) -> bool:
        async with self._conn() as conn:
            status = await conn.execute(
                """
                INSERT INTO proposal
                    (id, account_id, type, source_ref, state, priority, payload, slots, step,
                     step_token, message_refs, created_at, scheduled_for, sent_at, remind_at,
                     reminder_sent_at, expires_at, parked_at)
                VALUES ($1, $2::uuid, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, $10,
                        $11::jsonb, $12, $13, $14, $15, $16, $17, $18)
                ON CONFLICT (account_id, type, source_ref) DO NOTHING
                """,
                proposal.id,
                proposal.account_id,
                proposal.type.value,
                proposal.source_ref,
                proposal.state.value,
                proposal.priority,
                _json(dict(proposal.payload)),
                _json(dict(proposal.slots)),
                proposal.step,
                proposal.step_token,
                _json(dict(proposal.message_refs)),
                proposal.created_at,
                proposal.scheduled_for,
                proposal.sent_at,
                proposal.remind_at,
                proposal.reminder_sent_at,
                proposal.expires_at,
                proposal.parked_at,
            )
        return status.endswith(" 1")

    async def get_proposal(self, proposal_id: str) -> Proposal | None:
        async with self._conn() as conn:
            row = await conn.fetchrow(
                f"SELECT {_PROPOSAL} FROM proposal WHERE id = $1",  # noqa: S608
                proposal_id,
            )
        return _proposal(row) if row else None

    async def save_proposal(self, proposal: Proposal) -> None:
        # What a proposal was created with (account, type, origin, payload) never changes:
        # the role has no UPDATE on those columns.
        async with self._conn() as conn:
            await conn.execute(
                """
                UPDATE proposal SET
                    state = $2, priority = $3, slots = $4::jsonb, step = $5, step_token = $6,
                    message_refs = $7::jsonb, scheduled_for = $8, sent_at = $9,
                    remind_at = $10, reminder_sent_at = $11, expires_at = $12,
                    parked_at = $13, confirmed_at = $14, confirmed_event_id = $15,
                    confirmed_summary = $16, action_id = $17, outcome_reason = $18
                WHERE id = $1
                """,
                proposal.id,
                proposal.state.value,
                proposal.priority,
                _json(dict(proposal.slots)),
                proposal.step,
                proposal.step_token,
                _json(dict(proposal.message_refs)),
                proposal.scheduled_for,
                proposal.sent_at,
                proposal.remind_at,
                proposal.reminder_sent_at,
                proposal.expires_at,
                proposal.parked_at,
                proposal.confirmed_at,
                proposal.confirmed_event_id,
                proposal.confirmed_summary,
                proposal.action_id,
                proposal.outcome_reason,
            )

    async def list_unfinished_proposals(self, account_id: str) -> Sequence[Proposal]:
        async with self._conn() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_PROPOSAL} FROM proposal
                WHERE account_id = $1::uuid AND state = ANY($2::text[])
                ORDER BY created_at, id
                """,  # noqa: S608
                account_id,
                _UNFINISHED,
            )
        return [_proposal(r) for r in rows]

    async def get_proposal_by_source(
        self, account_id: str, proposal_type: str, source_ref: str
    ) -> Proposal | None:
        async with self._conn() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {_PROPOSAL} FROM proposal
                WHERE account_id = $1::uuid AND type = $2 AND source_ref = $3
                """,  # noqa: S608
                account_id,
                str(proposal_type),
                source_ref,
            )
        return _proposal(row) if row else None

    async def get_proposal_by_action(self, action_id: str) -> Proposal | None:
        async with self._conn() as conn:
            row = await conn.fetchrow(
                f"SELECT {_PROPOSAL} FROM proposal WHERE action_id = $1 LIMIT 1",  # noqa: S608
                action_id,
            )
        return _proposal(row) if row else None

    async def count_capped_sent_since(self, account_id: str, since: datetime) -> int:
        async with self._conn() as conn:
            return await conn.fetchval(
                """
                SELECT count(*) FROM proposal
                WHERE account_id = $1::uuid AND type = $2 AND sent_at >= $3
                """,
                account_id,
                ProposalType.KEY_DATE.value,
                since,
            )

    # --- outside the conversation: what its jobs read and write -------------------------

    async def review_targets(self, review_id: str) -> ReviewTarget | None:
        """The review and the accounts it may be proposed to: those whose selected
        profile has it (same Google user and profile), not deleted, without a scheduled
        leave, with an address linked before the review was written."""
        async with self._conn() as conn:
            row = await conn.fetchrow(
                """
                SELECT r.rating, r.comment, r.created_at,
                       r.reply_text IS NULL AS unanswered, r.removed_at IS NULL AS live,
                       array(
                           SELECT a.id::text FROM account a
                           JOIN location l
                             ON l.id = a.selected_location_id AND l.account_id = a.id
                            AND l.removed_at IS NULL
                           WHERE a.google_sub = r.google_sub AND l.gbp_location = r.location
                             AND a.deleted_at IS NULL
                             AND NOT EXISTS (SELECT 1 FROM offboarding o
                                             WHERE o.account_id = a.id
                                               AND o.restored_at IS NULL
                                               AND o.effective_at IS NULL)
                             AND (SELECT min(ci.verified_at) FROM channel_identity ci
                                  WHERE ci.account_id = a.id AND ci.unlinked_at IS NULL)
                                 <= r.created_at
                           ORDER BY a.id
                       ) AS accounts
                FROM review r WHERE r.id = $1
                """,
                review_id,
            )
        if row is None:
            return None
        return ReviewTarget(
            stars=row["rating"],
            text=row["comment"],
            created_at=row["created_at"],
            unanswered=row["unanswered"],
            live=row["live"],
            accounts=tuple(row["accounts"]),
        )

    async def linked_profiles(self) -> list[tuple[str, str]]:
        """(google_sub, Google location) of every selected profile of an account with a
        linked address, not deleted and without a scheduled leave: what the planner
        keeps in sync."""
        async with self._conn() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT a.google_sub, l.gbp_location
                FROM channel_identity ci
                JOIN account a ON a.id = ci.account_id
                JOIN location l
                  ON l.id = a.selected_location_id AND l.account_id = a.id
                 AND l.removed_at IS NULL
                WHERE ci.unlinked_at IS NULL AND a.deleted_at IS NULL
                  AND NOT EXISTS (SELECT 1 FROM offboarding o
                                  WHERE o.google_sub = a.google_sub
                                    AND o.restored_at IS NULL AND o.purged_at IS NULL)
                ORDER BY 1, 2
                """
            )
        return [(r["google_sub"], r["gbp_location"]) for r in rows]

    async def key_date_profiles(self) -> list[KeyDateProfile]:
        """The profiles the planner of key dates asks about (KeyDateProfiles)."""
        async with self._conn() as conn:
            rows = await conn.fetch(
                """
                SELECT a.id::text AS account_id, l.id AS location_id, p.facts::text AS facts
                FROM account a
                JOIN location l
                  ON l.id = a.selected_location_id AND l.account_id = a.id
                 AND l.removed_at IS NULL
                JOIN profile_summary p
                  ON p.google_sub = a.google_sub AND p.location = l.gbp_location
                WHERE a.deleted_at IS NULL AND a.disconnected_at IS NULL
                  AND p.status <> 'needs_reconnect' AND p.facts IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM offboarding o
                                  WHERE o.google_sub = a.google_sub
                                    AND o.restored_at IS NULL AND o.purged_at IS NULL)
                ORDER BY a.id
                """
            )
        found = []
        for row in rows:
            facts = facts_from_json(json.loads(row["facts"]))
            if facts is None:
                continue  # unreadable or of another version: the next sync rewrites them
            found.append(
                KeyDateProfile(
                    row["account_id"],
                    row["location_id"],
                    facts.region,
                    facts.address,
                    facts.special_dates,
                )
            )
        return found

    async def scrub_job(self, job_id: str) -> None:
        """Empty the payload of a job: the update of a webhook may carry a link code."""
        async with self._conn() as conn:
            await conn.execute("UPDATE job SET payload = '{}'::jsonb WHERE id = $1::uuid", job_id)

    async def scrub_finished_jobs(self, kind: str) -> int:
        async with self._conn() as conn:
            status = await conn.execute(
                """
                UPDATE job SET payload = '{}'::jsonb
                WHERE kind = $1 AND state IN ('done', 'failed') AND payload <> '{}'::jsonb
                """,
                kind,
            )
        return int(status.rsplit(" ", 1)[-1])
