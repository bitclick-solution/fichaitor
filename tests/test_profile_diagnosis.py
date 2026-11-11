"""Diagnosis of a connected profile: pure rules over the facts the worker stored."""

from dataclasses import replace
from datetime import date

from app.adapters.calendar.loader import load_calendar
from app.domain.calendar import Holiday
from app.domain.diagnosis import PlaceSnapshot, diagnose
from app.domain.profile_diagnosis import (
    ConnectedProfile,
    diagnose_profile,
    holidays_without_hours,
)
from app.domain.profile_summary import ProfileFacts

TODAY = date(2026, 11, 11)
INMACULADA = Holiday(date(2026, 12, 8), "Inmaculada Concepción", "national")
NAVIDAD = Holiday(date(2026, 12, 25), "Natividad del Señor", "national")
ANDALUCIA = Holiday(date(2026, 12, 7), "Lunes siguiente a la Constitución", "AN")
HOLIDAYS = (ANDALUCIA, INMACULADA, NAVIDAD)

FULL_FACTS = ProfileFacts(
    title="Pizzería Napoli",
    address="Calle Elvira 1, 18010 Granada",
    category="Pizzería",
    additional_categories=2,
    description_length=600,
    has_phone=True,
    has_website=True,
    service_count=5,
    is_open=True,
    place_id="ChIJ-napoli",
    maps_url="https://maps.google.com/?cid=1",
    special_dates=(date(2026, 12, 8),),
    photo_count=40,
    attribute_count=6,
)
FULL = ConnectedProfile(
    name="Pizzería Napoli (conexión)",
    facts=FULL_FACTS,
    review_count=120,
    average_rating=4.6,
    unanswered_count=0,
    unanswered_low_count=0,
    has_regular_hours=True,
    google_changed_fields=(),
)


def run(profile=FULL, holidays=HOLIDAYS, today=TODAY, **facts):
    if facts:
        profile = replace(profile, facts=replace(profile.facts, **facts))
    return diagnose_profile(profile, holidays, today)


def codes(diagnosis) -> list[str]:
    return [f.code for f in diagnosis.findings]


def finding(diagnosis, code):
    return next(f for f in diagnosis.findings if f.code == code)


# --- shape --------------------------------------------------------------------


def test_a_complete_profile_scores_100_with_nothing_to_improve_and_nothing_locked():
    d = run()
    assert d.score == 100
    assert d.label == "Bien"
    assert d.findings == ()
    assert d.locked_checks == ()


def test_identity_comes_from_the_facts_and_falls_back_to_the_connection():
    d = run()
    assert (d.name, d.address, d.category) == (
        "Pizzería Napoli",
        "Calle Elvira 1, 18010 Granada",
        "Pizzería",
    )
    assert d.place_id == "ChIJ-napoli"
    assert d.maps_url == "https://maps.google.com/?cid=1"

    d = run(replace(FULL, place_id="ChIJ-de-la-conexion"), title="", place_id="", maps_url="")
    assert d.name == "Pizzería Napoli (conexión)"
    assert d.place_id == "ChIJ-de-la-conexion"
    assert d.maps_url == ""


def test_at_most_five_findings_ordered_by_impact_and_the_score_counts_them_all():
    empty = ConnectedProfile(name="Vacía", facts=ProfileFacts(photo_count=0, attribute_count=0))
    d = diagnose_profile(empty, (), TODAY)
    assert len(d.findings) == 5
    impacts = [f.impact.value for f in d.findings]
    assert impacts == sorted(impacts, key=["alto", "medio", "bajo"].index)
    assert codes(d)[:2] == ["no_hours", "no_photos"]
    # no_hours 20, no_photos 15, few_reviews 10, no_description 8, no_website 8,
    # no_phone 8, no_secondary_categories 6, no_service_attributes 5
    assert d.score == 20
    assert d.label == "Necesita atención"


def test_the_score_never_goes_below_zero():
    worst = ConnectedProfile(
        name="Peor",
        facts=ProfileFacts(is_open=False, photo_count=0, attribute_count=0),
        review_count=3,
        average_rating=2.0,
        unanswered_count=3,
        unanswered_low_count=3,
        google_changed_fields=("title",),
    )
    assert diagnose_profile(worst, (), TODAY).score == 0


