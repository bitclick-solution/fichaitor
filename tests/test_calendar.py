import locale
from dataclasses import replace
from datetime import date, timedelta

import pytest

from app.domain.calendar import (
    Business,
    Calendar,
    CommercialEvent,
    DateRule,
    Dismissal,
    Holiday,
    KeyDateKind,
    LocalHoliday,
    ProposalKind,
    RuleKind,
    Sector,
    SectorMap,
    black_friday,
    check_local_holidays,
    due_proposals,
    easter_sunday,
    format_date_es,
    is_muted,
    key_dates,
    nth_weekday,
    sector_for,
)

SHOP_SECTORS = frozenset({Sector.RETAIL, Sector.BEAUTY_HEALTH})
ALL_SECTORS = frozenset(Sector)

INMACULADA = Holiday(date(2027, 12, 8), "Inmaculada Concepción", "national")
BLACK_FRIDAY = CommercialEvent(
    id="black_friday",
    name="Black Friday",
    kind=KeyDateKind.COMMERCIAL,
    rule=DateRule(RuleKind.NTH_WEEKDAY, month=11, weekday=3, n=4, offset_days=1),
    lead_days=14,
    sectors=SHOP_SECTORS,
    question="Se acerca Black Friday ({date}). ¿Tienes alguna oferta especial?",
)
CYBER_MONDAY = replace(
    BLACK_FRIDAY,
    id="cyber_monday",
    name="Cyber Monday",
    rule=DateRule(RuleKind.NTH_WEEKDAY, month=11, weekday=3, n=4, offset_days=4),
    question="Se acerca Cyber Monday ({date}). ¿Tienes alguna oferta especial?",
)
FATHERS_DAY = CommercialEvent(
    id="fathers_day",
    name="Día del Padre",
    kind=KeyDateKind.COMMERCIAL,
    rule=DateRule(RuleKind.FIXED, month=3, day=19),
    lead_days=14,
    sectors=SHOP_SECTORS,
    question="Se acerca el Día del Padre ({date}). ¿Tienes alguna oferta?",
)
SUMMER = CommercialEvent(
    id="summer_hours",
    name="horario de verano",
    kind=KeyDateKind.SEASON,
    rule=DateRule(RuleKind.FIXED, month=7, day=1),
    lead_days=21,
    sectors=ALL_SECTORS,
    question="Se acerca el verano. ¿Cambias de horario a partir del {date}?",
)
CHRISTMAS = CommercialEvent(
    id="christmas",
    name="Navidad",
    kind=KeyDateKind.SEASON,
    rule=DateRule(RuleKind.FIXED, month=12, day=24),
    lead_days=21,
    sectors=ALL_SECTORS,
    question="Se acerca la Navidad. ¿Qué horario tendrás en Nochebuena ({date})?",
    duration_days=2,
)

SHOP = Business(region="MD", sector=Sector.RETAIL)
NOTARY = Business(region="MD", sector=Sector.PROFESSIONAL_SERVICES)


def texts(business: Business, today: date, calendar: Calendar, **kwargs) -> list[str]:
    return [p.text for p in due_proposals(business, today, calendar, **kwargs)]


# --- date arithmetic -------------------------------------------------------


@pytest.mark.parametrize(
    ("year", "expected"),
    [
        (2000, date(2000, 4, 23)),
        (2008, date(2008, 3, 23)),
        (2019, date(2019, 4, 21)),
        (2024, date(2024, 3, 31)),
        (2025, date(2025, 4, 20)),
        (2026, date(2026, 4, 5)),
        (2027, date(2027, 3, 28)),
        (2028, date(2028, 4, 16)),
        (2038, date(2038, 4, 25)),
    ],
)
def test_easter_sunday_for_known_years(year: int, expected: date):
    assert easter_sunday(year) == expected


@pytest.mark.parametrize(
    ("year", "expected"),
    [
        (2019, date(2019, 11, 29)),
        (2023, date(2023, 11, 24)),
        (2024, date(2024, 11, 29)),
        (2025, date(2025, 11, 28)),
        (2026, date(2026, 11, 27)),
        (2027, date(2027, 11, 26)),
    ],
)
def test_black_friday_for_known_years(year: int, expected: date):
    assert black_friday(year) == expected
    assert BLACK_FRIDAY.rule.resolve(year) == expected


