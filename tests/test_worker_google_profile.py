"""Google Business Profile adapter of the worker, against a mocked HTTP transport.

No real call to Google is ever made here.
"""

import asyncio
import logging
from datetime import UTC, date, datetime

import httpx
import pytest

from app.adapters.google_profile.fake import FakeGoogleProfile
from app.adapters.google_profile.real import RealGoogleProfile
from app.ports.google_profile import GoogleProfileError, GoogleRejected, ReconnectRequired

REFRESH = "1//refresh-token-secreto"
ACCESS = "ya29.access-token-secreto"
ACCOUNT = "accounts/111"
LOCATION = "locations/222"


def run(coro):
    return asyncio.run(coro)


def make(handler) -> tuple[RealGoogleProfile, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return RealGoogleProfile("client-id", "client-secret", client=client, retry_delay=0), seen


# --- token refresh ------------------------------------------------------------


def test_refresh_posts_the_refresh_grant_and_returns_the_access_token():
    google, seen = make(lambda r: httpx.Response(200, json={"access_token": ACCESS}))
    assert run(google.refresh_access_token(REFRESH)) == ACCESS
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "https://oauth2.googleapis.com/token"
    body = request.content.decode()
    assert "grant_type=refresh_token" in body
    assert "client_id=client-id" in body and "client_secret=client-secret" in body
    assert REFRESH not in str(request.url)  # the token travels in the body, never in the URL


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (400, {"error": "invalid_grant", "error_description": "Token has been expired"}),
        (401, {"error": "unauthorized"}),
        (401, {}),
    ],
)
def test_refresh_rejected_means_reconnect_and_is_not_retried(status, body):
    google, seen = make(lambda r: httpx.Response(status, json=body))
    with pytest.raises(ReconnectRequired) as raised:
        run(google.refresh_access_token(REFRESH))
    assert len(seen) == 1
    assert REFRESH not in str(raised.value)


def test_refresh_with_wrong_client_credentials_is_our_fault_not_the_owners():
    google, seen = make(lambda r: httpx.Response(401, json={"error": "invalid_client"}))
    with pytest.raises(GoogleRejected):
        run(google.refresh_access_token(REFRESH))
    assert len(seen) == 1


def test_refresh_retries_5xx_and_then_succeeds():
    answers = iter([httpx.Response(503), httpx.Response(200, json={"access_token": ACCESS})])
    google, seen = make(lambda r: next(answers))
    assert run(google.refresh_access_token(REFRESH)) == ACCESS
    assert len(seen) == 2


def test_refresh_gives_up_after_repeated_5xx_with_a_temporary_error():
    google, seen = make(lambda r: httpx.Response(503, text=f"eco {REFRESH}"))
    with pytest.raises(GoogleProfileError) as raised:
        run(google.refresh_access_token(REFRESH))
    assert not isinstance(raised.value, GoogleRejected)
    assert len(seen) == 3
    assert REFRESH not in str(raised.value)


def test_network_errors_are_temporary_and_do_not_leak_the_token():
    def boom(request):
        raise httpx.ConnectError(f"no route, body had {REFRESH}")

    google, _ = make(boom)
    with pytest.raises(GoogleProfileError) as raised:
        run(google.refresh_access_token(REFRESH))
    assert REFRESH not in str(raised.value)
    assert raised.value.__cause__ is None  # the original exception is not chained


# --- reviews ------------------------------------------------------------------


def review(review_id, stars, created, reply=False, comment=None):
    item = {"reviewId": review_id, "starRating": stars, "createTime": created}
    if reply:
        item["reviewReply"] = {"comment": "Gracias", "updateTime": created}
    if comment is not None:
        item["comment"] = comment
    return item


def test_reviews_are_read_page_by_page():
    pages = {
        None: {
            "reviews": [
                review("a", "FIVE", "2026-10-01T10:00:00Z", reply=True),
                review(
                    "b",
                    "ONE",
                    "2026-10-20T08:30:00.123456789Z",
                    comment="(Translated by Google) Bad\n\n(Original)\nMal servicio",
                ),
            ],
            "averageRating": 4.7,
            "totalReviewCount": 3,
            "nextPageToken": "p2",
        },
        "p2": {
            "reviews": [review("c", "STAR_RATING_UNSPECIFIED", "no-es-fecha")],
            "averageRating": 4.7,
            "totalReviewCount": 3,
        },
    }

    def handler(request):
        return httpx.Response(200, json=pages[request.url.params.get("pageToken")])

    google, seen = make(handler)
    result = run(google.reviews(ACCESS, ACCOUNT, LOCATION))

    assert [r.url.path for r in seen] == ["/v4/accounts/111/locations/222/reviews"] * 2
    assert seen[0].url.host == "mybusiness.googleapis.com"
    assert seen[0].headers["authorization"] == f"Bearer {ACCESS}"
    assert "pageToken" not in seen[0].url.params
    assert result.total == 3 and result.average == 4.7
    first, second, third = result.reviews
    assert (first.rating, first.replied) == (5, True)
    assert (second.rating, second.replied, second.text) == (1, False, "Mal servicio")
    assert second.created_at == datetime(2026, 10, 20, 8, 30, 0, 123456, tzinfo=UTC)
    assert (third.rating, third.created_at) == (None, None)


def test_reviews_of_a_profile_without_reviews():
    google, _ = make(lambda r: httpx.Response(200, json={}))
    result = run(google.reviews(ACCESS, ACCOUNT, LOCATION))
    assert result.reviews == () and result.total is None and result.average is None


