"""The diagnosis against Google in two steps, over a fake transport. Nothing reaches Google.

Step 1: Text Search asking only for the identifier (free SKU). Step 2: Place Details
with the full mask. Between requests only "search text -> place_id" may be kept.
"""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters.places.google import FIELDS, SERVICE_FLAGS, GooglePlaces
from app.adapters.store.memory import MemoryStore
from app.api.main import create_app
from app.application.diagnosis_service import DiagnosisService
from app.config import Settings
from app.ports.places import PlacesUnavailable

PLACE_ID = "ChIJ-abc"
SEARCH_PATH = "/v1/places:searchText"
DETAILS_PATH = f"/v1/places/{PLACE_ID}"
URL = "/api/v1/public/diagnosis"


def details(**changes) -> dict:
    return {
        "id": PLACE_ID,
        "displayName": {"text": "Bar La Parra"},
        "formattedAddress": "Calle Real 1, Granada",
        "businessStatus": "OPERATIONAL",
        "rating": 4.4,
        "userRatingCount": 57,
        "regularOpeningHours": {"periods": [{"open": {"day": 1}}]},
        "photos": [{"name": f"f{n}"} for n in range(10)],
        "websiteUri": "https://bar.example",
        "nationalPhoneNumber": "958 00 00 00",
        "primaryType": "bar",
        "primaryTypeDisplayName": {"text": "Bar"},
        "types": ["bar", "restaurant", "establishment"],
        "outdoorSeating": True,
        "googleMapsUri": "https://maps.google.com/?cid=1",
    } | changes


