"""Pure rules of the key dates: which days are asked about, where a business is, the
change of hours the owner confirms and how it becomes Google's specialHours."""

from datetime import date

import pytest

from app.adapters.calendar.loader import load_calendar
from app.domain import key_dates as kd
from app.domain import special_hours as sh
from app.domain.calendar import Calendar, Holiday
from app.domain.proposals import ProposalState as S
from app.domain.proposals import advance

CAL = Calendar(
    holidays=(
        Holiday(date(2026, 12, 8), "Inmaculada Concepción", "national"),
        Holiday(date(2026, 12, 7), "Lunes siguiente a la Constitución", "AN"),
        Holiday(date(2026, 12, 7), "Lunes siguiente a la Constitución", "MD"),
        Holiday(date(2026, 12, 26), "San Esteban", "CT"),
        Holiday(date(2026, 12, 25), "Natividad del Señor", "national"),
        Holiday(date(2026, 12, 4), "Fiesta del pueblo", "AN", municipality="18087"),
        Holiday(date(2026, 12, 5), "Sin verificar", "AN", verified=False),
    )
)


# --- where the business is -----------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "region"),
    [
        ("18010", "AN"),
        ("28013", "MD"),
        ("08001", "CT"),
        ("35001", "CN"),
        ("38001", "CN"),
        ("07001", "IB"),
        ("51001", "CE"),
        ("52001", "ML"),
        ("01001", "PV"),
        ("53000", ""),
        ("00000", ""),
        ("1801", ""),
        ("", ""),
    ],
)
def test_region_comes_from_the_province_of_the_postal_code(code, region):
    assert kd.region_for_postal_code(code) == region


def test_region_falls_back_to_the_postal_code_in_the_stored_address():
    assert kd.region_of("", "Calle Elvira 1, 18010 Granada") == "AN"
    assert kd.region_of("MD", "Calle Elvira 1, 18010 Granada") == "MD"
    assert kd.region_of("", "Calle Mayor 123456, Granada") == ""
    assert kd.region_of("", "") == ""


# --- which days are asked about ---------------------------------------------------------


def days(questions):
    return [(q.day, q.kind, q.name) for q in questions]


def test_holidays_of_the_region_inside_the_lead_days():
    today = date(2026, 12, 3)
    assert days(kd.hours_questions("AN", today, CAL)) == [
        (date(2026, 12, 7), "holiday", "Lunes siguiente a la Constitución"),
        (date(2026, 12, 8), "holiday", "Inmaculada Concepción"),
    ]


def test_without_a_region_only_national_holidays():
    today = date(2026, 12, 3)
    assert [q.day for q in kd.hours_questions("", today, CAL)] == [date(2026, 12, 8)]


def test_another_region_and_unverified_ones_are_not_asked():
    today = date(2026, 12, 21)
    asked = [q.day for q in kd.hours_questions("AN", today, CAL)]
    assert date(2026, 12, 26) not in asked
    assert date(2026, 12, 25) in asked


def test_a_preloaded_local_holiday_only_with_its_town():
    today = date(2026, 12, 1)
    assert date(2026, 12, 4) not in [q.day for q in kd.hours_questions("AN", today, CAL)]
    with_town = kd.hours_questions("AN", today, CAL, municipality="18087")
    assert (date(2026, 12, 4), "holiday", "Fiesta del pueblo") in days(with_town)
    assert all(q.day != date(2026, 12, 5) for q in with_town)  # unverified


def test_not_today_and_not_beyond_the_lead_days():
    assert [q.day for q in kd.hours_questions("AN", date(2026, 12, 8), CAL)] == []
    assert [q.day for q in kd.hours_questions("AN", date(2026, 12, 2), CAL)] == [date(2026, 12, 7)]
    longer = kd.hours_questions("AN", date(2026, 11, 20), CAL, lead_days=20)
    assert [q.day for q in longer] == [date(2026, 12, 7), date(2026, 12, 8)]


def test_a_day_with_special_hours_in_google_is_not_asked():
    today = date(2026, 12, 3)
    asked = kd.hours_questions("AN", today, CAL, skip=[date(2026, 12, 8)])
    assert [q.day for q in asked] == [date(2026, 12, 7)]


def test_nochebuena_and_nochevieja_with_the_real_data():
    calendar = load_calendar().calendar
    asked = days(kd.hours_questions("AN", date(2026, 12, 20), calendar))
    assert (date(2026, 12, 24), "season", "Nochebuena") in asked
    assert (date(2026, 12, 25), "holiday", "Natividad del Señor") in asked
    assert (date(2026, 12, 31), "season", "Nochevieja") in asked
    assert all(name not in ("Black Friday", "Navidad") for _, _, name in asked)
    assert len({d for d, _, _ in asked}) == len(asked)  # one question per day