def test_reviews_pagination_has_an_upper_bound():
    google, seen = make(
        lambda r: httpx.Response(200, json={"reviews": [], "nextPageToken": "otra-vez"})
    )
    with pytest.raises(GoogleProfileError):
        run(google.reviews(ACCESS, ACCOUNT, LOCATION))
    assert len(seen) == 200


@pytest.mark.parametrize(
    ("account", "location"),
    [("accounts/1/../x", LOCATION), (ACCOUNT, "locations/2?x=1"), ("111", "222"), ("", "")],
)
def test_malformed_resource_names_never_reach_google(account, location):
    google, seen = make(lambda r: httpx.Response(200, json={}))
    with pytest.raises(GoogleRejected):
        run(google.reviews(ACCESS, account, location))
    assert seen == []


# --- profile and Google's own changes -----------------------------------------


def test_hours_reads_regular_and_special_hours():
    body = {
        "name": LOCATION,
        "regularHours": {"periods": [{"openDay": "MONDAY", "closeDay": "MONDAY"}]},
        "specialHours": {
            "specialHourPeriods": [
                {"startDate": {"year": 2026, "month": 12, "day": 25}, "closed": True},
                {"startDate": {"year": 2027, "month": 1, "day": 1}, "closed": True},
                {"startDate": {"year": 2027, "month": 2, "day": 31}},  # impossible date
                {"closed": True},  # no date at all
            ]
        },
    }
    google, seen = make(lambda r: httpx.Response(200, json=body))
    hours = run(google.hours(ACCESS, LOCATION))
    assert hours.has_regular_hours is True
    assert hours.special_dates == (date(2026, 12, 25), date(2027, 1, 1))
    assert seen[0].url.host == "mybusinessbusinessinformation.googleapis.com"
    assert seen[0].url.path == "/v1/locations/222"
    mask = seen[0].url.params["readMask"].split(",")
    assert "regularHours" in mask and "specialHours" in mask


def test_hours_of_a_profile_without_hours():
    google, _ = make(lambda r: httpx.Response(200, json={"name": LOCATION}))
    hours = run(google.hours(ACCESS, LOCATION))
    assert hours.has_regular_hours is False and hours.special_dates == ()


def test_google_updated_returns_the_changed_fields():
    google, seen = make(
        lambda r: httpx.Response(
            200, json={"location": {}, "diffMask": "specialHours,title", "pendingMask": ""}
        )
    )
    assert run(google.google_updated_fields(ACCESS, LOCATION)) == ("specialHours", "title")
    assert seen[0].url.path == "/v1/locations/222:getGoogleUpdated"
    assert seen[0].url.params["readMask"]


def test_google_updated_without_changes():
    google, _ = make(lambda r: httpx.Response(200, json={"location": {}}))
    assert run(google.google_updated_fields(ACCESS, LOCATION)) == ()


def test_reads_retry_the_occasional_503():
    answers = iter([httpx.Response(503), httpx.Response(503), httpx.Response(200, json={})])
    google, seen = make(lambda r: next(answers))
    run(google.hours(ACCESS, LOCATION))
    assert len(seen) == 3


def test_reads_retry_429_as_temporary():
    google, seen = make(lambda r: httpx.Response(429))
    with pytest.raises(GoogleProfileError) as raised:
        run(google.hours(ACCESS, LOCATION))
    assert not isinstance(raised.value, GoogleRejected)
    assert len(seen) == 3


def test_reads_do_not_retry_a_4xx():
    google, seen = make(lambda r: httpx.Response(403, text=f"denied {ACCESS}"))
    with pytest.raises(GoogleRejected) as raised:
        run(google.hours(ACCESS, LOCATION))
    assert len(seen) == 1
    assert ACCESS not in str(raised.value)


def test_a_401_on_a_read_means_reconnect():
    google, seen = make(lambda r: httpx.Response(401))
    with pytest.raises(ReconnectRequired):
        run(google.google_updated_fields(ACCESS, LOCATION))
    assert len(seen) == 1


def test_a_200_that_is_not_json_is_a_temporary_error():
    google, _ = make(lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(GoogleProfileError):
        run(google.hours(ACCESS, LOCATION))


def test_the_adapter_logs_no_tokens(caplog):
    caplog.set_level(logging.DEBUG)
    google, _ = make(lambda r: httpx.Response(200, json={"access_token": ACCESS}))
    run(google.refresh_access_token(REFRESH))
    run(google.hours(ACCESS, LOCATION))
    assert REFRESH not in caplog.text and ACCESS not in caplog.text


# --- fake ---------------------------------------------------------------------


def test_fake_serves_a_demo_profile_and_can_be_told_to_fail():
    fake = FakeGoogleProfile()
    access = run(fake.refresh_access_token("cualquiera"))
    reviews = run(fake.reviews(access, ACCOUNT, LOCATION))
    assert reviews.total == len(reviews.reviews) > 0
    assert run(fake.hours(access, LOCATION)).has_regular_hours
    assert isinstance(run(fake.google_updated_fields(access, LOCATION)), tuple)

    with pytest.raises(ReconnectRequired):
        run(FakeGoogleProfile(refresh_error=ReconnectRequired("x")).refresh_access_token("t"))

    flaky = FakeGoogleProfile(read_failures=1)
    with pytest.raises(GoogleProfileError):
        run(flaky.reviews(access, ACCOUNT, LOCATION))
    run(flaky.reviews(access, ACCOUNT, LOCATION))  # second try works
