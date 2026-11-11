"""Pure rules of the worker: job retry policy and the profile summary."""

from datetime import UTC, date, datetime

from app.domain.jobs import Job, PermanentJobError, backoff_seconds, retry_delay, safe_error
from app.domain.profile_summary import (
    ProfileHours,
    Review,
    ReviewSet,
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
