"""What the conversation keeps: linked addresses, link codes, the thread of each
account, the messages and the proposals. Tables in `db/schema_channels.sql`."""

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.domain.conversation import AccountInfo, ChannelIdentity, Conversation, LinkOutcome
from app.domain.link_codes import LinkCode, SenderState
from app.domain.proposals import Proposal


@dataclass(frozen=True)
class LinkResult:
    identity: ChannelIdentity
    outcome: LinkOutcome
    previous_account_id: str | None = None  # MOVED: the account it was unlinked from


@dataclass(frozen=True)
class MessageRecord:
    conversation_id: str | None
    identity_id: str | None
    direction: str  # "in" | "out"
    kind: str
    body: str
    at: datetime
    channel: str = ""
    channel_message_id: str = ""
    key: str = ""
    proposal_id: str | None = None


class ConversationStore(Protocol):
    # --- linked addresses -------------------------------------------------------
    async def get_identity(self, channel: str, address: str) -> ChannelIdentity | None:
        """The ACTIVE link of that address, or None. An unlinked address is unknown."""
        ...

    async def list_identities(self, account_id: str) -> Sequence[ChannelIdentity]:
        """The active addresses of an account, the oldest link first."""
        ...

    async def link_identity(
        self, account_id: str, channel: str, address: str, now: datetime
    ) -> LinkResult:
        """Link the address to the account. If it was another account's, that link
        ends in the same step: an address belongs to one account at a time."""
        ...

    async def unlink_identity(self, identity_id: str, now: datetime) -> None:
        """From the panel. From this moment nothing is sent to it nor attended."""
        ...

    async def touch_inbound(self, identity_id: str, now: datetime) -> None: ...

    # --- link codes -------------------------------------------------------------
    async def save_link_code(self, code: LinkCode) -> None:
        """Store a new code. The account's earlier unused codes stop being valid."""
        ...

    async def get_link_code(self, code_hash: str) -> LinkCode | None:
        """The code whose six digits OR whose token have that hash."""
        ...

    async def consume_link_code(self, code_hash: str, now: datetime) -> str | None:
        """Mark the code as used and return its account, in one atomic step. None if
        it does not exist, has expired or was already used. `code_hash` is the hash of
        the six digits or of the token: either spends the code."""
        ...

    async def get_sender_state(self, address_hash: str) -> SenderState: ...

    async def save_sender_state(self, address_hash: str, state: SenderState) -> None: ...

    async def take_link_attempt(self, address_hash: str, now: datetime) -> SenderState | None:
        """Count one more code of that address before it is looked at, in one atomic
        step (`link_codes.register_attempt`). None if the address is blocked."""
        ...

    async def clear_link_attempts(self, address_hash: str) -> None:
        """A right code: the count of that address starts again."""
        ...

    async def claim_unlinked_reply(self, address_hash: str, now: datetime) -> bool:
        """Whether an address that is not linked may get the generic answer now (once
        every 24 hours, never while blocked), recording it in the same atomic step."""
        ...

    async def link_failures(self, now: datetime) -> int:
        """Wrong codes of every address together in the current window."""
        ...

    async def count_link_failure(self, now: datetime) -> int:
        """One more wrong code in the current window, atomically. Returns the count."""
        ...

    # --- accounts ---------------------------------------------------------------
    async def get_account_info(self, account_id: str) -> AccountInfo: ...

    # --- conversations ----------------------------------------------------------
    async def get_conversation(self, account_id: str) -> Conversation:
        """The thread of the account; created the first time."""
        ...

    async def save_conversation(self, conversation: Conversation) -> None: ...

    def lock(self, account_id: str) -> AbstractAsyncContextManager[None]:
        """Exclusive access to the thread of an account: two presses of the same
        button are handled one after the other, never at once."""
        ...

    async def accounts_with_open_work(self) -> Sequence[str]:
        """Accounts with any proposal that is not finished."""
        ...

    # --- messages ---------------------------------------------------------------
    async def record_inbound(self, channel: str, event_id: str, now: datetime) -> bool:
        """False if that event of that channel was already seen: it is not handled twice."""
        ...

    async def record_message(self, record: MessageRecord) -> None: ...

    # --- proposals --------------------------------------------------------------
    async def add_proposal(self, proposal: Proposal) -> bool:
        """False if the account already has a proposal for that `source_ref` and type."""
        ...

    async def get_proposal(self, proposal_id: str) -> Proposal | None: ...

    async def save_proposal(self, proposal: Proposal) -> None: ...

    async def list_unfinished_proposals(self, account_id: str) -> Sequence[Proposal]: ...

    async def get_proposal_by_source(
        self, account_id: str, proposal_type: str, source_ref: str
    ) -> Proposal | None: ...

    async def get_proposal_by_action(self, action_id: str) -> Proposal | None:
        """The proposal whose confirmation created that action, if any."""
        ...

    async def count_capped_sent_since(self, account_id: str, since: datetime) -> int:
        """Proposals that count towards the weekly cap, first sent after `since`."""
        ...


@dataclass(frozen=True)
class ReviewTarget:
    """A stored review and the accounts it may be proposed to by the channels."""

    stars: int | None
    text: str  # written by a third party: data, only ever shown
    created_at: datetime | None
    unanswered: bool
    live: bool  # still returned by Google
    accounts: tuple[str, ...]  # selected profile with it, linked before it was written


class ReviewTargets(Protocol):
    async def review_targets(self, review_id: str) -> ReviewTarget | None: ...


class LinkedProfiles(Protocol):
    async def linked_profiles(self) -> Sequence[tuple[str, str]]:
        """(google_sub, Google location) of the selected profiles of the accounts with a
        linked address: what the planner keeps in sync."""
        ...