# --- rules shared with the public diagnosis: same code, text and weight ---------


def test_shared_rules_use_the_texts_and_weights_of_the_public_diagnosis():
    public = diagnose(PlaceSnapshot(place_id="p", name="n", photo_count=20, review_count=3))
    by_code = {f.code: f for f in public.findings}
    connected = diagnose_profile(
        ConnectedProfile(
            name="n",
            facts=ProfileFacts(description_length=600, photo_count=20, attribute_count=4),
            review_count=3,
        ),
        (),
        TODAY,
    )
    shared = {"no_hours", "few_reviews", "no_website", "no_phone", "no_secondary_categories"}
    assert set(codes(connected)) == shared
    for f in connected.findings:
        assert f == by_code[f.code]


def test_low_rating_and_closed_profile_match_the_public_ones():
    public = diagnose(
        PlaceSnapshot(place_id="p", name="n", operational=False, rating=3.4, review_count=50)
    )
    by_code = {f.code: f for f in public.findings}
    d = run(replace(FULL, average_rating=3.4), is_open=False)
    assert finding(d, "not_operational") == by_code["not_operational"]
    assert finding(d, "low_rating") == by_code["low_rating"]
    assert finding(d, "low_rating").title == "Tu nota media es 3,4"


def test_photos_count_what_the_owner_uploaded():
    assert finding(run(photo_count=0), "no_photos").title == "No has subido ninguna foto"
    assert finding(run(photo_count=0), "no_photos").impact.value == "alto"
    assert finding(run(photo_count=1), "few_photos").title == "Solo has subido 1 foto"
    assert finding(run(photo_count=3), "few_photos").title == "Solo has subido 3 fotos"
    assert finding(run(photo_count=7), "some_photos").title == "Has subido 7 fotos; hay margen"
    assert run(photo_count=10).findings == ()


def test_facts_google_did_not_let_us_read_are_not_judged():
    d = run(photo_count=None, attribute_count=None)
    assert d.findings == () and d.score == 100


def test_no_attributes_is_a_low_finding():
    f = finding(run(attribute_count=0), "no_service_attributes")
    assert f.impact.value == "bajo" and f.penalty == 5


# --- unanswered reviews ---------------------------------------------------------


def test_unanswered_reviews_say_how_many_and_are_high_impact_with_a_low_one():
    d = run(replace(FULL, unanswered_count=13, unanswered_low_count=1))
    f = finding(d, "unanswered_reviews")
    assert f.title == "Tienes 13 reseñas sin responder"
    assert f.impact.value == "alto"
    assert f.detail.startswith("Una es de 1 o 2 estrellas.")
    assert d.findings[0] is f
    assert d.score == 85

    f = finding(
        run(replace(FULL, unanswered_count=5, unanswered_low_count=2)), "unanswered_reviews"
    )
    assert f.detail.startswith("2 son de 1 o 2 estrellas.")


def test_unanswered_reviews_without_low_ones_are_medium_impact():
    f = finding(run(replace(FULL, unanswered_count=4)), "unanswered_reviews")
    assert f.title == "Tienes 4 reseñas sin responder"
    assert f.impact.value == "medio" and f.penalty == 8


def test_a_single_unanswered_review_reads_in_singular():
    f = finding(run(replace(FULL, unanswered_count=1)), "unanswered_reviews")
    assert f.title == "Tienes 1 reseña sin responder"
    f = finding(
        run(replace(FULL, unanswered_count=1, unanswered_low_count=1)), "unanswered_reviews"
    )
    assert f.detail.startswith("Es de 1 o 2 estrellas.")


def test_every_review_answered_is_no_finding():
    assert "unanswered_reviews" not in codes(run())


# --- holiday hours --------------------------------------------------------------


