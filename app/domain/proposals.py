"""Proposals: the life cycle of everything the agent takes the initiative on. No I/O.

A proposal is one question to the owner (a new review with a suggested reply, a
holiday, a commercial date) that may end in one action on Google. The states and the
transitions are the ones of `docs/01-flujos.md`, section 4; the few additions are
listed in `EXTRA_TRANSITIONS` and explained in `docs/canales.md`.

The state machine is a pure function: `transition(state, event)` returns the next
state or raises `InvalidTransition`. Nothing else in the code base changes a state.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class ProposalState(StrEnum):
    SCHEDULED = "programada"
    SENT = "enviada"
    REMINDED = "recordada"
    COLLECTING = "recopilando"
    TO_CONFIRM = "por_confirmar"
    EXECUTING = "ejecutando"
    APPLIED = "aplicada"
    EXPIRED = "caducada"
    DISMISSED = "descartada"
    FAILED = "fallida"
    CANCELLED = "cancelada"


class ProposalEvent(StrEnum):
    SEND = "send"  # its moment arrives
    CANCEL = "cancel"  # no longer relevant
    OWNER_REPLIED = "owner_replied"
    REMIND = "remind"  # no answer within the reminder delay
    EXPIRE = "expire"  # no answer
    CLARIFY = "clarify"
    DATA_COMPLETE = "data_complete"
    DECLINE = "decline"  # the owner says no
    CONFIRM = "confirm"
    REQUEST_CHANGES = "request_changes"
    APPLIED = "applied"  # Google accepts
    FAILED = "failed"  # definitive error
    REOPEN = "reopen"  # the owner answers an expired proposal that is still valid


class ProposalType(StrEnum):
    REVIEW_REPLY = "review_reply"
    KEY_DATE = "key_date"
    OWNER_ORDER = "owner_order"  # "hoy cierro": in the model, without a flow yet


S, E = ProposalState, ProposalEvent

# The diagram of docs/01-flujos.md, section 4, arrow by arrow.
DIAGRAM_TRANSITIONS: Mapping[tuple[ProposalState, ProposalEvent], ProposalState] = {
    (S.SCHEDULED, E.SEND): S.SENT,
    (S.SCHEDULED, E.CANCEL): S.CANCELLED,
    (S.SENT, E.OWNER_REPLIED): S.COLLECTING,
    (S.SENT, E.REMIND): S.REMINDED,
    (S.REMINDED, E.OWNER_REPLIED): S.COLLECTING,
    (S.REMINDED, E.EXPIRE): S.EXPIRED,
    (S.COLLECTING, E.CLARIFY): S.COLLECTING,
    (S.COLLECTING, E.DATA_COMPLETE): S.TO_CONFIRM,
    (S.COLLECTING, E.DECLINE): S.DISMISSED,
    (S.TO_CONFIRM, E.CONFIRM): S.EXECUTING,
    (S.TO_CONFIRM, E.REQUEST_CHANGES): S.COLLECTING,
    (S.TO_CONFIRM, E.DECLINE): S.DISMISSED,
    (S.EXECUTING, E.APPLIED): S.APPLIED,
    (S.EXECUTING, E.FAILED): S.FAILED,
}

# Not in the diagram, needed so that no proposal can hold a conversation for ever:
# - a proposal whose reminder no longer fits (sent late) or that the owner left half
#   answered expires all the same;
# - an expired proposal that is still valid goes back to the queue when the owner
#   answers it (docs/fase1/guion-conversacional.md, flow 9).
EXTRA_TRANSITIONS: Mapping[tuple[ProposalState, ProposalEvent], ProposalState] = {
    (S.SENT, E.EXPIRE): S.EXPIRED,
    (S.COLLECTING, E.EXPIRE): S.EXPIRED,
    (S.TO_CONFIRM, E.EXPIRE): S.EXPIRED,
    (S.EXPIRED, E.REOPEN): S.SCHEDULED,
}

TRANSITIONS: Mapping[tuple[ProposalState, ProposalEvent], ProposalState] = MappingProxyType(
    {**DIAGRAM_TRANSITIONS, **EXTRA_TRANSITIONS}
)

TERMINAL_STATES = frozenset({S.APPLIED, S.EXPIRED, S.DISMISSED, S.FAILED, S.CANCELLED})
# A leave of the profile or of the account was confirmed (B12 of
# docs/seguridad/revision-2026-11-12-b.md): what had not been confirmed yet is cancelled
# with this reason in the same write. A proposal cancelled so is offered again if it is
# submitted after the leave was restored. One that is executing keeps its course: its
# action already left, and its result is recorded without telling anybody.
OUTCOME_OFFBOARDING = "offboarding"
CANCELLED_BY_LEAVE = (S.SCHEDULED, S.SENT, S.REMINDED, S.COLLECTING, S.TO_CONFIRM)
# Multinegocio (docs/multinegocio.md, 5.3 and 6.5): the profile of the proposal stopped
# being active (the owner deactivated it, or it was removed). The same states as with a
# leave are cancelled, with this reason and without telling anybody. The database lets
# the API write it (policy proposal_api_update).
OUTCOME_LOCATION_INACTIVE = "location_inactive"
CANCELLED_BY_DEACTIVATION = CANCELLED_BY_LEAVE
# Shown to the owner and still waiting for them: the only states in which a button or
# a text of the owner can move the proposal.
OPEN_STATES = frozenset({S.SENT, S.REMINDED, S.COLLECTING, S.TO_CONFIRM})


class InvalidTransition(ValueError):
    def __init__(self, state: ProposalState, event: ProposalEvent) -> None:
        super().__init__(f"transición no válida: {state.value} + {event.value}")
        self.state = state
        self.event = event


def transition(state: ProposalState, event: ProposalEvent) -> ProposalState:
    """The next state, or InvalidTransition. The only way a state changes."""
    try:
        return TRANSITIONS[(state, event)]
    except KeyError:
        raise InvalidTransition(state, event) from None


def advance(state: ProposalState, *events: ProposalEvent) -> ProposalState:
    """Several transitions in a row; all of them must be valid.

    A one tap confirmation ("Publicar", "Cierro") walks the whole diagram:
    owner_replied, data_complete, confirm. It skips no state.
    """
    for event in events:
        state = transition(state, event)
    return state


def can(state: ProposalState, event: ProposalEvent) -> bool:
    return (state, event) in TRANSITIONS


def is_terminal(state: ProposalState) -> bool:
    return state in TERMINAL_STATES


# --- the proposal ------------------------------------------------------------------

LOW_STARS = 2  # 1 and 2 stars: jump the queue and are never answered without the owner

PRIORITY_LOW_REVIEW = 100
PRIORITY_REVIEW = 50
PRIORITY_OWNER_ORDER = 30
PRIORITY_KEY_DATE = 10


def review_priority(stars: int) -> int:
    return PRIORITY_LOW_REVIEW if stars <= LOW_STARS else PRIORITY_REVIEW


def jumps_queue(proposal_type: ProposalType, priority: int) -> bool:
    """Only a review of 1 or 2 stars interrupts the proposal the owner has open."""
    return proposal_type is ProposalType.REVIEW_REPLY and priority >= PRIORITY_LOW_REVIEW


def counts_towards_weekly_cap(proposal_type: ProposalType) -> bool:
    """Reviews and what the owner orders are not "proactive messages": they do not
    count towards the weekly cap (docs/04-ux-conversacional.md, section 3)."""
    return proposal_type is ProposalType.KEY_DATE


_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class Proposal:
    """One proposal. `payload` is what it was created with and never changes;
    `slots` is what the conversation collects (the owner's text, the hours)."""

    id: str
    account_id: str
    type: ProposalType
    source_ref: str  # the review id, or the key of the key date: one proposal each
    state: ProposalState
    priority: int
    created_at: datetime
    scheduled_for: datetime
    payload: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)
    slots: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)
    step: str = ""
    # Changes with every step: a button of an older message of this proposal carries
    # an old token and is refused.
    step_token: str = ""
    # identity id -> the channel's id of the last message with buttons sent to it
    message_refs: Mapping[str, str] = field(default_factory=lambda: _EMPTY)
    sent_at: datetime | None = None
    remind_at: datetime | None = None
    reminder_sent_at: datetime | None = None
    expires_at: datetime | None = None
    parked_at: datetime | None = None  # interrupted by a review of 1 or 2 stars
    # The registered confirmation of the owner: when, with which inbound event and
    # the exact text or change confirmed. No action exists without these three.
    confirmed_at: datetime | None = None
    confirmed_event_id: str | None = None
    confirmed_summary: str | None = None
    action_id: str | None = None
    outcome_reason: str | None = None

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


def idempotency_key(proposal_id: str, action_type: str, ordinal: int = 1) -> str:
    """`prop_8f2c:create_post:1` (docs/03-lld.md): the same confirmation asked twice
    is the same action."""
    return f"{proposal_id}:{action_type}:{ordinal}"
