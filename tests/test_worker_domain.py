"""Pure rules of the worker: job retry policy and the profile summary."""

import json
from datetime import UTC, date, datetime

from app.domain.jobs import Job, PermanentJobError, backoff_seconds, retry_delay, safe_error
from app.domain.profile_summary import (
    MediaSummary,
    ProfileDetails,
    ProfileHours,
    Review,
    ReviewSet,
    collect_facts,
    facts_from_json,
    facts_to_json,
    original_text,
    parse_diff_mask,
    star_rating,
    summarize,
)

TODAY = date(2026, 11, 11)


def review(rating, day, replied=False):
    return Review(rating=rating, created_at=datetime(2026, 10, day, tzinfo=UTC), replied=replied)


# --- jobs ---------------------------------------------------------------------


def test_backoff_grows_and_is_capped():
    delays = [backoff_seconds(n) for n in range(1, 12)]
    assert delays[:4] == [30, 60, 120, 240]
    assert delays == sorted(delays)
    assert delays[-1] == 3600
    assert backoff_seconds(0) == 30  # never negative or zero


def test_retry_delay_none_means_failed_for_good():
    job = Job(id="1", kind="k", payload={}, attempts=1, max_attempts=3)
    assert retry_delay(job, RuntimeError("x")) == 30
    assert retry_delay(job, PermanentJobError("x")) is None
    last = Job(id="1", kind="k", payload={}, attempts=3, max_attempts=3)
    assert last.is_last_attempt
    assert retry_delay(last, RuntimeError("x")) is None


def test_safe_error_scrubs_secrets_and_truncates():
    text = safe_error(
        "fallo con 1//token-secreto y ya29.acceso", ["1//token-secreto", "ya29.acceso"]
    )
    assert "token-secreto" not in text and "ya29" not in text
    assert "fallo con" in text
    assert len(safe_error("x" * 5000)) <= 500
    assert safe_error("nada", ["", None]) == "nada"  # empty secrets never blank the text


# --- review text --------------------------------------------------------------


def test_original_text_keeps_the_original_not_the_translation():
    raw = "(Translated by Google) Very good service\n\n(Original)\nMuy buen servicio"
    assert original_text(raw) == "Muy buen servicio"


def test_original_text_when_the_translation_comes_last():
    raw = "Muy buen servicio\n\n(Translated by Google)\nVery good service"
    assert original_text(raw) == "Muy buen servicio"


def test_original_text_without_markers_or_empty():
    assert original_text("  Todo perfecto ") == "Todo perfecto"
    assert original_text("") == ""
    assert original_text(None) == ""


def test_star_rating_and_diff_mask_parsing():
    assert [star_rating(s) for s in ("ONE", "TWO", "THREE", "FOUR", "FIVE")] == [1, 2, 3, 4, 5]
    assert star_rating("STAR_RATING_UNSPECIFIED") is None
    assert star_rating(None) is None
    assert parse_diff_mask("specialHours, title,,specialHours") == ("specialHours", "title")
    assert parse_diff_mask("") == ()
    assert parse_diff_mask(None) == ()


# --- summary ------------------------------------------------------------------


def test_summary_counts_answered_and_unanswered_reviews():
    reviews = ReviewSet(
        reviews=(
            review(5, 1, replied=True),
            review(1, 20),  # unanswered and low
            review(2, 3),  # unanswered and low
            review(2, 4, replied=True),  # low but answered
            review(4, 5),  # unanswered
            review(None, 6),  # no stars: unanswered, not low
        ),
        total=13,
        average=4.7,
    )
    s = summarize(reviews, ProfileHours(has_regular_hours=True), ("specialHours",), TODAY)
    assert s.review_count == 13  # Google's total wins over the page we read
    assert s.average_rating == 4.7
    assert s.unanswered_count == 4
    assert s.unanswered_low_count == 2
    assert s.last_review_at == datetime(2026, 10, 20, tzinfo=UTC)
    assert s.has_regular_hours is True
    assert s.google_changed is True
    assert s.google_changed_fields == ("specialHours",)


def test_summary_computes_totals_when_google_gives_none():
    s = summarize(
        ReviewSet(reviews=(review(5, 1), review(4, 2), review(None, 3))),
        ProfileHours(has_regular_hours=False),
        (),
        TODAY,
    )
    assert s.review_count == 3
    assert s.average_rating == 4.5
    assert s.google_changed is False