def test_a_national_holiday_in_the_next_30_days_without_special_hours_is_named():
    d = run(special_dates=())
    f = finding(d, "no_holiday_hours")
    assert f.title == "Sin horario para el festivo del 8 de diciembre"
    assert f.detail.startswith("El 8 de diciembre es festivo nacional y no has indicado si abres.")
    assert f.impact.value == "medio"
    assert d.score == 92


def test_regional_holidays_are_left_out_until_the_region_is_known():
    assert holidays_without_hours(HOLIDAYS, (), TODAY) == (date(2026, 12, 8),)


def test_a_holiday_with_special_hours_is_covered():
    assert "no_holiday_hours" not in codes(run(special_dates=(date(2026, 12, 8),)))


def test_the_window_is_30_days_and_today_counts():
    assert holidays_without_hours(HOLIDAYS, (), date(2026, 11, 7)) == ()  # 31 days ahead
    assert holidays_without_hours(HOLIDAYS, (), date(2026, 11, 8)) == (date(2026, 12, 8),)
    assert holidays_without_hours(HOLIDAYS, (), date(2026, 12, 8)) == (
        date(2026, 12, 8),
        date(2026, 12, 25),
    )
    assert holidays_without_hours(HOLIDAYS, (), date(2026, 12, 9)) == (date(2026, 12, 25),)


def test_several_holidays_are_listed_and_a_close_one_is_high_impact():
    d = run(today=date(2026, 12, 2), special_dates=())
    f = finding(d, "no_holiday_hours")
    assert f.title == "Sin horario para 2 festivos próximos"
    assert f.detail.startswith(
        "El 8 de diciembre y el 25 de diciembre son festivos nacionales y no has indicado"
    )
    assert f.impact.value == "alto"  # the first one is 6 days away


def test_unverified_and_local_holidays_are_never_used():
    doubtful = Holiday(date(2026, 11, 20), "Sin verificar", "national", verified=False)
    local = Holiday(date(2026, 11, 21), "Local", "national", municipality="18087")
    assert holidays_without_hours((doubtful, local), (), TODAY) == ()


def test_holiday_hours_are_not_asked_of_a_profile_without_hours_or_closed():
    assert "no_holiday_hours" not in codes(run(replace(FULL, has_regular_hours=False)))
    assert "no_holiday_hours" not in codes(run(is_open=False, special_dates=()))


def test_the_real_calendar_gives_the_national_holiday_of_december_2026():
    holidays = load_calendar().calendar.holidays
    assert holidays_without_hours(holidays, (), TODAY) == (date(2026, 12, 8),)
    # Christmas, New Year and Epiphany, with the Epiphany inside the 30 days.
    assert holidays_without_hours(holidays, (), date(2026, 12, 20)) == (
        date(2026, 12, 25),
        date(2027, 1, 1),
        date(2027, 1, 6),
    )


# --- description ----------------------------------------------------------------


def test_no_description_is_medium_and_a_short_one_is_low():
    f = finding(run(description_length=0), "no_description")
    assert f.title == "No tienes descripción" and f.impact.value == "medio"
    f = finding(run(description_length=64), "short_description")
    assert f.title == "Tu descripción es muy corta" and f.impact.value == "bajo"
    assert "64 caracteres de los 750" in f.detail
    assert run(description_length=150).findings == ()


# --- changes made by Google -----------------------------------------------------


def test_google_changes_are_listed_with_their_spanish_labels():
    d = run(
        replace(
            FULL,
            google_changed_fields=(
                "regularHours",
                "phoneNumbers.primaryPhone",
                "phoneNumbers.additionalPhones",
                "websiteUri",
            ),
        )
    )
    f = finding(d, "google_changed")
    assert f.title == "Google ha cambiado datos de tu ficha"
    assert f.detail.startswith("Ha cambiado: Horario, Teléfono y Web.")
    assert f.impact.value == "medio"


def test_a_single_change_and_an_unknown_field():
    f = finding(run(replace(FULL, google_changed_fields=("specialHours",))), "google_changed")
    assert f.detail.startswith("Ha cambiado: Horarios especiales.")
    f = finding(run(replace(FULL, google_changed_fields=("campoNuevo",))), "google_changed")
    assert f.detail.startswith("Ha cambiado: campoNuevo.")
