"""In-memory conversation store for tests and local development. Same rules as the
tables of `db/schema_channels.sql`, single process."""

import asyncio
import itertools
from collections import defaultdict
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime

from app.domain.conversation import (
    AccountInfo,
    ChannelIdentity,
    Conversation,
    LinkOutcome,
    normalize_address,
    plan_link,
)
from app.domain.link_codes import (
    CodeCheck,
    LinkCode,
    SenderState,
    check_code,
    failure_window,
    is_blocked,
    may_send_unlinked_reply,
    register_attempt,
    register_success,
)
from app.domain.proposals import Proposal, counts_towards_weekly_cap
from app.ports.conversation_store import LinkResult, MessageRecord


class MemoryConversationStore:
    def __init__(self) -> None:
        self.identities: dict[str, ChannelIdentity] = {}
        self.link_codes: dict[str, LinkCode] = {}
        self.senders: dict[str, SenderState] = {}
        self.failures: dict[datetime, int] = {}  # global wrong codes, by window
        self.accounts: dict[str, AccountInfo] = {}
        self.conversations: dict[str, Conversation] = {}
        self.messages: list[MessageRecord] = []
        self.proposals: dict[str, Proposal] = {}
        self._seen: set[tuple[str, str]] = set()
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._ids = itertools.count(1)
        # Accounts with a leave scheduled (in PostgreSQL, a row of `offboarding`).
        self.leaves: set[str] = set()

    async def on_leave(self, account_id: str) -> bool:
        return account_id in self.leaves

    # --- linked addresses -------------------------------------------------------
    async def get_identity(self, channel: str, address: str) -> ChannelIdentity | None:
        for identity in self.identities.values():
            if identity.active and (identity.channel, identity.address) == (channel, address):
                return identity
        return None

    async def list_identities(self, account_id: str) -> Sequence[ChannelIdentity]:
        found = [i for i in self.identities.values() if i.active and i.account_id == account_id]
        return sorted(found, key=lambda i: (i.verified_at, i.id))

    async def link_identity(
        self, account_id: str, channel: str, address: str, now: datetime
    ) -> LinkResult:
        address = normalize_address(channel, address)
        current = await self.get_identity(channel, address)
        outcome = plan_link(current, account_id)
        if outcome is LinkOutcome.SAME:
            assert current is not None
            return LinkResult(current, outcome)
        previous = None
        if outcome is LinkOutcome.MOVED:
            assert current is not None
            previous = current.account_id
            self.identities[current.id] = replace(current, unlinked_at=now)
        identity = ChannelIdentity(
            id=f"chid_{next(self._ids):08x}",
            account_id=account_id,
            channel=channel,
            address=address,
            verified_at=now,
            last_inbound_at=now,
        )
        self.identities[identity.id] = identity
        return LinkResult(identity, outcome, previous)

    async def unlink_identity(self, identity_id: str, now: datetime) -> None:
        identity = self.identities.get(identity_id)
        if identity is not None and identity.active:
            self.identities[identity_id] = replace(identity, unlinked_at=now)

    async def touch_inbound(self, identity_id: str, now: datetime) -> None:
        identity = self.identities.get(identity_id)
        if identity is not None:
            self.identities[identity_id] = replace(identity, last_inbound_at=now)

    # --- link codes -------------------------------------------------------------
    async def save_link_code(self, code: LinkCode) -> None:
        for key, other in list(self.link_codes.items()):
            if other.account_id == code.account_id and other.used_at is None:
                del self.link_codes[key]
        self.link_codes[code.code_hash] = code

    def _find_code(self, code_hash: str) -> LinkCode | None:
        record = self.link_codes.get(code_hash)
        if record is None:
            record = next((c for c in self.link_codes.values() if c.token_hash == code_hash), None)
        return record

    async def get_link_code(self, code_hash: str) -> LinkCode | None:
        return self._find_code(code_hash)

    async def consume_link_code(self, code_hash: str, now: datetime) -> str | None:
        record = self._find_code(code_hash)
        if record is None or check_code(record, now) is not CodeCheck.OK:
            return None
        self.link_codes[record.code_hash] = replace(record, used_at=now)
        return record.account_id

    async def get_sender_state(self, address_hash: str) -> SenderState:
        return self.senders.get(address_hash, SenderState())

    async def save_sender_state(self, address_hash: str, state: SenderState) -> None:
        self.senders[address_hash] = state

    async def take_link_attempt(self, address_hash: str, now: datetime) -> SenderState | None:
        taken = register_attempt(await self.get_sender_state(address_hash), now)
        if taken is not None:
            self.senders[address_hash] = taken
        return taken

    async def clear_link_attempts(self, address_hash: str) -> None:
        state = await self.get_sender_state(address_hash)
        self.senders[address_hash] = register_success(state)

    async def claim_unlinked_reply(self, address_hash: str, now: datetime) -> bool:
        state = await self.get_sender_state(address_hash)
        if is_blocked(state, now) or not may_send_unlinked_reply(state, now):
            return False
        self.senders[address_hash] = replace(state, last_unlinked_reply_at=now)
        return True

    async def link_failures(self, now: datetime) -> int:
        return self.failures.get(failure_window(now), 0)

    async def count_link_failure(self, now: datetime) -> int:
        window = failure_window(now)
        self.failures[window] = self.failures.get(window, 0) + 1
        return self.failures[window]

    # --- accounts ---------------------------------------------------------------
    async def get_account_info(self, account_id: str) -> AccountInfo:
        return self.accounts.get(account_id, AccountInfo())

    # --- conversations ----------------------------------------------------------
    async def get_conversation(self, account_id: str) -> Conversation:
        if account_id not in self.conversations:
            self.conversations[account_id] = Conversation(
                id=f"conv_{next(self._ids):08x}", account_id=account_id
            )
        return self.conversations[account_id]

    async def save_conversation(self, conversation: Conversation) -> None:
        self.conversations[conversation.account_id] = conversation

    @asynccontextmanager
    async def lock(self, account_id: str) -> AsyncIterator[None]:
        async with self._locks[account_id]:
            yield

    async def accounts_with_open_work(self) -> Sequence[str]:
        return sorted(
            {
                p.account_id
                for p in self.proposals.values()
                if not p.is_terminal and p.account_id not in self.leaves
            }
        )

    # --- messages ---------------------------------------------------------------
    async def record_inbound(self, channel: str, event_id: str, now: datetime) -> bool:
        if (channel, event_id) in self._seen:
            return False
        self._seen.add((channel, event_id))
        return True

    async def record_message(self, record: MessageRecord) -> None:
        self.messages.append(record)

    # --- proposals --------------------------------------------------------------
    async def add_proposal(self, proposal: Proposal) -> bool:
        if await self.get_proposal_by_source(
            proposal.account_id, proposal.type, proposal.source_ref
        ):
            return False
        self.proposals[proposal.id] = proposal
        return True

    async def get_proposal(self, proposal_id: str) -> Proposal | None:
        return self.proposals.get(proposal_id)

    async def save_proposal(self, proposal: Proposal) -> None:
        self.proposals[proposal.id] = proposal

    async def list_unfinished_proposals(self, account_id: str) -> Sequence[Proposal]:
        return [
            p for p in self.proposals.values() if p.account_id == account_id and not p.is_terminal
        ]

    async def get_proposal_by_source(
        self, account_id: str, proposal_type: str, source_ref: str
    ) -> Proposal | None:
        for p in self.proposals.values():
            if (p.account_id, p.type, p.source_ref) == (account_id, proposal_type, source_ref):
                return p
        return None

    async def get_proposal_by_action(self, action_id: str) -> Proposal | None:
        return next((p for p in self.proposals.values() if p.action_id == action_id), None)

    async def count_capped_sent_since(self, account_id: str, since: datetime) -> int:
        return sum(
            1
            for p in self.proposals.values()
            if p.account_id == account_id
            and counts_towards_weekly_cap(p.type)
            and p.sent_at is not None
            and p.sent_at >= since
        )