def test_black_friday_is_the_fourth_friday_except_when_november_starts_on_friday():
    for year in range(2015, 2040):
        fourth_friday = nth_weekday(year, 11, 4, 4)
        if date(year, 11, 1).weekday() == 4:
            assert black_friday(year) == fourth_friday + timedelta(days=7)
        else:
            assert black_friday(year) == fourth_friday


def test_cyber_monday_is_the_monday_after_black_friday_even_in_december():
    assert CYBER_MONDAY.rule.resolve(2026) == date(2026, 11, 30)
    assert CYBER_MONDAY.rule.resolve(2024) == date(2024, 12, 2)


def test_nth_weekday_first_sunday_of_may_is_mothers_day():
    assert nth_weekday(2026, 5, 6, 1) == date(2026, 5, 3)
    assert nth_weekday(2027, 5, 6, 1) == date(2027, 5, 2)


def test_nth_weekday_rejects_a_weekday_that_does_not_exist():
    with pytest.raises(ValueError):
        nth_weekday(2027, 2, 0, 5)


def test_easter_rule_with_offset_gives_palm_sunday_and_good_friday():
    assert DateRule(RuleKind.EASTER, offset_days=-7).resolve(2027) == date(2027, 3, 21)
    assert DateRule(RuleKind.EASTER, offset_days=-2).resolve(2027) == date(2027, 3, 26)


# --- Spanish dates ---------------------------------------------------------


def test_full_date_in_spanish():
    assert format_date_es(date(2026, 12, 8)) == "martes 8 de diciembre"
    assert format_date_es(date(2026, 11, 27)) == "viernes 27 de noviembre"
    assert format_date_es(date(2027, 1, 6)) == "miércoles 6 de enero"
    assert format_date_es(date(2027, 5, 1)) == "sábado 1 de mayo"


def test_full_date_does_not_depend_on_the_system_locale():
    previous = locale.setlocale(locale.LC_TIME)
    try:
        locale.setlocale(locale.LC_TIME, "C")
        assert format_date_es(date(2027, 8, 16)) == "lunes 16 de agosto"
    finally:
        locale.setlocale(locale.LC_TIME, previous)


def test_every_weekday_and_month_has_a_spanish_name():
    week = [format_date_es(date(2027, 3, 1) + timedelta(days=i)).split()[0] for i in range(7)]
    assert week == ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    months = [format_date_es(date(2027, m, 1)).split()[-1] for m in range(1, 13)]
    assert months[0] == "enero" and months[8] == "septiembre" and months[11] == "diciembre"
    assert len(set(months)) == 12


# --- lead time, exactly at the limit ---------------------------------------


def test_holiday_is_proposed_from_five_days_before():
    calendar = Calendar(holidays=(INMACULADA,))
    assert texts(SHOP, date(2027, 12, 2), calendar) == []
    assert texts(SHOP, date(2027, 12, 3), calendar) == [
        "El miércoles 8 de diciembre es festivo. ¿Abrís?"
    ]
    assert len(texts(SHOP, date(2027, 12, 4), calendar)) == 1


def test_holiday_stops_being_proposed_on_the_day_itself():
    calendar = Calendar(holidays=(INMACULADA,))
    assert len(texts(SHOP, date(2027, 12, 7), calendar)) == 1
    assert texts(SHOP, date(2027, 12, 8), calendar) == []
    assert texts(SHOP, date(2027, 12, 9), calendar) == []


def test_commercial_date_is_proposed_from_fourteen_days_before():
    calendar = Calendar(events=(BLACK_FRIDAY,))  # 26 November 2027
    assert texts(SHOP, date(2027, 11, 11), calendar) == []
    assert texts(SHOP, date(2027, 11, 12), calendar) == [
        "Se acerca Black Friday (viernes 26 de noviembre). ¿Tienes alguna oferta especial?"
    ]
    assert len(texts(SHOP, date(2027, 11, 13), calendar)) == 1


def test_commercial_date_expires_two_days_before():
    calendar = Calendar(events=(BLACK_FRIDAY,))
    assert len(texts(SHOP, date(2027, 11, 23), calendar)) == 1
    assert texts(SHOP, date(2027, 11, 24), calendar) == []


