"""Reviews in the Google adapter of the worker: what a sync keeps of each review, and the
write that answers one. Against a mocked HTTP transport.

No real call to Google is ever made here: a reply to a review is public and notifies
whoever wrote it.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime

import httpx
import pytest

from app.adapters.google_profile.fake import FakeGoogleProfile, demo_reviews
from app.adapters.google_profile.real import RealGoogleProfile
from app.domain.profile_summary import Review, ReviewSet
from app.ports.google_profile import GoogleProfileError, GoogleRejected, ReconnectRequired

ACCESS = "ya29.access-token-secreto"
ACCOUNT = "accounts/111"
LOCATION = "locations/222"
REVIEW_ID = "AbFvOqk-9_xYz"
REPLY_URL = f"https://mybusiness.googleapis.com/v4/{ACCOUNT}/{LOCATION}/reviews/{REVIEW_ID}/reply"
REVIEW_URL = f"https://mybusiness.googleapis.com/v4/{ACCOUNT}/{LOCATION}/reviews/{REVIEW_ID}"
TEXT = "Gracias, Marta.\nNos alegra que disfrutaras del arroz 🙂  "


def run(coro):
    return asyncio.run(coro)


def make(handler) -> tuple[RealGoogleProfile, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return RealGoogleProfile("client-id", "client-secret", client=client, retry_delay=0), seen


# --- what a sync keeps of each review --------------------


def test_reviews_keep_the_id_the_original_text_and_the_owner_reply_but_never_the_author():
    page = {
        "reviews": [
            {
                "reviewId": "rev-A",
                "name": f"{ACCOUNT}/{LOCATION}/reviews/rev-A",
                "reviewer": {
                    "displayName": "Marta Pérez",
                    "profilePhotoUrl": "https://lh3.googleusercontent.com/foto-de-marta",
                },
                "starRating": "ONE",
                "comment": "(Translated by Google) Cold food\n\n(Original)\nLa comida fría",
                "createTime": "2026-10-01T10:00:00Z",
                "updateTime": "2026-10-02T10:00:00Z",
            },
            {
                "reviewId": "rev-B",
                "reviewer": {"displayName": "Luis"},
                "starRating": "FIVE",
                "createTime": "2026-09-01T10:00:00Z",
                "reviewReply": {
                    "comment": "Gracias, Luis",
                    "updateTime": "2026-09-03T08:30:00Z",
                },
            },
        ],
        "totalReviewCount": 2,
        "averageRating": 3.0,
    }
    google, _ = make(lambda r: httpx.Response(200, json=page))
    found = run(google.reviews(ACCESS, ACCOUNT, LOCATION))

    first, second = found.reviews
    assert first.review_id == "rev-A"
    assert first.rating == 1
    assert first.text == "La comida fría"  # the original only, not the translation
    assert first.replied is False and first.reply_text is None and first.reply_at is None
    assert second.review_id == "rev-B"
    assert second.replied is True
    assert second.reply_text == "Gracias, Luis"
    assert second.reply_at == datetime(2026, 9, 3, 8, 30, tzinfo=UTC)
    # Data minimisation: nothing about who wrote the review is kept anywhere.
    assert "Marta" not in repr(found) and "Luis," not in repr(first)
    assert "foto-de-marta" not in repr(found)
    assert not any("author" in name or "reviewer" in name for name in Review.__dataclass_fields__)


def test_a_review_without_an_id_is_read_but_cannot_be_addressed():
    page = {"reviews": [{"starRating": "FOUR", "createTime": "2026-09-01T10:00:00Z"}]}
    google, _ = make(lambda r: httpx.Response(200, json=page))
    (review,) = run(google.reviews(ACCESS, ACCOUNT, LOCATION)).reviews
    assert review.review_id == "" and review.rating == 4


# --- the write --------------------


def test_reply_puts_the_comment_exactly_as_given():
    google, seen = make(
        lambda r: httpx.Response(
            200, json={"comment": TEXT, "updateTime": "2026-11-12T10:00:00.5Z"}
        )
    )
    updated = run(google.reply_to_review(ACCESS, ACCOUNT, LOCATION, REVIEW_ID, TEXT))

    (request,) = seen
    assert request.method == "PUT"
    assert str(request.url) == REPLY_URL
    assert request.headers["authorization"] == f"Bearer {ACCESS}"
    assert json.loads(request.content) == {"comment": TEXT}  # byte for byte: nothing trimmed
    assert ACCESS not in str(request.url)
    assert updated == datetime(2026, 11, 12, 10, 0, 0, 500000, tzinfo=UTC)


def test_reply_without_an_update_time_in_the_answer_still_succeeds():
    google, _ = make(lambda r: httpx.Response(200, json={}))
    assert run(google.reply_to_review(ACCESS, ACCOUNT, LOCATION, REVIEW_ID, TEXT)) is None


@pytest.mark.parametrize("status", [500, 502, 503, 429])
def test_reply_is_sent_once_and_a_temporary_failure_is_left_to_the_job(status):
    # No retry inside the adapter: a write that may or may not have arrived is retried
    # by the job, which first checks what Google has.
    google, seen = make(lambda r: httpx.Response(status, text=f"eco {TEXT} {ACCESS}"))
    with pytest.raises(GoogleProfileError) as raised:
        run(google.reply_to_review(ACCESS, ACCOUNT, LOCATION, REVIEW_ID, TEXT))
    assert not isinstance(raised.value, GoogleRejected)
    assert raised.value.status == status
    assert len(seen) == 1
    assert TEXT not in str(raised.value) and ACCESS not in str(raised.value)


@pytest.mark.parametrize("status", [400, 403, 404, 409])
def test_reply_rejected_by_google_is_not_retried_and_carries_only_the_status(status):
    google, seen = make(
        lambda r: httpx.Response(status, json={"error": {"message": f"bad {TEXT}"}})
    )
    with pytest.raises(GoogleRejected) as raised:
        run(google.reply_to_review(ACCESS, ACCOUNT, LOCATION, REVIEW_ID, TEXT))
    assert raised.value.status == status
    assert len(seen) == 1
    assert TEXT not in str(raised.value)


def test_a_401_on_the_reply_means_reconnect():
    google, seen = make(lambda r: httpx.Response(401))
    with pytest.raises(ReconnectRequired):
        run(google.reply_to_review(ACCESS, ACCOUNT, LOCATION, REVIEW_ID, TEXT))
    assert len(seen) == 1


def test_a_network_error_on_the_reply_is_temporary_and_quotes_nothing():
    def boom(request):
        raise httpx.ReadTimeout(f"timeout sending {TEXT} with {ACCESS}")

    google, _ = make(boom)
    with pytest.raises(GoogleProfileError) as raised:
        run(google.reply_to_review(ACCESS, ACCOUNT, LOCATION, REVIEW_ID, TEXT))
    assert not isinstance(raised.value, GoogleRejected)
    assert TEXT not in str(raised.value) and ACCESS not in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    ("account", "location", "review_id"),
    [
        ("accounts/1/../2", LOCATION, REVIEW_ID),
        (ACCOUNT, "locations/2?x=1", REVIEW_ID),
        (ACCOUNT, LOCATION, "abc/reply/../../x"),
        (ACCOUNT, LOCATION, "abc?updateMask=x"),
        (ACCOUNT, LOCATION, ""),
        (ACCOUNT, LOCATION, "a b"),
    ],
)
def test_malformed_names_never_reach_google_on_a_write(account, location, review_id):
    google, seen = make(lambda r: httpx.Response(200, json={}))
    with pytest.raises(GoogleRejected):
        run(google.reply_to_review(ACCESS, account, location, review_id, TEXT))
    with pytest.raises(GoogleRejected):
        run(google.current_reply(ACCESS, account, location, review_id))
    assert seen == []


# --- reading back one review (what a retry checks before writing again) ------------


def test_current_reply_reads_the_reply_of_one_review():
    body = {"reviewId": REVIEW_ID, "reviewReply": {"comment": TEXT, "updateTime": "x"}}
    google, seen = make(lambda r: httpx.Response(200, json=body))
    assert run(google.current_reply(ACCESS, ACCOUNT, LOCATION, REVIEW_ID)) == TEXT
    assert seen[0].method == "GET" and str(seen[0].url) == REVIEW_URL


def test_current_reply_of_an_unanswered_review_is_none():
    google, _ = make(lambda r: httpx.Response(200, json={"reviewId": REVIEW_ID}))
    assert run(google.current_reply(ACCESS, ACCOUNT, LOCATION, REVIEW_ID)) is None


def test_current_reply_of_a_review_that_is_gone_is_a_rejection_with_its_status():
    google, _ = make(lambda r: httpx.Response(404))
    with pytest.raises(GoogleRejected) as raised:
        run(google.current_reply(ACCESS, ACCOUNT, LOCATION, REVIEW_ID))
    assert raised.value.status == 404


def test_the_write_logs_neither_the_token_nor_the_text(caplog):
    caplog.set_level(logging.DEBUG)
    google, _ = make(lambda r: httpx.Response(200, json={}))
    run(google.reply_to_review(ACCESS, ACCOUNT, LOCATION, REVIEW_ID, TEXT))
    assert ACCESS not in caplog.text and "Marta" not in caplog.text


# --- the fake --------------------


def test_the_demo_profile_has_reviews_with_ids_and_texts():
    reviews = demo_reviews().reviews
    assert len({r.review_id for r in reviews}) == len(reviews)
    assert all(r.review_id for r in reviews)
    assert any(r.text for r in reviews) and any(not r.text for r in reviews)
    assert all((r.reply_text is not None) == r.replied for r in reviews)


def test_the_fake_remembers_a_reply_and_shows_it_in_the_next_read():
    async def scenario():
        google = FakeGoogleProfile()
        before = await google.reviews(ACCESS, ACCOUNT, LOCATION)
        target = next(r for r in before.reviews if not r.replied)
        assert await google.current_reply(ACCESS, ACCOUNT, LOCATION, target.review_id) is None
        await google.reply_to_review(ACCESS, ACCOUNT, LOCATION, target.review_id, TEXT)
        after = await google.reviews(ACCESS, ACCOUNT, LOCATION)
        other = await google.reviews(ACCESS, ACCOUNT, "locations/999")
        return google, target, after, other

    google, target, after, other = run(scenario())
    answered = next(r for r in after.reviews if r.review_id == target.review_id)
    assert answered.replied and answered.reply_text == TEXT
    assert google.replies == [(ACCOUNT, LOCATION, target.review_id, TEXT)]
    # Another profile of the same fake is not touched.
    assert not next(r for r in other.reviews if r.review_id == target.review_id).replied


def test_the_fake_can_be_told_to_fail_the_write():
    one = ReviewSet((Review(5, None, False, review_id="r1"),))

    async def scenario(reviews=one, **options):
        google = FakeGoogleProfile(reviews=reviews, **options)
        await google.reply_to_review(ACCESS, ACCOUNT, LOCATION, "r1", TEXT)

    with pytest.raises(GoogleRejected):
        run(scenario(reply_error=GoogleRejected("no", status=400)))
    with pytest.raises(GoogleRejected) as gone:
        run(scenario(reviews=ReviewSet(())))
    assert gone.value.status == 404
