"""What the web session does with the messaging channels (docs/canales.md): ask for a
link code, list the linked addresses and unlink one. Implemented by the API's stores
(`PostgresStore`, `MemoryStore`). Linking itself is the conversation's: it needs the
code sent from the chat."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from app.domain.conversation import ChannelIdentity
from app.domain.link_codes import LinkCode


class LinkCodeTaken(Exception):
    """The hash of the new code is already stored (six digits are a million values):
    the caller draws another code."""


class ChannelLinks(Protocol):
    async def save_link_code(self, code: LinkCode) -> None:
        """Store a new code, with its filter, and retire every unused one of the account,
        in one step. Raises LinkCodeTaken."""
        ...

    async def list_channels(self, account_id: str) -> Sequence[ChannelIdentity]:
        """The active addresses of that account, the oldest link first, each with its
        filter of profiles."""
        ...

    async def set_channel_filter(
        self, account_id: str, identity_id: str, location_filter: tuple[str, ...] | None
    ) -> ChannelIdentity | None:
        """Change which profiles an active address of that account sees (None: all of
        them). The caller has checked that each one is an active profile of the account;
        the database checks again that it is of the account (location_filter_guard).
        None, and nothing changes, if the address is unknown, unlinked or of another
        account, or a profile is not of the account."""
        ...

    async def unlink_channel(self, account_id: str, identity_id: str, now: datetime) -> bool:
        """Unlink one active address of that account. False, and nothing changes, if it
        is unknown, already unlinked or of another account."""
        ...