def test_season_is_proposed_from_twenty_one_days_before_until_it_starts():
    calendar = Calendar(events=(SUMMER,))
    assert texts(SHOP, date(2027, 6, 9), calendar) == []
    assert texts(SHOP, date(2027, 6, 10), calendar) == [
        "Se acerca el verano. ¿Cambias de horario a partir del jueves 1 de julio?"
    ]
    assert len(texts(SHOP, date(2027, 6, 11), calendar)) == 1
    assert len(texts(SHOP, date(2027, 6, 30), calendar)) == 1
    assert texts(SHOP, date(2027, 7, 1), calendar) == []


def test_lead_time_crosses_the_year_boundary():
    sales = CommercialEvent(
        id="winter_sales",
        name="rebajas de invierno",
        kind=KeyDateKind.COMMERCIAL,
        rule=DateRule(RuleKind.FIXED, month=1, day=7),
        lead_days=14,
        sectors=SHOP_SECTORS,
        question="Las rebajas de invierno suelen empezar el {date}. ¿Tienes rebajas?",
    )
    calendar = Calendar(events=(sales,))
    assert texts(SHOP, date(2026, 12, 23), calendar) == []
    assert texts(SHOP, date(2026, 12, 24), calendar) == [
        "Las rebajas de invierno suelen empezar el jueves 7 de enero. ¿Tienes rebajas?"
    ]


def test_already_proposed_key_dates_are_not_proposed_again():
    calendar = Calendar(holidays=(INMACULADA,))
    (proposal,) = due_proposals(SHOP, date(2027, 12, 3), calendar)
    assert proposal.keys == ("holiday:2027-12-08",)
    assert proposal.kind is ProposalKind.HOLIDAY
    assert proposal.expires_on == date(2027, 12, 8)
    again = due_proposals(
        SHOP, date(2027, 12, 4), calendar, already_proposed=frozenset(proposal.keys)
    )
    assert again == ()


# --- which holidays apply --------------------------------------------------


def test_regional_holiday_only_applies_in_its_region():
    calendar = Calendar(holidays=(Holiday(date(2027, 3, 1), "Día de Andalucía", "AN"),))
    andalusian = replace(SHOP, region="AN")
    assert len(texts(andalusian, date(2027, 2, 24), calendar)) == 1
    assert texts(SHOP, date(2027, 2, 24), calendar) == []


def test_unverified_holidays_are_never_proposed():
    unverified = Holiday(date(2027, 3, 1), "Día de Andalucía", "AN", verified=False)
    calendar = Calendar(holidays=(unverified,))
    assert texts(replace(SHOP, region="AN"), date(2027, 2, 24), calendar) == []


def test_preloaded_local_holiday_only_applies_in_its_municipality():
    corpus = Holiday(date(2027, 5, 27), "Corpus Christi", "AN", municipality="18087")
    calendar = Calendar(holidays=(corpus,))
    granada = Business(region="AN", sector=Sector.RETAIL, municipality_code="18087")
    seville = replace(granada, municipality_code="41091")
    unknown_town = replace(granada, municipality_code=None)
    assert texts(granada, date(2027, 5, 22), calendar) == [
        "El jueves 27 de mayo es fiesta local. ¿Abrís?"
    ]
    assert texts(seville, date(2027, 5, 22), calendar) == []
    assert texts(unknown_town, date(2027, 5, 22), calendar) == []


def test_two_holidays_in_the_same_week_are_one_question():
    constitution = Holiday(date(2027, 12, 6), "Día de la Constitución Española", "national")
    calendar = Calendar(holidays=(constitution, INMACULADA))
    (proposal,) = due_proposals(SHOP, date(2027, 12, 1), calendar)
    assert proposal.text == (
        "El lunes 6 de diciembre y el miércoles 8 de diciembre son festivos. ¿Abrís?"
    )
    assert proposal.keys == ("holiday:2027-12-06", "holiday:2027-12-08")


# --- local holidays declared by the owner ----------------------------------


