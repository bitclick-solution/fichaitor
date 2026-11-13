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
- Cobro (step 7 of docs/admin-facturacion.md): an account blocked for non-payment
  (`fichaitor_account_blocked`, the condition the API and the worker use too) is left out
  of the planner, of the review targets, of the key dates and of the lanes with work.
- Multinegocio (docs/multinegocio.md, step 6a): one lane per profile
  (`conversation_lane`), the filter of each chat and its focus. A new proposal must be
  of an active profile of its account: the database refuses it otherwise
  (`proposal_location_guard`), and that refusal is `LocationInactive` here.
"""

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Any

import asyncpg

from app.domain import message_allowance as allowance
from app.domain.channel_messages import business_label
from app.domain.conversation import (
    BILLING_NOTICE_EVERY,
    DEFAULT_TIMEZONE,
    AccountInfo,
    ChannelIdentity,
    Conversation,
    Focus,
    LinkOutcome,
    normalize_address,
    plan_link,
    timezone_for,
)
from app.domain.link_codes import (
    BLOCK_DURATION,
    MAX_FAILED_ATTEMPTS,
    UNLINKED_REPLY_EVERY,
    LinkCode,
    SenderState,
    failure_window,
)
from app.domain.plans import Plan, parse_plan
from app.domain.profile_summary import facts_from_json
from app.domain.proposals import Proposal, ProposalState, ProposalType
from app.ports.conversation_store import (
    KeyDateProfile,
    Lane,
    LinkResult,
    LocationInactive,
    MessageRecord,
    ReviewTarget,
)


class _BagFull(Exception):
    """The bag of the month has no room for one more message: undo what was counted."""


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
# A leave confirmed and not yet restored nor effective (`o` is the leave).
_SCHEDULED = "o.restored_at IS NULL AND o.effective_at IS NULL"
# The messages of `proposal_location_guard` (db/schema_channels.sql).
_LOCATION_REFUSALS = ("ficha_inactiva", "propuesta_sin_ficha")

_IDENTITY = (
    "id, account_id::text AS account_id, channel, address, verified_at, last_inbound_at,"
    " location_filter"
)
_PROPOSAL = """
    id, account_id::text AS account_id, type, source_ref, state, priority,
    payload::text AS payload, slots::text AS slots, step, step_token,
    message_refs::text AS message_refs, created_at, scheduled_for, sent_at, remind_at,
    reminder_sent_at, expires_at, parked_at, confirmed_at, confirmed_event_id,
    confirmed_summary, action_id, outcome_reason, location_id