def test_the_real_data_for_granada_in_december_2026():
    calendar = load_calendar().calendar
    asked = [q.day for q in kd.hours_questions("AN", date(2026, 12, 3), calendar)]
    # Holidays 5 days ahead; Nochebuena with the 21 days of its season (docs/calendario.md).
    assert asked == [date(2026, 12, 7), date(2026, 12, 8), date(2026, 12, 24)]


def test_one_proposal_per_profile_and_day():
    assert kd.source_ref("loc_abc123", date(2026, 12, 8)) == "loc_abc123:2026-12-08"


# --- the panel ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state", [S.SCHEDULED, S.SENT, S.REMINDED, S.COLLECTING, S.TO_CONFIRM, S.EXPIRED]
)
def test_the_panel_confirms_walking_the_diagram(state):
    path = kd.panel_confirm_path(state, date(2026, 12, 8), date(2026, 12, 8))
    assert advance(state, *path) is S.EXECUTING


@pytest.mark.parametrize("state", [S.EXECUTING, S.APPLIED, S.FAILED, S.DISMISSED, S.CANCELLED])
def test_the_panel_cannot_answer_what_is_resolved(state):
    with pytest.raises(kd.PanelRefused) as caught:
        kd.panel_confirm_path(state, date(2026, 12, 8), date(2026, 12, 1))
    assert caught.value.code == kd.REFUSED_CLOSED


def test_the_panel_cannot_answer_a_day_that_has_passed():
    with pytest.raises(kd.PanelRefused) as caught:
        kd.panel_confirm_path(S.SENT, date(2026, 12, 8), date(2026, 12, 9))
    assert caught.value.code == kd.REFUSED_PAST
    assert not kd.answerable(S.SENT, date(2026, 12, 8), date(2026, 12, 9))
    assert kd.answerable(S.EXPIRED, date(2026, 12, 8), date(2026, 12, 8))


@pytest.mark.parametrize(
    ("state", "final"),
    [
        (S.SCHEDULED, S.CANCELLED),
        (S.SENT, S.DISMISSED),
        (S.REMINDED, S.DISMISSED),
        (S.COLLECTING, S.DISMISSED),
        (S.TO_CONFIRM, S.DISMISSED),
    ],
)
def test_the_panel_dismisses(state, final):
    assert advance(state, *kd.panel_dismiss_path(state)) is final


def test_the_panel_names_the_states():
    assert kd.panel_status(S.SENT) == "pendiente"
    assert kd.panel_status(S.EXECUTING) == "en_curso"
    assert kd.panel_status(S.CANCELLED) == "caducada"


# --- the change of hours ----------------------------------------------------------------

DAY = date(2026, 12, 8)


def test_canonical_text_round_trip():
    change = sh.checked(DAY, sh.KIND_PERIODS, [("09:00", "13:00"), ("17:00", "20:30")])
    assert change.periods == (("9:00", "13:00"), ("17:00", "20:30"))
    text = change.to_text()
    assert (
        text
        == '{"date":"2026-12-08","kind":"periods","periods":[["9:00","13:00"],["17:00","20:30"]]}'
    )
    assert sh.from_text(text) == change
    assert sh.describe(change) == "de 9:00 a 13:00 y de 17:00 a 20:30"


@pytest.mark.parametrize(
    "periods",
    [
        [],
        [("9:00", "13:00"), ("14:00", "15:00"), ("17:00", "20:00")],  # three
        [("20:00", "2:00")],  # past midnight
        [("17:00", "20:00"), ("9:00", "13:00")],  # out of order
        [("9:00", "14:00"), ("13:00", "20:00")],  # overlapping
        [("9:00", "9:00")],
        [("25:00", "26:00")],
        [("9:60", "10:00")],
        [("24:00", "24:00")],
        [("9", "13")],
        [("9:00",)],
    ],
)
def test_bad_periods_are_refused(periods):
    with pytest.raises(sh.InvalidSpecialHours):
        sh.checked(DAY, sh.KIND_PERIODS, periods)


def test_closing_at_midnight_is_valid():
    change = sh.checked(DAY, sh.KIND_PERIODS, [("18:00", "24:00")])
    (period,) = sh.google_periods(change)
    assert period["closeTime"] == {"hours": 24, "minutes": 0}


def test_closed_and_regular_carry_no_hours():
    with pytest.raises(sh.InvalidSpecialHours):
        sh.checked(DAY, sh.KIND_CLOSED, [("9:00", "13:00")])
    with pytest.raises(sh.InvalidSpecialHours):
        sh.checked(DAY, "open_all_day")