def test_declared_local_holiday_is_proposed_like_a_holiday():
    business = replace(
        SHOP,
        local_holidays=(
            LocalHoliday(date(2027, 6, 3), "Corpus"),
            LocalHoliday(date(2027, 9, 15)),
        ),
    )
    calendar = Calendar()
    assert texts(business, date(2027, 5, 28), calendar) == []
    assert texts(business, date(2027, 5, 29), calendar) == [
        "El jueves 3 de junio es fiesta local. ¿Abrís?"
    ]
    assert texts(business, date(2027, 9, 10), calendar) == [
        "El miércoles 15 de septiembre es fiesta local. ¿Abrís?"
    ]


def test_declared_local_holiday_on_an_official_holiday_is_not_duplicated():
    business = replace(SHOP, local_holidays=(LocalHoliday(date(2027, 12, 8), "La Purísima"),))
    (proposal,) = due_proposals(business, date(2027, 12, 3), Calendar(holidays=(INMACULADA,)))
    assert len(proposal.items) == 1
    assert proposal.text == "El miércoles 8 de diciembre es festivo. ¿Abrís?"


def test_local_holiday_declaration_allows_two_per_year():
    check_local_holidays(
        [
            LocalHoliday(date(2027, 6, 3)),
            LocalHoliday(date(2027, 9, 15)),
            LocalHoliday(date(2028, 6, 15)),
            LocalHoliday(date(2028, 9, 15)),
        ]
    )


def test_local_holiday_declaration_rejects_a_third_one_in_a_year():
    declared = [LocalHoliday(date(2027, m, 3)) for m in (2, 6, 9)]
    with pytest.raises(ValueError, match="3 fiestas locales en 2027"):
        check_local_holidays(declared)


def test_local_holiday_declaration_rejects_a_repeated_date():
    with pytest.raises(ValueError, match="repetida"):
        check_local_holidays([LocalHoliday(date(2027, 6, 3)), LocalHoliday(date(2027, 6, 3))])


# --- relevance by sector ---------------------------------------------------


def test_black_friday_is_for_shops_and_not_for_a_notary():
    calendar = Calendar(events=(BLACK_FRIDAY,))
    day = date(2027, 11, 12)
    assert len(texts(SHOP, day, calendar)) == 1
    assert len(texts(replace(SHOP, sector=Sector.BEAUTY_HEALTH), day, calendar)) == 1
    assert texts(NOTARY, day, calendar) == []
    assert texts(replace(SHOP, sector=Sector.HOSPITALITY), day, calendar) == []


def test_holidays_are_relevant_for_every_sector():
    calendar = Calendar(holidays=(INMACULADA,))
    for sector in Sector:
        assert len(texts(replace(SHOP, sector=sector), date(2027, 12, 3), calendar)) == 1


def test_an_event_the_owner_used_before_is_relevant_whatever_the_sector():
    calendar = Calendar(events=(BLACK_FRIDAY,))
    notary = replace(NOTARY, accepted_events=frozenset({"black_friday"}))
    assert len(texts(notary, date(2027, 11, 12), calendar)) == 1


def test_sector_comes_from_the_google_category():
    sector_map = SectorMap(
        types={
            "restaurant": Sector.HOSPITALITY,
            "barber_shop": Sector.BEAUTY_HEALTH,
            "lawyer": Sector.PROFESSIONAL_SERVICES,
        },
        suffixes=(("_restaurant", Sector.HOSPITALITY), ("_shop", Sector.RETAIL)),
    )
    assert sector_for("restaurant", (), sector_map) is Sector.HOSPITALITY
    assert sector_for("pizza_restaurant", (), sector_map) is Sector.HOSPITALITY
    assert sector_for("gift_shop", (), sector_map) is Sector.RETAIL
    assert sector_for("barber_shop", (), sector_map) is Sector.BEAUTY_HEALTH  # exact wins
    assert sector_for("lawyer", ("gift_shop",), sector_map) is Sector.PROFESSIONAL_SERVICES
    assert sector_for("establishment", ("point_of_interest", "lawyer"), sector_map) is (
        Sector.PROFESSIONAL_SERVICES
    )
    assert sector_for("", (), sector_map) is Sector.OTHER
    assert sector_for("planetarium", ("establishment",), sector_map) is Sector.OTHER