"""


def _identity(row: asyncpg.Record) -> ChannelIdentity:
    return ChannelIdentity(
        id=row["id"],
        account_id=row["account_id"],
        channel=row["channel"],
        address=row["address"],
        verified_at=row["verified_at"],
        last_inbound_at=row["last_inbound_at"],
        location_filter=tuple(row["location_filter"])
        if row["location_filter"] is not None
        else None,
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
        location_id=row.get("location_id"),
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
            INSERT INTO link_code
                (code_hash, token_hash, account_id, created_at, expires_at, location_filter)
            VALUES ($1, $2, $3::uuid, $4, $5, $6::text[])
            """,
            code.code_hash,
            code.token_hash,
            code.account_id,
            code.created_at,
            code.expires_at,
            list(code.location_filter) if code.location_filter is not None else None,
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
        self,
        account_id: str,
        channel: str,
        address: str,
        now: datetime,
        location_filter: tuple[str, ...] | None = None,
    ) -> LinkResult:
        address = normalize_address(channel, address)
        try:
            return await self._link(account_id, channel, address, now, location_filter)
        except asyncpg.UniqueViolationError:
            # The same address linked by another code at the same moment: once more,
            # now that the other link is visible.
            return await self._link(account_id, channel, address, now, location_filter)

    async def _link(
        self,
        account_id: str,
        channel: str,
        address: str,
        now: datetime,
        location_filter: tuple[str, ...] | None,
    ) -> LinkResult:
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
                    (account_id, channel, address, verified_at, last_inbound_at,
                     location_filter)
                VALUES ($1::uuid, $2, $3, $4, $4, $5::text[])
                RETURNING {_IDENTITY}
                """,  # noqa: S608
                account_id,
                channel,
                address,
                now,
                list(location_filter) if location_filter is not None else None,
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

    async def identities_for(self, account_id: str, location_id: str) -> Sequence[ChannelIdentity]:
        async with self._conn() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_IDENTITY} FROM channel_identity
                WHERE account_id = $1::uuid AND unlinked_at IS NULL
                  AND (location_filter IS NULL OR $2 = ANY(location_filter))
                  AND EXISTS (SELECT 1 FROM location l
                              WHERE l.account_id = $1::uuid AND l.id = $2)
                ORDER BY verified_at, id
                """,  # noqa: S608
                account_id,
                location_id,
            )
        return [_identity(r) for r in rows]

    async def set_focus(self, identity_id: str, proposal_id: str, now: datetime) -> None:
        async with self._conn() as conn:
            await conn.execute(
                """
                UPDATE channel_identity SET focus_proposal_id = $2, focus_at = $3
                WHERE id = $1 AND unlinked_at IS NULL
                """,
                identity_id,
                proposal_id,
                now,
            )

    async def get_focus(self, identity_id: str) -> Focus | None:
        async with self._conn() as conn:
            row = await conn.fetchrow(
                """
                SELECT focus_proposal_id, focus_at FROM channel_identity
                WHERE id = $1 AND unlinked_at IS NULL
                """,
                identity_id,
            )
        if row is None or row["focus_proposal_id"] is None or row["focus_at"] is None:
            return None
        return Focus(row["focus_proposal_id"], row["focus_at"])

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
                       expires_at, used_at, location_filter
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
            location_filter=(
                tuple(row["location_filter"]) if row["location_filter"] is not None else None
            ),
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
        return AccountInfo(timezone=DEFAULT_TIMEZONE)

    async def location_label(self, account_id: str, location_id: str) -> str:
        async with self._conn() as conn:
            row = await conn.fetchrow(
                """
                SELECT l.title, p.facts::text AS facts,
                       EXISTS (SELECT 1 FROM location o
                               WHERE o.account_id = l.account_id AND o.id <> l.id
                                 AND o.active_since IS NOT NULL
                                 AND lower(btrim(o.title)) = lower(btrim(l.title))) AS twin
                FROM location l
                JOIN account a ON a.id = l.account_id
                LEFT JOIN profile_summary p
                  ON p.google_sub = a.google_sub AND p.location = l.gbp_location
                WHERE l.account_id = $1::uuid AND l.id = $2
                """,
                account_id,
                location_id,
            )
        if row is None:
            return business_label("")
        address = ""
        if row["twin"] and row["facts"] is not None:
            facts = facts_from_json(json.loads(row["facts"]))
            address = facts.address if facts is not None else ""
        return business_label(
            row["title"],
            same_title_elsewhere=row["twin"],
            address=address,
            location_id=location_id,
        )

    async def active_locations(self, account_id: str) -> Sequence[str]:
        async with self._conn() as conn:
            rows = await conn.fetch(
                """
                SELECT l.id FROM location l
                JOIN account a ON a.id = l.account_id AND a.deleted_at IS NULL
                WHERE l.account_id = $1::uuid AND l.active_since IS NOT NULL
                  AND l.removed_at IS NULL
                ORDER BY l.active_since, l.id
                """,
                account_id,
            )
        return [r["id"] for r in rows]

    async def location_active(self, account_id: str, location_id: str) -> bool:
        async with self._conn() as conn:
            return await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM location l
                    JOIN account a ON a.id = l.account_id AND a.deleted_at IS NULL
                    WHERE l.account_id = $1::uuid AND l.id = $2
                      AND l.active_since IS NOT NULL AND l.removed_at IS NULL)
                """,
                account_id,
                location_id,
            )

    async def location_plan(self, account_id: str, location_id: str) -> Plan | None:
        async with self._conn() as conn:
            value = await conn.fetchval(
                """
                SELECT l.plan FROM location l
                JOIN account a ON a.id = l.account_id AND a.deleted_at IS NULL
                WHERE l.account_id = $1::uuid AND l.id = $2
                  AND l.active_since IS NOT NULL AND l.removed_at IS NULL
                """,
                account_id,
                location_id,
            )
        return parse_plan(value)

    # --- the bag of WhatsApp messages (app/domain/message_allowance.py) ----------------

    async def take_message_allowance(self, account_id: str, units: int, now: datetime) -> bool:
        """In usage_counter, as every cap: the first message creates the counter with 1 and
        each one after adds 1 while below the limit (B11: it only goes up, one by one). The
        limit is read in the same transaction: 80 per active Pro+ profile now. Several
        units are counted one by one inside a savepoint: all of them or none."""
        if units < 1:
            return False
        info = await self.get_account_info(account_id)
        start, end = allowance.month_window(now, timezone_for(info.timezone))
        scope = allowance.scope(account_id)
        async with self._conn() as conn, conn.transaction():
            profiles = await conn.fetchval(
                """
                SELECT count(*) FROM location l
                JOIN account a ON a.id = l.account_id AND a.deleted_at IS NULL
                WHERE l.account_id = $1::uuid AND l.plan = 'pro_whatsapp'
                  AND l.active_since IS NOT NULL AND l.removed_at IS NULL
                """,
                account_id,
            )
            limit = allowance.monthly_limit(int(profiles or 0))
            if limit < units:
                return False
            try:
                async with conn.transaction():
                    for _ in range(units):
                        hits = await conn.fetchval(
                            """
                            INSERT INTO usage_counter AS u (scope, window_start, expires_at, hits)
                            VALUES ($1, $2, $3, 1)
                            ON CONFLICT (scope, window_start) DO UPDATE SET hits = u.hits + 1
                            WHERE u.hits < $4
                            RETURNING hits
                            """,
                            scope,
                            start,
                            end,
                            limit,
                        )
                        if hits is None:
                            raise _BagFull
            except _BagFull:
                return False
        return True

    # --- conversations ------------------------------------------------------------------

    async def get_conversation(self, account_id: str) -> Conversation:
        async with self._conn() as conn:
            await conn.execute(
                "INSERT INTO conversation (account_id) VALUES ($1::uuid) ON CONFLICT DO NOTHING",
                account_id,
            )
            row = await conn.fetchrow(
                """
                SELECT id, account_id::text AS account_id, unknown_streak
                FROM conversation WHERE account_id = $1::uuid
                """,
                account_id,
            )
        return Conversation(
            id=row["id"],
            account_id=row["account_id"],
            unknown_streak=row["unknown_streak"],
        )

    async def save_conversation(self, conversation: Conversation) -> None:
        async with self._conn() as conn:
            await conn.execute(
                """
                UPDATE conversation SET unknown_streak = $2, updated_at = now()
                WHERE account_id = $1::uuid
                """,
                conversation.account_id,
                min(conversation.unknown_streak, 1000),
            )

    # --- lanes ---------------------------------------------------------------------------

    async def get_lane(self, account_id: str, location_id: str) -> Lane:
        async with self._conn() as conn:
            active = await conn.fetchval(
                """
                SELECT active_proposal_id FROM conversation_lane
                WHERE account_id = $1::uuid AND location_id = $2
                """,
                account_id,
                location_id,
            )
        return Lane(account_id, location_id, active)

    async def save_lane(self, lane: Lane) -> None:
        async with self._conn() as conn:
            await conn.execute(
                """
                INSERT INTO conversation_lane (account_id, location_id, active_proposal_id)
                VALUES ($1::uuid, $2, $3)
                ON CONFLICT (account_id, location_id) DO UPDATE
                    SET active_proposal_id = EXCLUDED.active_proposal_id, updated_at = now()
                """,
                lane.account_id,
                lane.location_id,
                lane.active_proposal_id,
            )

    async def lanes_with_open_work(self) -> Sequence[tuple[str, str]]:
        async with self._conn() as conn:
            rows = await conn.fetch(
                f"""
                SELECT DISTINCT p.account_id::text AS account_id, p.location_id
                FROM proposal p
                WHERE p.state = ANY($1::text[]) AND p.location_id IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM offboarding o
                                  WHERE o.account_id = p.account_id AND {_SCHEDULED})
                  AND NOT fichaitor_account_blocked(p.account_id)
                ORDER BY 1, 2
                """,  # noqa: S608
                _UNFINISHED,
            )
        return [(r["account_id"], r["location_id"]) for r in rows]

    async def on_leave(self, account_id: str) -> bool:
        async with self._conn() as conn:
            return await conn.fetchval(
                f"""
                SELECT EXISTS (SELECT 1 FROM offboarding o
                               WHERE o.account_id = $1::uuid AND {_SCHEDULED})
                """,  # noqa: S608
                account_id,
            )

    async def billing_blocked(self, account_id: str) -> bool:
        async with self._conn() as conn:
            return await conn.fetchval("SELECT fichaitor_account_blocked($1::uuid)", account_id)

    async def claim_paused_notice(self, account_id: str, now: datetime) -> bool:
        # One statement: two messages at once tell the account once.
        async with self._conn() as conn:
            await conn.execute(
                "INSERT INTO conversation (account_id) VALUES ($1::uuid) ON CONFLICT DO NOTHING",
                account_id,
            )
            claimed = await conn.fetchval(
                """
                UPDATE conversation SET billing_notice_at = $2, updated_at = now()
                WHERE account_id = $1::uuid
                  AND (billing_notice_at IS NULL
                       OR billing_notice_at <= $2::timestamptz - $3::interval)
                RETURNING 1
                """,
                account_id,
                now,
                BILLING_NOTICE_EVERY,
            )
        return claimed is not None

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
        try:
            return await self._insert_proposal(proposal)
        except asyncpg.CheckViolationError as exc:
            if str(exc) in _LOCATION_REFUSALS:
                raise LocationInactive from None
            raise
        except asyncpg.ForeignKeyViolationError as exc:
            if getattr(exc, "constraint_name", None) == "proposal_location_fk":
                raise LocationInactive from None
            raise

    async def _insert_proposal(self, proposal: Proposal) -> bool:
        # In a savepoint when the lock is held: a refused proposal does not spoil the
        # transaction of the thread.
        async with self._conn() as conn, conn.transaction():
            status = await conn.execute(
                """
                INSERT INTO proposal
                    (id, account_id, type, source_ref, state, priority, payload, slots, step,
                     step_token, message_refs, created_at, scheduled_for, sent_at, remind_at,
                     reminder_sent_at, expires_at, parked_at, location_id)
                VALUES ($1, $2::uuid, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, $10,
                        $11::jsonb, $12, $13, $14, $15, $16, $17, $18, $19)
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
                proposal.location_id,
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

    async def list_unfinished_proposals(
        self, account_id: str, location_id: str | None = None
    ) -> Sequence[Proposal]:
        async with self._conn() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {_PROPOSAL} FROM proposal
                WHERE account_id = $1::uuid AND state = ANY($2::text[])
                  AND ($3::text IS NULL OR location_id = $3)
                ORDER BY created_at, id
                """,  # noqa: S608
                account_id,
                _UNFINISHED,
                location_id,
            )
        return [_proposal(r) for r in rows]

    async def proposal_by_message_ref(
        self, account_id: str, identity_id: str, message_ref: str
    ) -> Proposal | None:
        if not message_ref:
            return None
        async with self._conn() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT {_PROPOSAL} FROM proposal
                WHERE account_id = $1::uuid AND state = ANY($2::text[])
                  AND message_refs->>$3 = $4
                ORDER BY created_at DESC, id
                LIMIT 1
                """,  # noqa: S608
                account_id,
                _UNFINISHED,
                identity_id,
                message_ref,
            )
        return _proposal(row) if row else None

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

    async def count_capped_sent_since(
        self, account_id: str, since: datetime, location_id: str | None = None
    ) -> int:
        async with self._conn() as conn:
            return await conn.fetchval(
                """
                SELECT count(*) FROM proposal
                WHERE account_id = $1::uuid AND type = $2 AND sent_at >= $3
                  AND ($4::text IS NULL OR location_id = $4)
                """,
                account_id,
                ProposalType.KEY_DATE.value,
                since,
                location_id,
            )

    # --- outside the conversation: what its jobs read and write -------------------------

    async def review_targets(self, review_id: str) -> ReviewTarget | None:
        """The review and whom it may be proposed to (ReviewTarget.targets): every
        active profile with it (same Google user and profile), of an account not deleted,
        without a scheduled leave and not blocked for non-payment, whose last sync is
        "ok" (docs/multinegocio.md, 6.5) and with an address that sees that profile
        linked before the review was written."""
        async with self._conn() as conn:
            row = await conn.fetchrow(
                """
                SELECT r.rating, r.comment, r.created_at,
                       r.reply_text IS NULL AS unanswered, r.removed_at IS NULL AS live
                FROM review r WHERE r.id = $1
                """,
                review_id,
            )
            if row is None:
                return None
            targets = await conn.fetch(
                f"""
                SELECT a.id::text AS account_id, l.id AS location_id
                FROM review r
                JOIN account a ON a.google_sub = r.google_sub
                JOIN location l
                  ON l.account_id = a.id AND l.gbp_location = r.location
                 AND l.active_since IS NOT NULL AND l.removed_at IS NULL
                JOIN profile_summary ps
                  ON ps.google_sub = a.google_sub AND ps.location = l.gbp_location
                 AND ps.status = 'ok'
                WHERE r.id = $1 AND a.deleted_at IS NULL
                  AND NOT EXISTS (SELECT 1 FROM offboarding o
                                  WHERE o.account_id = a.id AND {_SCHEDULED})
                  AND NOT fichaitor_account_blocked(a.id)
                  AND (SELECT min(ci.verified_at) FROM channel_identity ci
                       WHERE ci.account_id = a.id AND ci.unlinked_at IS NULL
                         AND (ci.location_filter IS NULL OR l.id = ANY(ci.location_filter)))
                      <= r.created_at
                ORDER BY 1, 2
                """,  # noqa: S608
                review_id,
            )
        return ReviewTarget(
            stars=row["rating"],
            text=row["comment"],
            created_at=row["created_at"],
            unanswered=row["unanswered"],
            live=row["live"],
            targets=tuple((t["account_id"], t["location_id"]) for t in targets),
        )

    async def linked_profiles(self) -> list[tuple[str, str]]:
        """(google_sub, Google location) of every active profile of an account not
        deleted, without a scheduled leave and not blocked for non-payment, with a linked
        address that sees it: what the planner keeps in sync. Once a blocked account
        pays, its profiles are back in the next round."""
        async with self._conn() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT a.google_sub, l.gbp_location
                FROM channel_identity ci
                JOIN account a ON a.id = ci.account_id
                JOIN location l
                  ON l.account_id = a.id AND l.active_since IS NOT NULL
                 AND l.removed_at IS NULL
                 AND (ci.location_filter IS NULL OR l.id = ANY(ci.location_filter))
                WHERE ci.unlinked_at IS NULL AND a.deleted_at IS NULL
                  AND NOT EXISTS (SELECT 1 FROM offboarding o
                                  WHERE o.google_sub = a.google_sub
                                    AND o.restored_at IS NULL AND o.purged_at IS NULL)
                  AND NOT fichaitor_account_blocked(a.id)
                ORDER BY 1, 2
                """
            )
        return [(r["google_sub"], r["gbp_location"]) for r in rows]

    async def key_date_profiles(self) -> list[KeyDateProfile]:
        """The profiles the planner of key dates asks about (KeyDateProfiles)."""
        async with self._conn() as conn:
            rows = await conn.fetch(
                """
                SELECT a.id::text AS account_id, l.id AS location_id, l.plan,
                       p.facts::text AS facts
                FROM account a
                JOIN location l
                  ON l.account_id = a.id AND l.active_since IS NOT NULL
                 AND l.removed_at IS NULL
                JOIN profile_summary p
                  ON p.google_sub = a.google_sub AND p.location = l.gbp_location
                WHERE a.deleted_at IS NULL AND a.disconnected_at IS NULL
                  AND p.status <> 'needs_reconnect' AND p.facts IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM offboarding o
                                  WHERE o.google_sub = a.google_sub
                                    AND o.restored_at IS NULL AND o.purged_at IS NULL)
                  AND NOT fichaitor_account_blocked(a.id)
                ORDER BY a.id, l.id
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
                    parse_plan(row["plan"]),
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