def test_entries_of_the_conversation():
    assert sh.from_entries([{"date": "2026-12-08", "closed": True}]) == sh.checked(DAY, "closed")
    assert sh.from_entries([{"date": "2026-12-08", "regular": True}]).kind == "regular"
    two = sh.from_entries(
        [
            {"date": "2026-12-08", "open": "9:00", "close": "13:00"},
            {"date": "2026-12-08", "open": "17:00", "close": "20:00"},
        ]
    )
    assert two.periods == (("9:00", "13:00"), ("17:00", "20:00"))


@pytest.mark.parametrize(
    "entries",
    [
        [],
        "cerrado",
        [{"date": "2026-12-08", "closed": True}, {"date": "2026-12-08", "closed": True}],
        [{"date": "2026-12-08", "closed": True, "extra": 1}],
        [
            {"date": "2026-12-08", "open": "9:00", "close": "13:00"},
            {"date": "2026-12-09", "open": "9:00", "close": "13:00"},
        ],
        [{"date": "2026-02-30", "closed": True}],
        [{"closed": True}],
    ],
)
def test_bad_entries_are_refused(entries):
    with pytest.raises(sh.InvalidSpecialHours):
        sh.from_entries(entries)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "null",
        "[]",
        '{"date":"2026-12-08","kind":"closed"}',
        '{"date":"2026-12-08","kind":"closed","periods":[],"x":1}',
        '{"date": "2026-12-08", "kind": "closed", "periods": []}',  # not canonical
        '{"date":"2026-12-08","kind":"periods","periods":[["09:00","13:00"]]}',  # not canonical
    ],
)
def test_only_the_canonical_text_is_read_back(text):
    with pytest.raises(sh.InvalidSpecialHours):
        sh.from_text(text)


REGULAR = (
    {
        "openDay": "TUESDAY",
        "openTime": {"hours": 17},
        "closeDay": "TUESDAY",
        "closeTime": {"hours": 20},
    },
    {
        "openDay": "TUESDAY",
        "openTime": {"hours": 9},
        "closeDay": "TUESDAY",
        "closeTime": {"hours": 14},
    },
    {
        "openDay": "FRIDAY",
        "openTime": {"hours": 20},
        "closeDay": "SATURDAY",
        "closeTime": {"hours": 2},
    },
    {"openDay": "MONDAY", "openTime": {}, "closeDay": "WEDNESDAY", "closeTime": {}},
)


def test_regular_hours_of_that_weekday():
    periods = sh.regular_periods_for(REGULAR, DAY)  # a Tuesday
    assert [(p["openTime"]["hours"], p["closeTime"]["hours"]) for p in periods] == [
        (9, 14),
        (17, 20),
    ]


def test_a_regular_period_past_midnight_ends_the_next_day():
    (period,) = sh.regular_periods_for(REGULAR, date(2026, 12, 11))  # a Friday
    assert period["endDate"] == {"year": 2026, "month": 12, "day": 12}
    assert period["closeTime"] == {"hours": 2, "minutes": 0}


def test_a_weekday_normally_closed_stays_closed_and_no_regular_hours_is_an_error():
    assert sh.regular_periods_for(REGULAR, date(2026, 12, 13)) == [
        {"startDate": {"year": 2026, "month": 12, "day": 13}, "closed": True}
    ]
    with pytest.raises(sh.NoRegularHours):
        sh.regular_periods_for((), DAY)


def test_merge_replaces_only_that_day_and_keeps_what_it_cannot_read():
    other = {"startDate": {"year": 2027, "month": 8, "day": 15}, "closed": True}
    unreadable = {"weird": True}
    old_same_day = {"startDate": {"year": 2026, "month": 12, "day": 8}, "openTime": {"hours": 10}}
    wanted = sh.google_periods(sh.checked(DAY, sh.KIND_CLOSED))
    merged = sh.merge([other, old_same_day, unreadable], DAY, wanted)
    assert other in merged and unreadable in merged and old_same_day not in merged
    assert wanted[0] in merged and len(merged) == 3


def test_already_there_reads_googles_echo_with_fields_left_out():
    wanted = sh.google_periods(sh.checked(DAY, sh.KIND_PERIODS, [("10:00", "14:00")]))
    echo = [
        {
            "startDate": {"year": 2026, "month": 12, "day": 8},
            "openTime": {"hours": 10},
            "endDate": {"year": 2026, "month": 12, "day": 8},
            "closeTime": {"hours": 14},
        }
    ]
    assert sh.already_there(echo, wanted)
    assert not sh.already_there([], wanted)
    closed = sh.google_periods(sh.checked(DAY, sh.KIND_CLOSED))
    assert not sh.already_there(echo, closed)