class Google:
    """Fake Places API: counts the requests to each endpoint."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.search_status = 200
        self.search_body: dict = {"places": [{"id": PLACE_ID}]}
        self.details_status = 200
        self.details_body = details()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST" and request.url.path == SEARCH_PATH:
            return httpx.Response(self.search_status, json=self.search_body)
        if request.method == "GET" and request.url.path.startswith("/v1/places/"):
            return httpx.Response(self.details_status, json=self.details_body)
        return httpx.Response(418)

    @property
    def searches(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == SEARCH_PATH]

    @property
    def lookups(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "GET"]

    def counts(self) -> tuple[int, int]:
        return len(self.searches), len(self.lookups)

    def gateway(self) -> GooglePlaces:
        return GooglePlaces(
            "clave-de-pruebas", httpx.AsyncClient(transport=httpx.MockTransport(self))
        )


def api(google: Google, **overrides) -> TestClient:
    settings = Settings(_env_file=None, **overrides)
    return TestClient(create_app(settings, google.gateway(), MemoryStore()))


# --- adapter ------------------------------------------------------------------


def test_search_asks_only_for_the_identifier():
    google = Google()
    assert asyncio.run(google.gateway().find_id("bar la parra granada")) == PLACE_ID
    (request,) = google.requests
    assert request.method == "POST"
    assert (request.url.host, request.url.path) == ("places.googleapis.com", SEARCH_PATH)
    # Exactly this mask: any other field moves the request to a paid SKU.
    assert request.headers["X-Goog-FieldMask"] == "places.id"
    assert request.headers["X-Goog-Api-Key"] == "clave-de-pruebas"
    assert "clave-de-pruebas" not in str(request.url)
    body = json.loads(request.content)
    assert body == {
        "textQuery": "bar la parra granada",
        "languageCode": "es",
        "regionCode": "ES",
        "maxResultCount": 1,
    }


def test_search_without_results_is_none():
    google = Google()
    for body in ({}, {"places": []}, {"places": [{}]}):
        google.search_body = body
        assert asyncio.run(google.gateway().find_id("nada")) is None


def test_search_reports_provider_failures_as_unavailable():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lento")

    for handler in (lambda _: httpx.Response(500), lambda _: httpx.Response(429), boom):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(PlacesUnavailable):
            asyncio.run(GooglePlaces("k", client).find_id("bar"))


def test_details_mask_keeps_every_field_the_scoring_rules_need():
    needed = {
        "id",
        "displayName",
        "formattedAddress",
        "businessStatus",  # not_operational
        "regularOpeningHours.periods",  # no_hours
        "photos.name",  # no_photos, few_photos, some_photos
        "rating",  # low_rating
        "userRatingCount",  # few_reviews
        "websiteUri",  # no_website
        "nationalPhoneNumber",  # no_phone
        "primaryType",  # no_secondary_categories, no_service_attributes
        "primaryTypeDisplayName",
        "types",  # no_secondary_categories
        "googleMapsUri",
        *SERVICE_FLAGS,  # no_service_attributes
    }
    assert set(FIELDS) == needed
    assert not any(f.startswith("reviews") for f in FIELDS)  # review dates are not judged


# --- one diagnosis ------------------------------------------------------------


def test_one_diagnosis_is_one_id_search_and_one_details_request():
    google = Google()
    r = api(google).post(URL, json={"query": "Bar La Parra Granada"})
    assert r.status_code == 200
    assert google.counts() == (1, 1)
    search, lookup = google.requests
    assert search.headers["X-Goog-FieldMask"] == "places.id"
    assert lookup.url.path == DETAILS_PATH
    assert lookup.headers["X-Goog-FieldMask"].split(",") == list(FIELDS)
    body = r.json()
    assert (body["place_id"], body["name"]) == (PLACE_ID, "Bar La Parra")
    assert body["score"] == 100
    assert body["attribution"] == "Google Maps"


def test_no_scoring_rule_is_lost_with_the_two_steps():
    google = Google()
    google.details_body = {
        "id": PLACE_ID,
        "businessStatus": "CLOSED_TEMPORARILY",
        "primaryType": "bar",
        "types": ["bar", "establishment"],
        "rating": 3.1,
        "userRatingCount": 4,
    }
    gateway = google.gateway()
    service = DiagnosisService(
        gateway, rate_limit=5, rate_window_seconds=60, place_id_cache_seconds=60
    )
    place = asyncio.run(gateway.get(PLACE_ID))
    assert (place.operational, place.has_regular_hours, place.photo_count) == (False, False, 0)
    assert (place.rating, place.review_count, place.website, place.phone) == (3.1, 4, "", "")
    assert place.service_attributes == 0
    result = asyncio.run(service.run("bar", "cliente"))
    assert result.score == 0  # every rule fired: 30+20+15+10+10+8+8+6+5 = 112
    assert [f.code for f in result.findings][0] == "not_operational"


# --- what is kept between requests -------------------------------------------


def test_repeated_search_reuses_the_place_id_and_asks_for_the_content_again():
    google = Google()
    client = api(google)

    first = client.post(URL, json={"query": "Bar La Parra  GRANADA"}).json()
    assert google.counts() == (1, 1)

    # The profile changes at Google between the two diagnoses.
    google.details_body = details(
        displayName={"text": "Bar La Parra (nuevo rótulo)"},
        formattedAddress="Plaza Nueva 2, Granada",
        photos=[],
        websiteUri="",
    )
    second = client.post(URL, json={"query": "bar la parra granada"}).json()

    assert google.counts() == (1, 2)  # no second search: the place_id was reused
    assert google.lookups[1].url.path == DETAILS_PATH  # the details were asked for again
    assert first["place_id"] == second["place_id"] == PLACE_ID
    assert first["name"] == "Bar La Parra" and first["score"] == 100
    assert second["name"] == "Bar La Parra (nuevo rótulo)"
    assert second["address"] == "Plaza Nueva 2, Granada"
    assert {"no_photos", "no_website"} <= {f["code"] for f in second["findings"]}
    assert second["score"] == 100 - 15 - 8


def test_the_service_keeps_only_search_text_and_place_id():
    google = Google()
    service = DiagnosisService(
        google.gateway(), rate_limit=5, rate_window_seconds=60, place_id_cache_seconds=60
    )
    asyncio.run(service.run("Bar La Parra Granada", "203.0.113.7"))
    kept = {k: v for k, v in vars(service).items() if k not in ("_places", "_clock")}
    assert service._place_ids["bar la parra granada"][1] == PLACE_ID
    text = repr(kept)
    for content in ("Bar La Parra", "Calle Real", "958 00", "bar.example", "maps.google", "4.4"):
        assert content not in text


def test_a_different_text_for_the_same_business_searches_again():
    google = Google()
    client = api(google)
    client.post(URL, json={"query": "bar la parra granada"})
    client.post(URL, json={"query": "la parra, granada"})
    assert google.counts() == (2, 2)


def test_not_found_is_not_remembered():
    google = Google()
    google.search_body = {}
    client = api(google)
    assert client.post(URL, json={"query": "negocio fantasma"}).status_code == 404
    assert google.counts() == (1, 0)
    google.search_body = {"places": [{"id": PLACE_ID}]}
    assert client.post(URL, json={"query": "negocio fantasma"}).status_code == 200


def test_remembered_searches_are_bounded(monkeypatch):
    monkeypatch.setattr("app.application.diagnosis_service.MAX_REMEMBERED_SEARCHES", 2)
    google = Google()
    service = DiagnosisService(
        google.gateway(), rate_limit=50, rate_window_seconds=60, place_id_cache_seconds=60
    )
    for text in ("uno", "dos", "tres"):
        asyncio.run(service.run(text, "c"))
    assert list(service._place_ids) == ["dos", "tres"]


# --- errors -------------------------------------------------------------------


@pytest.mark.parametrize("step", ["search", "details"])
@pytest.mark.parametrize("status", [403, 429, 500])
def test_google_errors_in_either_step_are_503_in_spanish_without_details(step, status):
    google = Google()
    setattr(google, f"{step}_status", status)
    r = api(google).post(URL, json={"query": "bar la parra"})
    assert r.status_code == 503
    assert r.json() == {"detail": "Google no responde ahora mismo. Prueba en un minuto."}
    assert str(status) not in r.text
    assert google.counts() == ((1, 0) if step == "search" else (1, 1))


@pytest.mark.parametrize("status", [400, 404])
def test_place_id_that_step_two_does_not_recognise_is_not_found(status):
    google = Google()
    google.details_status = status
    client = api(google)
    r = client.post(URL, json={"query": "bar la parra"})
    assert r.status_code == 404
    assert "No encontramos" in r.json()["detail"]
    assert google.counts() == (1, 1)
    assert client.post(URL, json={"query": "bar la parra"}).status_code == 404
    assert google.counts() == (2, 2)  # an identifier Google rejected is not remembered


def test_remembered_place_id_retired_by_google_is_searched_again():
    google = Google()
    client = api(google)
    assert client.post(URL, json={"query": "bar la parra"}).status_code == 200
    google.details_status = 404
    assert client.post(URL, json={"query": "bar la parra"}).status_code == 404
    assert google.counts() == (2, 3)  # details with the old id, new search, details again


def test_rate_limit_stops_requests_before_they_reach_google():
    google = Google()
    client = api(google, diagnosis_rate_limit=2)
    for _ in range(2):
        assert client.post(URL, json={"query": "bar la parra"}).status_code == 200
    assert client.post(URL, json={"query": "bar la parra"}).status_code == 429
    assert google.counts() == (1, 2)


def test_daily_cap_for_all_clients_together_stops_requests_before_google():
    # M2 (docs/seguridad/revision-2026-11-12.md): rotating addresses goes round the
    # limit per IP, not round this one.
    google = Google()
    settings = Settings(_env_file=None, diagnosis_rate_limit=100, diagnosis_daily_limit=2)
    app = create_app(settings, google.gateway(), MemoryStore())
    ips = iter(f"198.51.100.{n}" for n in range(1, 10))

    def post():
        client = TestClient(app, client=(next(ips), 50000))
        return client.post(URL, json={"query": "bar la parra"})

    assert post().status_code == 200
    assert post().status_code == 200
    r = post()
    assert r.status_code == 503
    assert r.json() == {
        "detail": "Hoy hemos recibido muchas consultas y no podemos hacer más. Prueba mañana."
    }
    assert google.counts() == (1, 2)  # the third one never reached Google


def test_the_daily_cap_of_the_diagnosis_is_configurable_and_bounded_by_default():
    assert Settings(_env_file=None).diagnosis_daily_limit == 500