def test_summary_with_empty_lists():
    s = summarize(ReviewSet(reviews=()), ProfileHours(has_regular_hours=False), (), TODAY)
    assert s.review_count == 0
    assert s.average_rating is None
    assert s.unanswered_count == 0
    assert s.unanswered_low_count == 0
    assert s.last_review_at is None
    assert s.has_regular_hours is False
    assert s.upcoming_special_hours == 0
    assert s.google_changed is False
    assert s.google_changed_fields == ()


def test_summary_counts_only_future_special_days_once_each():
    hours = ProfileHours(
        has_regular_hours=True,
        special_dates=(
            date(2026, 11, 10),  # yesterday: gone
            date(2026, 11, 11),  # today counts
            date(2026, 12, 25),
            date(2026, 12, 25),  # split shift on the same day: one day
            date(2027, 1, 1),
        ),
    )
    s = summarize(ReviewSet(reviews=()), hours, (), TODAY)
    assert s.upcoming_special_hours == 3


# --- facts of the profile -------------------------------------------------------


def _details(**changes):
    values = {
        "hours": ProfileHours(True, (date(2027, 1, 1), date(2026, 12, 25), date(2026, 12, 25))),
        "title": "Bar La Parra",
        "address": "Calle Elvira 1, 18010 Granada",
        "category": "Bar",
        "additional_categories": 1,
        "description_length": 300,
        "has_phone": True,
        "has_website": True,
        "service_count": 2,
        "is_open": True,
        "place_id": "ChIJ-ficha",
        "maps_url": "https://maps.google.com/?cid=42",
    }
    return ProfileDetails(**{**values, **changes})


def test_facts_keep_upcoming_special_dates_sorted_and_unique():
    hours = ProfileHours(True, (date(2027, 1, 1), TODAY, date(2020, 1, 1), date(2027, 1, 1)))
    photo = datetime(2026, 6, 15, 9, 30, tzinfo=UTC)
    facts = collect_facts(_details(hours=hours), MediaSummary(7, photo), 4, TODAY)
    assert facts.special_dates == (TODAY, date(2027, 1, 1))
    assert (facts.photo_count, facts.latest_photo_at, facts.attribute_count) == (7, photo, 4)
    assert facts.title == "Bar La Parra" and facts.description_length == 300


def test_facts_of_reads_google_refused_are_unknown_not_zero():
    facts = collect_facts(_details(), None, None, TODAY)
    assert (facts.photo_count, facts.latest_photo_at, facts.attribute_count) == (None, None, None)


def test_facts_survive_the_trip_through_json():
    photo = datetime(2026, 6, 15, 9, 30, tzinfo=UTC)
    for facts in (
        collect_facts(_details(), MediaSummary(7, photo), 4, TODAY),
        collect_facts(_details(), None, None, TODAY),
    ):
        raw = json.loads(json.dumps(facts_to_json(facts)))
        assert raw["v"] == 1
        assert facts_from_json(raw) == facts


def test_stored_facts_hold_no_text_of_the_description_nor_of_reviews():
    raw = facts_to_json(collect_facts(_details(), MediaSummary(1), 0, TODAY))
    assert set(raw) == {
        "v",
        "title",
        "address",
        "category",
        "additional_categories",
        "description_length",
        "has_phone",
        "has_website",
        "service_count",
        "is_open",
        "place_id",
        "maps_url",
        "special_dates",
        "region",
        "regular_hours",
        "photo_count",
        "latest_photo_at",
        "attribute_count",
    }


def test_facts_of_before_the_key_dates_still_read_without_region_nor_hours():
    raw = facts_to_json(collect_facts(_details(), MediaSummary(1), 0, TODAY))
    del raw["region"], raw["regular_hours"]
    facts = facts_from_json(raw)
    assert facts is not None and facts.region == "" and facts.regular_hours == ()
    for bad in ({"region": "andalucia"}, {"regular_hours": [[7, "09:00", "14:00"]]}):
        assert facts_from_json({**raw, **bad}) is None


def test_missing_malformed_or_newer_facts_read_as_none():
    good = facts_to_json(collect_facts(_details(), MediaSummary(1), 0, TODAY))
    assert facts_from_json(good) is not None
    for bad in (
        None,
        "texto",
        [],
        {},
        {**good, "v": 2},
        {k: v for k, v in good.items() if k != "has_phone"},
        {**good, "photo_count": "muchas"},
        {**good, "photo_count": -1},
        {**good, "photo_count": True},
        {**good, "has_website": 1},
        {**good, "special_dates": ["ayer"]},
        {**good, "special_dates": "2026-12-25"},
        {**good, "latest_photo_at": "nunca"},
    ):
        assert facts_from_json(bad) is None
