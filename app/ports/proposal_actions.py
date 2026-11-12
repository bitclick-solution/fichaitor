"""Where a confirmed proposal becomes an action on Google.

The conversation never writes to Google and never runs anything: when the owner
confirms, it hands over an `ActionRequest` and somebody else (the executor of the
worker) carries it out. This port is the only door, and it is not wired to the
existing actions yet: `docs/canales.md` says how `reply_review` connects to
`Store.create_reply_action`.

An implementation must:
- create at most one action per `idempotency_key`, and return the first one again
  when asked twice;
- check again that `source_ref` belongs to `account_id`: the conversation already
  did, this is the second barrier;
- send exactly `payload`: nothing is trimmed or corrected after the owner confirmed.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

REJECT_TARGET_GONE = "target_gone"  # the review no longer exists
REJECT_INVALID = "invalid"  # Google would not take this payload: the owner changes it
REJECT_RECONNECT = "reconnect"  # the access to Google is gone
REJECT_UNAVAILABLE = "unavailable"  # nothing was created: worth asking again


class ActionRejected(Exception):
    """No action was created. `code` is one of the REJECT_ constants."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ActionRequest:
    account_id: str
    proposal_id: str
    type: str  # "reply_review" | "set_special_hours"
    source_ref: str
    payload: Mapping[str, Any]
    idempotency_key: str
    # The registered confirmation of the owner. A request without it is a bug.
    confirmed_at: datetime
    confirmed_channel: str
    confirmed_event_id: str
    # set_special_hours: the profile ("loc_...") the key date was proposed for. The
    # implementation checks that it is still the selected one of the account.
    location_id: str = ""


@dataclass(frozen=True)
class ActionReceipt:
    action_id: str
    created: bool  # False: the same confirmation had already created it


class ProposalActions(Protocol):
    async def create(self, request: ActionRequest) -> ActionReceipt:
        """Record the action as pending for the executor. Raises ActionRejected."""
        ...


@dataclass
class InMemoryProposalActions:
    """The double: keeps what was asked, creates each action once."""

    requests: list[ActionRequest] = field(default_factory=list)
    reject_with: str | None = None  # set to a REJECT_ code to refuse the next ones
    _by_key: dict[str, str] = field(default_factory=dict)

    async def create(self, request: ActionRequest) -> ActionReceipt:
        if not request.confirmed_event_id or request.confirmed_at is None:
            raise AssertionError("acción sin confirmación registrada")
        if self.reject_with is not None:
            raise ActionRejected(self.reject_with)
        existing = self._by_key.get(request.idempotency_key)
        if existing is not None:
            return ActionReceipt(existing, created=False)
        action_id = f"act_{len(self._by_key) + 1:08x}"
        self._by_key[request.idempotency_key] = action_id
        self.requests.append(request)
        return ActionReceipt(action_id, created=True)
