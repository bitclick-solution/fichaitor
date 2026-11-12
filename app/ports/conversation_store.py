"""What the conversation keeps: linked addresses, link codes, the thread of each
account, the messages and the proposals. Tables in `db/schema_channels.sql`."""

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from app.domain.conversation import (
    AccountInfo,
    ChannelIdentity,
    Conversation,
    Focus,
    LinkOutcome,
)
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


@dataclass(frozen=True)
class Lane:
    """The lane of one profile in the thread of its account: at most one active
    proposal per profile (docs/multinegocio.md, 6.1). The thread (`Conversation`) is
    still the lock of the account and keeps `unknown_streak`."""

    account_id: str
    location_id: str
    active_proposal_id: str | None = None


class LocationInactive(Exception):
    """A proposal for a profile that is not an active profile of its account (or, from
    the old code, of an account with no selected profile). Nothing was stored."""


class ConversationStore(Protocol):
    # --- the account ------------------------------------------------------------
    async def on_leave(self, account_id: str) -> bool:
        """The account has a leave scheduled (of its profile or of itself): nothing is
        sent to it on the agent's initiative, and no new proposal is made, until the leave
        is restored (B12 of docs/seguridad/revision-2026-11-12-b.md)."""
        ...

    # --- linked addresses -------------------------------------------------------
    async def get_identity(self, channel: str, address: str) -> ChannelIdentity | None:
        """The ACTIVE link of that address, or None. An unlinked address is unknown."""
        ...

    async def list_identities(self, account_id: str) -> Sequence[ChannelIdentity]:
        """The active addresses of an account, the oldest link first."""
        ...

    async def link_identity(
        self,
        account_id: str,
        channel: str,
        address: str,
        now: datetime,
        location_filter: tuple[str, ...] | None = None,
    ) -> LinkResult:
        """Link the address to the account. If it was another account's, that link
        ends in the same step: an address belongs to one account at a time.

        location_filter: the one of the code it was linked with (None: every profile).
        The new address has it from the first moment (docs/multinegocio.md, 3.7)."""
        ...

    async def unlink_identity(self, identity_id: str, now: datetime) -> None:
        """From the panel. From this moment nothing is sent to it nor attended."""
        ...

    async def touch_inbound(self, identity_id: str, now: datetime) -> None: ...

    async def identities_for(self, account_id: str, location_id: str) -> Sequence[ChannelIdentity]:
        """The active addresses of an account that see that profile (`sees`: without a
        filter, or with the profile in it), the oldest link first. None if the profile
        is not of that account."""
        ...

    async def set_focus(self, identity_id: str, proposal_id: str, now: datetime) -> None:
        """That address pressed a button of that proposal (docs/multinegocio.md, 6.3)."""
        ...

    async def get_focus(self, identity_id: str) -> Focus | None:
        """The last proposal on which that address pressed a button, if any."""
        ...

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

    async def location_label(self, account_id: str, location_id: str) -> str:
        """The name of that profile of that account for the header of a message
        (`channel_messages.business_label`): its title, with the town if another active
        profile of the account has the same title. "tu negocio" if it is not found."""
        ...

    async def active_locations(self, account_id: str) -> Sequence[str]:
        """The active profiles ("loc_...") of the account, the oldest activation first.
        Empty for an account deleted or without any."""
        ...

    async def location_active(self, account_id: str, location_id: str) -> bool:
        """Whether that profile is an active profile of that account. When it stopped
        being one, its unfinished proposals are cancelled (`location_inactive`)."""
        ...

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

    # --- lanes (multinegocio) -----------------------------------------------------
    async def get_lane(self, account_id: str, location_id: str) -> Lane:
        """The lane of that profile; an empty one if it has none yet. Reading it does
        not create it."""
        ...

    async def save_lane(self, lane: Lane) -> None:
        """Create or update the lane. Its proposal must be of that account and profile."""
        ...

    async def lanes_with_open_work(self) -> Sequence[tuple[str, str]]:
        """(account, profile) with any proposal that is not finished, of accounts
        without a scheduled leave. Inactive profiles too: their proposals are cancelled
        there (`location_inactive`)."""
        ...

    # --- messages ---------------------------------------------------------------
    async def record_inbound(self, channel: str, event_id: str, now: datetime) -> bool:
        """False if that event of that channel was already seen: it is not handled twice."""
        ...

    async def record_message(self, record: MessageRecord) -> None: ...

    # --- proposals --------------------------------------------------------------
    async def add_proposal(self, proposal: Proposal) -> bool:
        """False if the account already has a proposal for that `source_ref` and type.
        Raises LocationInactive if its profile is not an active profile of its account."""
        ...

    async def get_proposal(self, proposal_id: str) -> Proposal | None: ...

    async def save_proposal(self, proposal: Proposal) -> None: ...

    async def list_unfinished_proposals(
        self, account_id: str, location_id: str | None = None
    ) -> Sequence[Proposal]:
        """Those of the account; with `location_id`, only those of that profile."""
        ...

    async def proposal_by_message_ref(
        self, account_id: str, identity_id: str, message_ref: str
    ) -> Proposal | None:
        """The unfinished proposal of that account whose last message with buttons to
        that address is `message_ref` (a reply quoting it, docs/multinegocio.md, 6.3)."""
        ...

    async def get_proposal_by_source(
        self, account_id: str, proposal_type: str, source_ref: str
    ) -> Proposal | None: ...

    async def get_proposal_by_action(self, action_id: str) -> Proposal | None:
        """The proposal whose confirmation created that action, if any."""
        ...

    async def count_capped_sent_since(
        self, account_id: str, since: datetime, location_id: str | None = None
    ) -> int:
        """Proposals that count towards the weekly cap, first sent after `since`. With
        `location_id`, only those of that profile: the cap is per profile."""
        ...


@dataclass(frozen=True)
class ReviewTarget:
    """A stored review and the accounts it may be proposed to by the channels."""

    stars: int | None
    text: str  # written by a third party: data, only ever shown
    created_at: datetime | None
    unanswered: bool
    live: bool  # still returned by Google
    # Until step 6b: accounts whose selected profile, active, has it, linked before it
    # was written. The database gives their proposal that profile.
    accounts: tuple[str, ...]
    # Multinegocio: (account, profile) of every ACTIVE profile with it, of an account
    # without a scheduled leave, whose last sync is "ok" and with an address that sees
    # that profile linked before the review was written.
    targets: tuple[tuple[str, str], ...] = ()


class ReviewTargets(Protocol):
    async def review_targets(self, review_id: str) -> ReviewTarget | None: ...


class LinkedProfiles(Protocol):
    async def linked_profiles(self) -> Sequence[tuple[str, str]]:
        """(google_sub, Google location) of the active profiles of the accounts without
        a leave that have a linked address that sees them: what the planner keeps in
        sync."""
        ...


@dataclass(frozen=True)
class KeyDateProfile:
    """An active profile Fichaitor asks about key dates for, with what the last sync
    stored of it."""

    account_id: str
    location_id: str  # ours: "loc_..."
    region: str  # "AN", or empty when the sync did not get it
    address: str  # as the panel shows it; its postal code is the fallback for the region
    special_dates: tuple[date, ...]  # days that already have special hours in Google


class KeyDateProfiles(Protocol):
    async def key_date_profiles(self) -> Sequence[KeyDateProfile]:
        """Every active profile of an account that is not deleted nor disconnected,
        without a scheduled leave, whose last sync did not lose the access to Google and
        stored its facts. Linked chat or not: the panel shows them too."""
        ...
