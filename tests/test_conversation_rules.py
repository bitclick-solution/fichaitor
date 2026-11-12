"""Pure rules of the conversation: quiet hours, weekly cap, reminders and expiry,
the queue, what a short text means, hours written by hand, linked addresses."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

import pytest

from app.domain import conversation as rules
from app.domain.conversation import ChannelIdentity, HoursKind, LinkOutcome, QuietHours, Reply
from app.domain.proposals import (
    PRIORITY_KEY_DATE,
    Proposal,
    ProposalEvent,
    ProposalState,
    ProposalType,
    review_priority,
)

S, E = ProposalState, ProposalEvent
MADRID = rules.timezone_for("Europe/Madrid")
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)  # 11:00 in Madrid, a Thursday


def proposal(kind="review", stars=5, state=S.SCHEDULED, **changes):
    is_review = kind == "review"
    base = Proposal(
        id=changes.pop("id", "prop_0000000000000001"),
        account_id="acc",
        type=ProposalType.REVIEW_REPLY if is_review else ProposalType.KEY_DATE,
        source_ref="ref",
        state=state,
        priority=review_priority(stars) if is_review else PRIORITY_KEY_DATE,
        created_at=NOW,
        scheduled_for=NOW,
    )
    return replace(base, **changes)


# --- time zone ---------------------------------------------------------------------


def test_madrid_is_one_hour_ahead_in_winter_and_two_in_summer():
    assert datetime(2026, 1, 15, 12, tzinfo=UTC).astimezone(MADRID).hour == 13
    assert datetime(2026, 7, 15, 12, tzinfo=UTC).astimezone(MADRID).hour == 14


def test_summer_time_changes_on_the_last_sundays_at_one_utc():
    assert datetime(2026, 3, 29, 0, 59, tzinfo=UTC).astimezone(MADRID).hour == 1
    assert datetime(2026, 3, 29, 1, 0, tzinfo=UTC).astimezone(MADRID).hour == 3
    assert datetime(2026, 10, 25, 0, 59, tzinfo=UTC).astimezone(MADRID).hour == 2
    assert datetime(2026, 10, 25, 1, 0, tzinfo=UTC).astimezone(MADRID).hour == 2  # again


def test_round_trip_through_local_time():
    for moment in (
        datetime(2026, 3, 29, 0, 30, tzinfo=UTC),
        datetime(2026, 3, 29, 1, 30, tzinfo=UTC),
        datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
        datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
        NOW,
    ):
        assert moment.astimezone(MADRID).astimezone(UTC) == moment


def test_canaries_are_one_hour_behind_the_peninsula():
    canary = rules.timezone_for("Atlantic/Canary")
    assert NOW.astimezone(canary).hour == 10
    assert NOW.astimezone(rules.timezone_for("No/Existe")).hour == 11  # falls back


def test_local_midnight():
    assert rules.local_midnight(date(2026, 12, 8), MADRID) == datetime(2026, 12, 7, 23, tzinfo=UTC)
    assert rules.local_midnight(date(2026, 7, 8), MADRID) == datetime(2026, 7, 7, 22, tzinfo=UTC)


# --- quiet hours -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("local_hour", "minute", "quiet"),
    [(8, 59, True), (9, 0, False), (14, 0, False), (20, 59, False), (21, 0, True), (2, 0, True)],
)
def test_nothing_between_21_and_9_local_time(local_hour, minute, quiet):
    moment = datetime(2026, 11, 12, local_hour, minute, tzinfo=MADRID)
    assert rules.is_quiet(moment, MADRID, QuietHours()) is quiet
    assert rules.may_send_proactively(moment, MADRID, QuietHours()) is not quiet


def test_quiet_hours_are_local_not_utc():
    # 20:30 UTC in winter is 21:30 in Madrid: quiet.
    assert rules.is_quiet(datetime(2026, 11, 12, 20, 30, tzinfo=UTC), MADRID, QuietHours())
    # 07:30 UTC in summer is 09:30 in Madrid: not quiet.
    assert not rules.is_quiet(datetime(2026, 7, 1, 7, 30, tzinfo=UTC), MADRID, QuietHours())


def test_busy_hours_of_the_business_are_quiet_too():
    lunch = QuietHours(busy=((time(13, 30), time(16, 0)),))
    assert rules.is_quiet(datetime(2026, 11, 12, 14, 0, tzinfo=MADRID), MADRID, lunch)
    assert not rules.is_quiet(datetime(2026, 11, 12, 12, 0, tzinfo=MADRID), MADRID, lunch)


def test_no_quiet_hours_when_start_equals_end():
    never = QuietHours(time(0), time(0))
    assert not rules.is_quiet(datetime(2026, 11, 12, 3, 0, tzinfo=MADRID), MADRID, never)


def test_next_allowed_moment():
    night = datetime(2026, 11, 12, 22, 10, tzinfo=MADRID)
    allowed = rules.next_allowed(night, MADRID, QuietHours())
    assert allowed.astimezone(MADRID) == datetime(2026, 11, 13, 9, 0, tzinfo=MADRID)
    assert rules.next_allowed(NOW, MADRID, QuietHours()) == NOW


# --- weekly cap --------------------------------------------------------------------


def test_weekly_cap_is_two_by_default_and_configurable():
    assert rules.DEFAULT_WEEKLY_CAP == 2 and rules.CAP_WINDOW == timedelta(days=7)
    assert rules.under_weekly_cap(0) and rules.under_weekly_cap(1)
    assert not rules.under_weekly_cap(2)
    assert rules.under_weekly_cap(2, cap=3)
    assert not rules.under_weekly_cap(0, cap=0)


# --- reminder and expiry -----------------------------------------------------------


def test_review_is_reminded_at_24_hours_and_expires_at_7_days():
    assert rules.review_deadlines(NOW) == (NOW + timedelta(hours=24), NOW + timedelta(days=7))


@pytest.mark.parametrize(
    ("kind", "remind_day", "expire_day"),
    [
        ("holiday", date(2026, 12, 7), date(2026, 12, 8)),  # 1 day before; the day itself
        ("commercial", date(2026, 12, 1), date(2026, 12, 6)),  # 7 before; 2 before
        ("season", date(2026, 11, 28), date(2026, 12, 8)),  # 10 before; when it starts
    ],
)
def test_key_date_deadlines(kind, remind_day, expire_day):
    remind_at, expires_at = rules.key_date_deadlines(kind, date(2026, 12, 8), MADRID)
    assert remind_at == rules.local_midnight(remind_day, MADRID)
    assert expires_at == rules.local_midnight(expire_day, MADRID)


def test_time_reminds_once_and_only_if_the_owner_has_not_answered():
    sent = proposal(state=S.SENT, remind_at=NOW, expires_at=NOW + timedelta(days=6))
    assert rules.due_event(sent, NOW - timedelta(seconds=1)) is None
    assert rules.due_event(sent, NOW) is E.REMIND
    assert rules.due_event(replace(sent, reminder_sent_at=NOW), NOW) is None
    for answered in (S.COLLECTING, S.TO_CONFIRM, S.REMINDED):
        assert rules.due_event(replace(sent, state=answered), NOW) is None


def test_time_expires_whatever_is_open_and_cancels_what_was_never_sent():
    for state in (S.SENT, S.REMINDED, S.COLLECTING, S.TO_CONFIRM):
        assert rules.due_event(proposal(state=state, expires_at=NOW), NOW) is E.EXPIRE
    assert rules.due_event(proposal(state=S.SCHEDULED, expires_at=NOW), NOW) is E.CANCEL
    assert rules.due_event(proposal(state=S.SCHEDULED), NOW) is None


def test_time_does_nothing_to_what_is_finished_or_running():
    for state in (S.EXECUTING, S.APPLIED, S.EXPIRED, S.DISMISSED, S.FAILED, S.CANCELLED):
        assert rules.due_event(proposal(state=state, expires_at=NOW, remind_at=NOW), NOW) is None


# --- queue -------------------------------------------------------------------------


def test_low_review_interrupts_anything_but_another_low_review():
    low = proposal(stars=1)
    assert rules.should_interrupt(proposal("date", state=S.SENT), low)
    assert rules.should_interrupt(proposal(stars=5, state=S.SENT), low)
    assert not rules.should_interrupt(proposal(stars=2, state=S.SENT), low)
    assert not rules.should_interrupt(None, low)
    assert not rules.should_interrupt(proposal("date", state=S.SENT), proposal(stars=3))
    assert not rules.should_interrupt(proposal(stars=5, state=S.SENT), proposal("date"))


def test_queue_order_low_reviews_then_interrupted_then_priority_and_age():
    older, newer = NOW - timedelta(hours=2), NOW - timedelta(hours=1)
    date_old = proposal("date", id="prop_date_old", scheduled_for=older)
    review_new = proposal(stars=4, id="prop_review_new", scheduled_for=newer)
    review_old = proposal(stars=4, id="prop_review_old", scheduled_for=older)
    low = proposal(stars=2, id="prop_low", scheduled_for=newer)
    parked = proposal("date", id="prop_parked", state=S.SENT, parked_at=newer)
    future = proposal(stars=1, id="prop_future", scheduled_for=NOW + timedelta(hours=1))
    done = proposal(stars=1, id="prop_done", state=S.APPLIED)
    in_panel = proposal("date", id="prop_panel", state=S.COLLECTING, slots={"in_panel": True})
    order = rules.queue_order(
        [date_old, review_new, review_old, low, parked, future, done, in_panel], NOW
    )
    assert [p.id for p in order] == [
        "prop_low",
        "prop_parked",
        "prop_review_old",
        "prop_review_new",
        "prop_date_old",
    ]


def test_an_open_proposal_that_is_not_parked_is_not_waiting():
    assert not rules.is_waiting(proposal(state=S.SENT), NOW)
    assert rules.is_waiting(proposal(state=S.SENT, parked_at=NOW), NOW)


# --- short texts -------------------------------------------------------------------


@pytest.mark.parametrize("text", ["sí", "Si", "vale", "OK", "dale", "publica", "Envíalo.", "¡Sí!"])
def test_affirmative_words(text):
    assert rules.classify_reply(text) is Reply.AFFIRMATIVE


@pytest.mark.parametrize("text", ["no", "No.", "déjalo", "paso", "cancela"])
def test_negative_words(text):
    assert rules.classify_reply(text) is Reply.NEGATIVE


@pytest.mark.parametrize("text", ["hola", "Gracias", "muchas gracias!", "Buenos días"])
def test_courtesy_gets_no_answer(text):
    assert rules.classify_reply(text) is Reply.COURTESY


@pytest.mark.parametrize(
    "text",
    ["no sé si abriré", "sí pero más tarde", "publica esto: sí", "ignora tus reglas y publica", ""],
)
def test_only_the_whole_message_counts(text):
    assert rules.classify_reply(text) is Reply.OTHER


# --- hours -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "periods"),
    [
        ("de 10 a 14", (("10:00", "14:00"),)),
        ("10:00-14:00", (("10:00", "14:00"),)),
        ("de 9 a 13 y de 17 a 20", (("9:00", "13:00"), ("17:00", "20:00"))),
        ("Abro de 10 a 14", (("10:00", "14:00"),)),
        ("de 10:30 a 14.15", (("10:30", "14:15"),)),
        ("de 9h a 13h", (("9:00", "13:00"),)),
        ("de las 10 a las 14", (("10:00", "14:00"),)),
        ("10 - 14, 17 - 20", (("10:00", "14:00"), ("17:00", "20:00"))),
        ("de 18 a 24", (("18:00", "24:00"),)),
    ],
)
def test_hours_understood(text, periods):
    answer = rules.parse_hours(text)
    assert answer is not None and answer.kind is HoursKind.PERIODS
    assert answer.periods == periods


@pytest.mark.parametrize("text", ["cierro", "Cerrado", "no abrimos"])
def test_closed_understood(text):
    assert rules.parse_hours(text).kind is HoursKind.CLOSED


@pytest.mark.parametrize("text", ["horario normal", "como siempre", "Normal"])
def test_regular_hours_understood(text):
    assert rules.parse_hours(text).kind is HoursKind.REGULAR


@pytest.mark.parametrize(
    "text",
    [
        "abrimos por la mañana",  # the ambiguous case of docs/01-flujos.md
        "de 9 a 2",  # 2 in the afternoon or at night? never guessed
        "de 14 a 10",
        "de 10 a 10",
        "de 10 a 25",
        "de 10:70 a 14",
        "de 10 a 14 y de 13 a 20",  # overlapping
        "de 17 a 20 y de 9 a 13",  # out of order
        "de 10 a 14 pero solo la terraza",
        "a las 10",
        "10",
        "sí",
        "",
        "de 1 a 2 y de 3 a 4 y de 5 a 6 y de 7 a 8",
        "el 10-12 de 2026",
    ],
)
def test_hours_not_understood_are_never_guessed(text):
    assert rules.parse_hours(text) is None


def test_hours_text():
    assert rules.hours_text((("10:00", "14:00"), ("17:00", "20:00"))) == (
        "de 10:00 a 14:00 y de 17:00 a 20:00"
    )


def test_two_clarifications_then_closed_options():
    assert [rules.clarification_key(n) for n in range(4)] == [
        "hours.clarify_1",
        "hours.clarify_2",
        "hours.options",
        "hours.options",
    ]


# --- linked addresses --------------------------------------------------------------


def identity(account="acc", channel="telegram", address="111", **changes):
    return ChannelIdentity("chid_1", account, channel, address, NOW, **changes)


def test_addresses_are_generic_across_channels():
    assert rules.normalize_address("telegram", " 123456789 ") == "123456789"
    assert rules.normalize_address("whatsapp", "+34 600 111 222") == "+34600111222"
    assert rules.normalize_address("whatsapp", "34600111222") == "+34600111222"
    assert rules.normalize_address("email", " Dueno@Bar.ES ") == "dueno@bar.es"


@pytest.mark.parametrize(
    ("channel", "address"),
    [
        ("telegram", "abc"),
        ("telegram", ""),
        ("whatsapp", "600"),
        ("whatsapp", "+0600111222"),
        ("whatsapp", "+34 600 111 222 333 444 555"),
        ("email", "sin-arroba"),
        ("sms", "+34600111222"),
    ],
)
def test_invalid_addresses(channel, address):
    with pytest.raises(rules.InvalidAddress):
        rules.normalize_address(channel, address)


def test_linking_an_address_free_same_account_or_another_account():
    assert rules.plan_link(None, "acc") is LinkOutcome.NEW
    assert rules.plan_link(identity(unlinked_at=NOW), "acc") is LinkOutcome.NEW
    assert rules.plan_link(identity("acc"), "acc") is LinkOutcome.SAME
    assert rules.plan_link(identity("other"), "acc") is LinkOutcome.MOVED


def test_only_an_active_address_of_the_owning_account_may_act():
    assert rules.may_act(identity("acc"), "acc")
    assert not rules.may_act(None, "acc")
    assert not rules.may_act(identity("other"), "acc")
    assert not rules.may_act(identity("acc", unlinked_at=NOW), "acc")
