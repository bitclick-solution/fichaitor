"""Special hours in the Google adapter of the worker: the read just before writing and
the PATCH with updateMask=specialHours. Against a mocked HTTP transport: no real call to
Google is ever made here."""

import asyncio
import json

import httpx
import pytest

from app.adapters.google_profile.real import RealGoogleProfile
from app.ports.google_profile import GoogleProfileError, GoogleRejected, ReconnectRequired

ACCESS = "ya29.access-token-secreto"
LOCATION = "locations/222"
URL = f"https://mybusinessbusinessinformation.googleapis.com/v1/{LOCATION}"
PERIODS = [
    {"startDate": {"year": 2027, "month": 8, "day": 15}, "closed": True},
    {"startDate": {"year": 2026, "month": 12, "day": 8}, "closed": True},
]


def run(coro):
    return asyncio.run(coro)


def make(handler):
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return RealGoogleProfile("id", "secret", client=client, retry_delay=0), seen


def test_hours_reads_regular_and_special_hours_only():
    body = {
        "regularHours": {"periods": [{"openDay": "MONDAY", "openTime": {"hours": 9}}]},
        "specialHours": {"specialHourPeriods": PERIODS},
    }
    google, seen = make(lambda r: httpx.Response(200, json=body))
    hours = run(google.hours(ACCESS, LOCATION))
    assert hours.regular == ({"openDay": "MONDAY", "openTime": {"hours": 9}},)
    assert list(hours.special) == PERIODS
    (request,) = seen
    assert request.method == "GET" and str(request.url).startswith(URL)
    assert request.url.params["readMask"] == "regularHours,specialHours"
    assert request.headers["Authorization"] == f"Bearer {ACCESS}"


def test_hours_of_a_profile_without_any():
    google, _ = make(lambda r: httpx.Response(200, json={}))
    hours = run(google.hours(ACCESS, LOCATION))
    assert hours.regular == () and hours.special == ()


def test_too_many_special_periods_are_not_trusted():
    many = {"specialHours": {"specialHourPeriods": [PERIODS[0]] * 501}}
    google, _ = make(lambda r: httpx.Response(200, json=many))
    with pytest.raises(GoogleRejected):
        run(google.hours(ACCESS, LOCATION))


def test_the_write_is_a_patch_of_special_hours_only():
    google, seen = make(lambda r: httpx.Response(200, json={"name": LOCATION}))
    run(google.set_special_hours(ACCESS, LOCATION, PERIODS))
    (request,) = seen
    assert request.method == "PATCH"
    assert str(request.url) == f"{URL}?updateMask=specialHours"
    assert json.loads(request.content) == {"specialHours": {"specialHourPeriods": PERIODS}}
    assert request.headers["Authorization"] == f"Bearer {ACCESS}"


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, ReconnectRequired),
        (400, GoogleRejected),
        (403, GoogleRejected),
        (404, GoogleRejected),
        (429, GoogleProfileError),
        (503, GoogleProfileError),
    ],
)
def test_write_errors_carry_only_the_status(status, error):
    answer = {"error": {"message": f"echo {ACCESS} y {PERIODS}"}}
    google, seen = make(lambda r: httpx.Response(status, json=answer))
    with pytest.raises(error) as caught:
        run(google.set_special_hours(ACCESS, LOCATION, PERIODS))
    assert ACCESS not in str(caught.value) and "2027" not in str(caught.value)
    assert len(seen) == 1  # one request, no retry inside
    if error is not ReconnectRequired:
        assert caught.value.status == status


def test_a_network_failure_on_the_write_is_temporary():
    def fail(request):
        raise httpx.ConnectError("sin red")

    google, _ = make(fail)
    with pytest.raises(GoogleProfileError):
        run(google.set_special_hours(ACCESS, LOCATION, PERIODS))


def test_a_location_of_another_shape_never_reaches_the_url():
    google, seen = make(lambda r: httpx.Response(200, json={}))
    with pytest.raises(GoogleRejected):
        run(google.set_special_hours(ACCESS, "locations/1/../../accounts", PERIODS))
    assert not seen


@pytest.mark.parametrize(
    ("address", "region"),
    [
        ({"regionCode": "ES", "postalCode": "18010", "locality": "Granada"}, "AN"),
        ({"regionCode": "ES", "postalCode": "28013"}, "MD"),
        ({"regionCode": "PT", "postalCode": "18010"}, ""),
        ({"regionCode": "ES"}, ""),
        ({}, ""),
    ],
)
def test_the_sync_reads_the_region_from_the_postal_code(address, region):
    body = {"title": "Bar", "storefrontAddress": address}
    google, _ = make(lambda r: httpx.Response(200, json=body))
    assert run(google.details(ACCESS, LOCATION)).region == region