# --- grouping --------------------------------------------------------------


def test_holiday_and_commercial_date_on_the_same_day_are_one_combined_proposal():
    san_jose = Holiday(date(2027, 3, 19), "San José", "MD", substitutable=True)
    calendar = Calendar(holidays=(san_jose,), events=(FATHERS_DAY,))

    (proposal,) = due_proposals(SHOP, date(2027, 3, 5), calendar)  # 14 days before
    assert proposal.kind is ProposalKind.COMBINED
    assert set(proposal.keys) == {"fathers_day:2027-03-19", "holiday:2027-03-19"}
    assert proposal.text == (
        "Se acercan varias fechas: Día del Padre (viernes 19 de marzo) y festivo el "
        "viernes 19 de marzo. ¿Tienes alguna oferta especial o cambia tu horario?"
    )

    # once that proposal exists, the holiday does not produce a second message
    later = due_proposals(
        SHOP, date(2027, 3, 14), calendar, already_proposed=frozenset(proposal.keys)
    )
    assert later == ()


def test_where_the_day_is_not_a_holiday_the_commercial_date_goes_alone():
    san_jose = Holiday(date(2027, 3, 19), "San José", "MD", substitutable=True)
    calendar = Calendar(holidays=(san_jose,), events=(FATHERS_DAY,))
    (proposal,) = due_proposals(replace(SHOP, region="AN"), date(2027, 3, 5), calendar)
    assert proposal.kind is ProposalKind.COMMERCIAL
    assert (
        proposal.text == "Se acerca el Día del Padre (viernes 19 de marzo). ¿Tienes alguna oferta?"
    )


def test_where_the_commercial_date_is_not_relevant_the_holiday_goes_alone():
    san_jose = Holiday(date(2027, 3, 19), "San José", "MD", substitutable=True)
    calendar = Calendar(holidays=(san_jose,), events=(FATHERS_DAY,))
    assert texts(NOTARY, date(2027, 3, 5), calendar) == []
    assert texts(NOTARY, date(2027, 3, 14), calendar) == [
        "El viernes 19 de marzo es festivo. ¿Abrís?"
    ]


def test_black_friday_and_cyber_monday_are_one_proposal():
    calendar = Calendar(events=(BLACK_FRIDAY, CYBER_MONDAY))
    (proposal,) = due_proposals(SHOP, date(2027, 11, 12), calendar)
    assert proposal.kind is ProposalKind.COMMERCIAL
    assert proposal.date == date(2027, 11, 26)
    assert proposal.text == (
        "Se acercan varias fechas: Black Friday (viernes 26 de noviembre) y Cyber Monday "
        "(lunes 29 de noviembre). ¿Tienes alguna oferta especial o cambia tu horario?"
    )


def test_season_absorbs_the_holiday_inside_it():
    christmas_day = Holiday(date(2027, 12, 25), "Natividad del Señor", "national")
    calendar = Calendar(holidays=(christmas_day,), events=(CHRISTMAS,))
    (proposal,) = due_proposals(NOTARY, date(2027, 12, 3), calendar)
    assert proposal.kind is ProposalKind.COMBINED
    assert proposal.text == (
        "Se acercan varias fechas: Navidad (desde el viernes 24 de diciembre) y festivo el "
        "sábado 25 de diciembre. ¿Cambia tu horario esos días?"
    )


def test_key_dates_far_apart_stay_in_separate_proposals():
    far = Holiday(date(2027, 12, 8), "Inmaculada Concepción", "national")
    event = replace(
        FATHERS_DAY, id="early_december", rule=DateRule(RuleKind.FIXED, month=12, day=17)
    )
    calendar = Calendar(holidays=(far,), events=(event,))
    proposals = due_proposals(SHOP, date(2027, 12, 3), calendar)
    assert [p.kind for p in proposals] == [ProposalKind.HOLIDAY, ProposalKind.COMMERCIAL]


