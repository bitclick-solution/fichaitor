"""Google Places adapter against a fake transport. Nothing here reaches Google."""

import asyncio

import httpx
import pytest

from app.adapters.places.google import FIELDS, GooglePlaces
from app.ports.places import PlacesUnavailable

RAW = {
    "id": "ChIJ-abc",
    "displayName": {"text": "Bar La Parra"},
    "formattedAddress": "Calle Real 1, Granada",
    "googleMapsUri": "https://maps.google.com/?cid=1",
}


def gateway(handler) -> GooglePlaces:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GooglePlaces("clave-de-pruebas", client)


def test_get_asks_for_the_place_by_id_with_the_unprefixed_field_mask():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=RAW)

    place = asyncio.run(gateway(handler).get("ChIJ-abc"))
    assert place is not None
    assert (place.place_id, place.name) == ("ChIJ-abc", "Bar La Parra")
    (request,) = seen
    assert request.method == "GET"
    assert request.url.path == "/v1/places/ChIJ-abc"
    assert request.url.host == "places.googleapis.com"
    assert request.url.params["languageCode"] == "es"
    assert request.headers["X-Goog-Api-Key"] == "clave-de-pruebas"
    mask = request.headers["X-Goog-FieldMask"].split(",")
    assert mask == list(FIELDS)
    assert not any(field.startswith("places.") for field in mask)
    assert "clave-de-pruebas" not in str(request.url)


def test_get_cannot_be_steered_to_another_path_by_the_identifier():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404, json={})

    assert asyncio.run(gateway(handler).get("../places:searchText?x=1")) is None
    assert seen[0].url.raw_path.startswith(b"/v1/places/..%2Fplaces%3AsearchText%3Fx%3D1")


def test_get_returns_none_for_unknown_or_invalid_identifiers():
    for status in (400, 404):
        assert asyncio.run(gateway(lambda _, s=status: httpx.Response(s, json={})).get("x")) is None


def test_get_reports_provider_failures_as_unavailable():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin red")

    for handler in (lambda _: httpx.Response(500), lambda _: httpx.Response(403), boom):
        with pytest.raises(PlacesUnavailable):
            asyncio.run(gateway(handler).get("ChIJ-abc"))
