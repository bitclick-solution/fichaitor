"""Multinegocio, pure rules of the conversation (docs/multinegocio.md, 6.2 to 6.4).

Which proposal a free text is about when several are open, what an address limited to
some profiles may see and do, and the header with the name of the business.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.domain import channel_messages as cm
from app.domain import conversation as rules
from app.domain import proposals
from app.domain.conversation import (
    ChannelIdentity,
    Focus,
    TargetKind,
    TargetReason,
    TextCandidate,
    resolve_text_target,
)

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
A, B, C, D = (f"prop_{n:016x}" for n in (0xA, 0xB, 0xC, 0xD))


def identity(location_filter=None, account="acc-1", unlinked=None) -> ChannelIdentity:
    return ChannelIdentity(
        id="chid_0011223344556677",
        account_id=account,
        channel="telegram",
        address="123456",
        verified_at=NOW,
        unlinked_at=unlinked,
        location_filter=location_filter,
    )


# --- who sees what -------------------------------------------------------------------


def test_an_address_without_filter_sees_every_profile():
    assert rules.sees(identity(), "loc_a")
    assert rules.sees(identity(), None)


def test_a_filtered_address_sees_only_its_profiles():
    manager = identity(("loc_b",))
    assert rules.sees(manager, "loc_b")
    assert not rules.sees(manager, "loc_a")
    assert not rules.sees(manager, None)  # a proposal that names no profile


def test_a_filtered_chat_does_not_act_on_another_profile():
    manager = identity(("loc_b",))
    assert rules.may_act(manager, "acc-1", "loc_b")
    assert not rules.may_act(manager, "acc-1", "loc_a")
    # Not even knowing the id of a proposal of another account, nor once unlinked.
    assert not rules.may_act(manager, "acc-2", "loc_b")
    assert not rules.may_act(identity(("loc_b",), unlinked=NOW), "acc-1", "loc_b")


def test_may_act_without_a_profile_is_as_today_for_an_address_without_filter():
    assert rules.may_act(identity(), "acc-1")
    assert not rules.may_act(identity(), "acc-2")
    assert not rules.may_act(None, "acc-1")


# --- which proposal a text is about: the five criteria, in order -----------------------


def cand(pid: str, ref: str = "", *, any_text: bool = False, hours: bool = False):
    return TextCandidate(pid, message_ref=ref, any_text=any_text, hours=hours)


def test_without_candidates_it_is_as_without_proposals():
    assert resolve_text_target("hola", [], "", None, NOW).kind is TargetKind.NONE


def test_1_quoting_a_message_wins_over_everything_else():
    candidates = [cand(A, "m1", hours=True), cand(B, "m2", hours=True)]
    target = resolve_text_target("de 10 a 14", candidates, "m2", Focus(A, NOW), NOW)
    assert (target.kind, target.proposal_id, target.reason) == (
        TargetKind.PROPOSAL,
        B,
        TargetReason.REPLY_TO,
    )


def test_1_quoting_an_unknown_message_falls_to_the_next_criteria():
    target = resolve_text_target("vale", [cand(A, "m1")], "m9", None, NOW)
    assert target.reason is TargetReason.SINGLE


def test_2_a_single_candidate_is_the_one():
    target = resolve_text_target("sí", [cand(A)], "", None, NOW)
    assert (target.proposal_id, target.reason) == (A, TargetReason.SINGLE)


def test_2_a_single_candidate_receives_even_a_courtesy_as_today():
    # What the step does with "gracias" is the step's business (today: nothing, or the
    # reply after "Editar").
    target = resolve_text_target("gracias", [cand(A, any_text=True)], "", None, NOW)
    assert target.proposal_id == A


def test_3_a_recent_focus_decides():
    candidates = [cand(A, any_text=True), cand(B, hours=True)]
    focus = Focus(A, NOW - timedelta(minutes=29))
    target = resolve_text_target("Gracias por venir, os esperamos", candidates, "", focus, NOW)
    assert (target.proposal_id, target.reason) == (A, TargetReason.FOCUS)


def test_3_an_old_focus_or_one_that_is_no_longer_a_candidate_does_not():
    candidates = [cand(A, hours=True), cand(B, hours=True)]
    old = Focus(A, NOW - rules.FOCUS_TTL)
    assert resolve_text_target("vale", candidates, "", old, NOW).kind is TargetKind.WHICH
    gone = Focus(C, NOW)
    assert resolve_text_target("vale", candidates, "", gone, NOW).kind is TargetKind.WHICH


def test_4_hours_go_to_the_only_candidate_that_takes_hours():
    candidates = [cand(A), cand(B, hours=True)]
    target = resolve_text_target("de 10 a 14", candidates, "", None, NOW)
    assert (target.proposal_id, target.reason) == (B, TargetReason.ANSWER_TYPE)


def test_4_hours_with_two_candidates_that_take_hours_are_asked():
    candidates = [cand(A, hours=True), cand(B, hours=True)]
    target = resolve_text_target("de 10 a 14", candidates, "", None, NOW)
    assert target.kind is TargetKind.WHICH and set(target.choices) == {A, B}


def test_4_a_step_that_takes_any_text_always_counts_so_nothing_is_decided():
    candidates = [cand(A, any_text=True), cand(B, hours=True)]
    assert resolve_text_target("de 10 a 14", candidates, "", None, NOW).kind is TargetKind.WHICH


def test_5_an_ambiguous_yes_or_no_confirms_nothing():
    candidates = [cand(A, hours=True), cand(B)]
    for text in ("sí", "Vale", "ok", "no", "déjalo"):
        target = resolve_text_target(text, candidates, "", None, NOW)
        assert target.kind is TargetKind.WHICH
        assert target.proposal_id is None


def test_5_a_courtesy_is_never_answered_when_it_is_ambiguous():
    candidates = [cand(A), cand(B)]
    for text in ("hola", "Gracias", "buenas tardes"):
        assert resolve_text_target(text, candidates, "", None, NOW).kind is TargetKind.IGNORE


def test_5_more_than_three_candidates_ask_to_quote_the_message():
    candidates = [cand(p) for p in (A, B, C, D)]
    target = resolve_text_target("lo de antes", candidates, "", None, NOW)
    assert target.kind is TargetKind.QUOTE
    three = resolve_text_target("lo de antes", candidates[:3], "", None, NOW)
    assert three.kind is TargetKind.WHICH and three.choices == (A, B, C)


# --- the header ----------------------------------------------------------------------


def test_the_header_is_the_name_of_the_business_on_one_line():
    label = cm.business_label("Bar\nLa Parra")
    assert label == "Bar La Parra"
    assert cm.with_header(label, "Nueva reseña").startswith("Bar La Parra\nNueva reseña")


def test_two_profiles_with_the_same_name_get_their_town():
    label = cm.business_label(
        "Bar La Parra",
        same_title_elsewhere=True,
        address="Calle Mayor 3, 28901 Getafe, Madrid",
        location_id="loc_3f2a9b1c0d4e",
    )
    assert label == "Bar La Parra · Getafe"


def test_without_address_the_end_of_the_public_id_tells_them_apart():
    label = cm.business_label("Bar La Parra", same_title_elsewhere=True, location_id="loc_3f2a9b1c")
    assert label == "Bar La Parra · 9b1c"


@pytest.mark.parametrize(
    ("address", "town"),
    [
        ("Calle Mayor 3, 28901 Getafe, Madrid", "Getafe"),
        ("Av. de la Constitución 1, 41001 Sevilla", "Sevilla"),
        ("Plaza Nueva", ""),
        (None, ""),
    ],
)
def test_locality(address, town):
    assert cm.locality(address) == town


def test_a_long_name_never_exceeds_the_header_reserve():
    label = cm.business_label(
        "x" * 500, same_title_elsewhere=True, address="C/ A, 28901 " + "y" * 100
    )
    assert len(cm.render("fmt.business", negocio=label)) + 1 <= cm.HEADER_RESERVE
    assert len(cm.business_label("x" * 500)) + 1 <= cm.HEADER_RESERVE


def test_the_longest_message_with_header_and_prefix_fits_in_whatsapp():
    header = cm.business_label("N" * 500, same_title_elsewhere=True, address="1, 28901 " + "G" * 99)
    reply = "r" * cm.SHOWN_REPLY_MAX
    key = "review.new.negative"  # the longest of the review messages
    fixed = len(cm.render(key, estrellas=cm.stars_text(1), extracto="", respuesta=reply))
    room = cm.review_excerpt_room(fixed, header)
    assert room >= cm.REVIEW_EXCERPT_MIN  # the excerpt still says something
    body = cm.render(
        key, estrellas=cm.stars_text(1), extracto=cm.quoted("t" * 5000, room), respuesta=reply
    )
    prefix = "p" * (cm.PREFIX_RESERVE - 1) + "\n"
    text = prefix + cm.with_header(header, body)
    assert len(text) <= cm.WHATSAPP_LIMITS.max_body
    cm.validate(cm.OutboundMessage(to="chid_00", kind=cm.MessageKind.PROPOSAL, text=text))


def test_without_header_the_room_is_as_before():
    assert cm.review_excerpt_room(100) == min(
        cm.REVIEW_EXCERPT_MAX, cm.CORE_LIMITS.max_body - cm.PREFIX_RESERVE - 100
    )


# --- which business, welcome and the inbound event -----------------------------------


def test_which_business_has_its_text_and_unique_button_titles():
    assert cm.render("text.which") == "¿De qué negocio me hablas?"
    assert cm.render("text.quote")
    assert cm.render("text.write_again")
    titles = cm.choice_titles(["Bar La Parra · Getafe", "Bar La Parra · Madrid", "Otro"])
    assert len(set(titles)) == 3
    assert all(len(t) <= cm.CORE_LIMITS.max_button_title for t in titles)


def test_welcome_names_the_businesses_that_chat_will_see():
    many = cm.render(
        "welcome.linked_many",
        nombre="",
        negocios=cm.businesses_phrase(["A", "B", "C"], filtered=False),
    )
    assert "las fichas de tus 3 negocios" in many
    listed = cm.businesses_phrase(["Bar A", "Bar B"], filtered=True)
    assert listed == "Bar A y Bar B"
    assert cm.businesses_phrase(["Bar A", "Bar B", "Bar C"], filtered=True) == (
        "Bar A, Bar B y Bar C"
    )


def test_an_inbound_text_may_quote_a_message():
    event = cm.InboundEvent(
        channel="telegram",
        address="1",
        event_id="2",
        kind=cm.InboundKind.TEXT,
        text="ok",
        reply_to="345",
    )
    assert event.reply_to == "345"
    assert cm.InboundEvent("telegram", "1", "3", cm.InboundKind.TEXT).reply_to == ""


def test_the_outcome_of_a_deactivated_profile():
    assert proposals.OUTCOME_LOCATION_INACTIVE == "location_inactive"
    assert proposals.ProposalState.SENT in proposals.CANCELLED_BY_DEACTIVATION
    assert proposals.ProposalState.EXECUTING not in proposals.CANCELLED_BY_DEACTIVATION