def test_a_dismissed_date_is_left_out_of_the_group():
    san_jose = Holiday(date(2027, 3, 19), "San José", "MD", substitutable=True)
    calendar = Calendar(holidays=(san_jose,), events=(FATHERS_DAY,))
    business = replace(SHOP, dismissed=(Dismissal("fathers_day", date(2027, 3, 19)),))
    assert texts(business, date(2027, 3, 5), calendar) == []
    assert texts(business, date(2027, 3, 14), calendar) == [
        "El viernes 19 de marzo es festivo. ¿Abrís?"
    ]


# --- learning from dismissals ----------------------------------------------


def _dismissed_black_friday(*years: int) -> tuple[Dismissal, ...]:
    return tuple(Dismissal("black_friday", black_friday(year)) for year in years)


def test_commercial_date_dismissed_two_years_in_a_row_is_no_longer_proposed():
    calendar = Calendar(events=(BLACK_FRIDAY,))
    business = replace(SHOP, dismissed=_dismissed_black_friday(2025, 2026))
    assert texts(business, date(2027, 11, 12), calendar) == []
    # and it stays muted afterwards, although 2027 has no dismissal of its own
    assert texts(business, date(2028, 11, 10), calendar) == []


def test_commercial_date_dismissed_once_is_proposed_again_next_year():
    calendar = Calendar(events=(BLACK_FRIDAY,))
    business = replace(SHOP, dismissed=_dismissed_black_friday(2026))
    assert len(texts(business, date(2027, 11, 12), calendar)) == 1


def test_dismissals_in_non_consecutive_years_do_not_mute():
    calendar = Calendar(events=(BLACK_FRIDAY,))
    business = replace(SHOP, dismissed=_dismissed_black_friday(2024, 2026))
    assert len(texts(business, date(2027, 11, 12), calendar)) == 1


def test_dismissing_this_year_only_silences_this_year():
    calendar = Calendar(events=(BLACK_FRIDAY,))
    business = replace(SHOP, dismissed=_dismissed_black_friday(2027))
    assert texts(business, date(2027, 11, 12), calendar) == []
    assert len(texts(business, date(2028, 11, 10), calendar)) == 1


def test_muting_one_event_does_not_mute_another():
    calendar = Calendar(events=(BLACK_FRIDAY, FATHERS_DAY))
    business = replace(SHOP, dismissed=_dismissed_black_friday(2025, 2026))
    assert len(texts(business, date(2027, 3, 5), calendar)) == 1


def test_holidays_are_never_muted_by_dismissals():
    calendar = Calendar(holidays=(INMACULADA,))
    dismissed = tuple(Dismissal("holiday", date(year, 12, 8)) for year in (2025, 2026))
    assert len(texts(replace(SHOP, dismissed=dismissed), date(2027, 12, 3), calendar)) == 1
    this_year = (Dismissal("holiday", date(2027, 12, 8)),)
    assert texts(replace(SHOP, dismissed=this_year), date(2027, 12, 3), calendar) == []


def test_accepting_an_event_again_lifts_the_mute():
    calendar = Calendar(events=(BLACK_FRIDAY,))
    business = replace(
        SHOP,
        dismissed=_dismissed_black_friday(2025, 2026),
        accepted_events=frozenset({"black_friday"}),
    )
    assert len(texts(business, date(2027, 11, 12), calendar)) == 1


def test_is_muted_needs_two_consecutive_years_before_the_given_one():
    two_in_a_row = _dismissed_black_friday(2025, 2026)
    assert is_muted("black_friday", 2027, two_in_a_row)
    assert is_muted("black_friday", 2030, two_in_a_row)
    assert not is_muted("black_friday", 2026, two_in_a_row)
    assert not is_muted("cyber_monday", 2027, two_in_a_row)
    assert not is_muted("black_friday", 2027, ())


# --- the whole year --------------------------------------------------------


def test_key_dates_lists_the_year_in_order_for_the_calendar_screen():
    calendar = Calendar(holidays=(INMACULADA,), events=(BLACK_FRIDAY, SUMMER, CHRISTMAS))
    found = key_dates(SHOP, calendar, (2027,))
    assert [(k.event_id, k.date) for k in found] == [
        ("summer_hours", date(2027, 7, 1)),
        ("black_friday", date(2027, 11, 26)),
        ("holiday", date(2027, 12, 8)),
        ("christmas", date(2027, 12, 24)),
    ]
    assert found[3].end == date(2027, 12, 25)
