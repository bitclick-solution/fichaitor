"""What the key dates need from the store: the change of hours of one day confirmed by
the owner (by the channel or in the panel), and the panel's view of the proposals.

Implemented by `PostgresStore` (the API's role, and the conversation's for the action)
and by `MemoryStore`. Every method works on the active profiles of the account it is
given and on nothing else (docs/multinegocio.md, 5.4).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol

from app.domain.proposals import ProposalState
from app.domain.special_hours import SpecialHoursChange
from app.ports.store import StoredAction


class SpecialHoursInProgress(Exception):
    """That day of that profile already has a change of hours on its way to Google."""


class KeyDateNotFound(Exception):
    """No key date with that id for that account."""


@dataclass(frozen=True)
class KeyDateView:
    """A key date proposal as the panel shows it."""

    id: str  # "prop_..."
    day: date
    name: str
    kind: str  # "holiday" | "season" | "commercial"
    state: ProposalState
    location_id: str
    expires_at: datetime | None
    # What the owner confirmed ({"kind", "periods"}), once they did.
    confirmed: Mapping[str, Any] = field(default_factory=dict)
    action: StoredAction | None = None


class KeyDateStore(Protocol):
    async def create_special_hours_action(
        self, account_id: str, location_id: str, change: SpecialHoursChange, now: datetime
    ) -> str | None:
        """The owner confirmed `change` for that profile: create, all or nothing, the
        pending action (its text is `change.to_text()`, which nobody can change after),
        its `special_hours_confirmed` audit row and the `set_special_hours` job.

        None when that profile is not, now, an active one of that account (or the
        account has a scheduled leave). Raises SpecialHoursInProgress when that day of
        that profile already has a change on its way.
        """
        ...

    async def list_key_dates(
        self, account_id: str, today: date, location_id: str | None = None
    ) -> Sequence[KeyDateView]:
        """The key date proposals of that active profile of that account (None: the
        preference) whose day is `today` or later, earliest first, each with its action
        if there is one. Empty if it is not an active profile of that account."""
        ...

    async def get_key_date(self, account_id: str, proposal_id: str) -> KeyDateView | None:
        """A key date proposal of an active profile of that account. None otherwise."""
        ...

    async def answer_key_date(
        self,
        account_id: str,
        proposal_id: str,
        change: SpecialHoursChange,
        now: datetime,
        today: date,
    ) -> str:
        """The owner answers a key date in the panel. In one transaction, holding the
        thread of the conversation (so that the chat cannot move it at the same time):
        the proposal walks to `ejecutando` with the confirmation registered, the chat
        lets it go, and the action is created as in create_special_hours_action.
        Returns the action id.

        Raises KeyDateNotFound, PanelRefused (app/domain/key_dates.py: resolved, its day
        has passed, or its profile is no longer active) or
        SpecialHoursInProgress.
        """
        ...

    async def dismiss_key_date(self, account_id: str, proposal_id: str, now: datetime) -> None:
        """The owner says no in the panel. Raises KeyDateNotFound or PanelRefused."""
        ...
