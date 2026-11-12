"""The state machine of a proposal: every valid transition and every invalid one."""

from datetime import UTC, datetime

import pytest

from app.domain.proposals import (
    DIAGRAM_TRANSITIONS,
    EXTRA_TRANSITIONS,
    OPEN_STATES,
    PRIORITY_KEY_DATE,
    TERMINAL_STATES,
    TRANSITIONS,
    InvalidTransition,
    Proposal,
    ProposalEvent,
    ProposalState,
    ProposalType,
    advance,
    can,
    counts_towards_weekly_cap,
    idempotency_key,
    is_terminal,
    jumps_queue,
    review_priority,
    transition,
)

S, E = ProposalState, ProposalEvent

# docs/01-flujos.md, section 4, written out again on purpose: if the table in the
# code drifts from the document, this fails.
DOCUMENTED = {
    ("programada", "send"): "enviada",
    ("programada", "cancel"): "cancelada",
    ("enviada", "owner_replied"): "recopilando",
    ("enviada", "remind"): "recordada",
    ("recordada", "owner_replied"): "recopilando",
    ("recordada", "expire"): "caducada",
    ("recopilando", "clarify"): "recopilando",
    ("recopilando", "data_complete"): "por_confirmar",
    ("recopilando", "decline"): "descartada",
    ("por_confirmar", "confirm"): "ejecutando",
    ("por_confirmar", "request_changes"): "recopilando",
    ("por_confirmar", "decline"): "descartada",
    ("ejecutando", "applied"): "aplicada",
    ("ejecutando", "failed"): "fallida",
}


def test_the_diagram_is_the_documented_one():
    got = {(s.value, e.value): t.value for (s, e), t in DIAGRAM_TRANSITIONS.items()}
    assert got == DOCUMENTED


def test_the_states_are_the_documented_ones():
    assert {s.value for s in S} == {
        "programada",
        "enviada",
        "recopilando",
        "por_confirmar",
        "ejecutando",
        "aplicada",
        "recordada",
        "caducada",
        "descartada",
        "fallida",
        "cancelada",
    }


@pytest.mark.parametrize(("origin", "target"), list(TRANSITIONS.items()))
def test_every_valid_transition(origin, target):
    state, event = origin
    assert transition(state, event) is target
    assert can(state, event)


@pytest.mark.parametrize(
    ("state", "event"), [(s, e) for s in S for e in E if (s, e) not in TRANSITIONS]
)
def test_every_other_transition_is_invalid(state, event):
    assert not can(state, event)
    with pytest.raises(InvalidTransition):
        transition(state, event)


def test_additions_to_the_diagram_are_only_expiry_and_reopening():
    assert set(EXTRA_TRANSITIONS) == {
        (S.SENT, E.EXPIRE),
        (S.COLLECTING, E.EXPIRE),
        (S.TO_CONFIRM, E.EXPIRE),
        (S.EXPIRED, E.REOPEN),
    }


def test_finished_states_have_no_way_out_except_reopening_an_expired_one():
    assert TERMINAL_STATES == {S.APPLIED, S.EXPIRED, S.DISMISSED, S.FAILED, S.CANCELLED}
    exits = {(s, e) for (s, e) in TRANSITIONS if s in TERMINAL_STATES}
    assert exits == {(S.EXPIRED, E.REOPEN)}
    assert all(is_terminal(s) for s in TERMINAL_STATES)


def test_nothing_reaches_executing_without_confirm():
    into_executing = [(s, e) for (s, e), t in TRANSITIONS.items() if t is S.EXECUTING]
    assert into_executing == [(S.TO_CONFIRM, E.CONFIRM)]
    # and "por confirmar" is only reached with the data complete
    into_confirm = [(s, e) for (s, e), t in TRANSITIONS.items() if t is S.TO_CONFIRM]
    assert into_confirm == [(S.COLLECTING, E.DATA_COMPLETE)]


def test_one_tap_confirmation_walks_every_state():
    assert advance(S.SENT, E.OWNER_REPLIED, E.DATA_COMPLETE, E.CONFIRM) is S.EXECUTING
    with pytest.raises(InvalidTransition):
        advance(S.SENT, E.CONFIRM)
    with pytest.raises(InvalidTransition):
        advance(S.SCHEDULED, E.OWNER_REPLIED, E.DATA_COMPLETE, E.CONFIRM)


def test_open_states_are_the_ones_waiting_for_the_owner():
    assert OPEN_STATES == {S.SENT, S.REMINDED, S.COLLECTING, S.TO_CONFIRM}


def test_types_include_the_owner_order_for_later():
    assert {t.value for t in ProposalType} == {"review_reply", "key_date", "owner_order"}


@pytest.mark.parametrize(("stars", "jumps"), [(1, True), (2, True), (3, False), (5, False)])
def test_only_reviews_of_one_or_two_stars_jump_the_queue(stars, jumps):
    assert jumps_queue(ProposalType.REVIEW_REPLY, review_priority(stars)) is jumps
    assert not jumps_queue(ProposalType.KEY_DATE, 1000)


def test_reviews_do_not_count_towards_the_weekly_cap():
    assert counts_towards_weekly_cap(ProposalType.KEY_DATE)
    assert not counts_towards_weekly_cap(ProposalType.REVIEW_REPLY)
    assert not counts_towards_weekly_cap(ProposalType.OWNER_ORDER)


def test_proposal_defaults_and_idempotency_key():
    now = datetime(2026, 11, 12, tzinfo=UTC)
    proposal = Proposal(
        id="prop_0123456789abcdef",
        account_id="acc",
        type=ProposalType.KEY_DATE,
        source_ref="holiday:2026-12-08",
        state=S.SCHEDULED,
        priority=PRIORITY_KEY_DATE,
        created_at=now,
        scheduled_for=now,
    )
    assert not proposal.is_open and not proposal.is_terminal
    assert proposal.confirmed_at is None and proposal.action_id is None
    assert idempotency_key(proposal.id, "set_special_hours") == (
        "prop_0123456789abcdef:set_special_hours:1"
    )
