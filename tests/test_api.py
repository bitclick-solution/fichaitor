from dataclasses import replace

from fastapi.testclient import TestClient

from app.adapters.places.fake import DEMO, FakePlaces
from app.adapters.store.memory import MemoryStore
from app.api.main import create_app
from app.config import Settings

URL = "/api/v1/public/diagnosis"


def make(places: FakePlaces | None = None, **overrides) -> tuple[TestClient, FakePlaces]:
    places = places or FakePlaces()
    settings = Settings(_env_file=None, **overrides)
    return TestClient(create_app(settings, places, MemoryStore())), places


def test_healthz():
    client, _ = make()
    assert client.get("/healthz").json() == {"status": "ok"}


def test_diagnosis_returns_score_findings_and_locked_checks():
    client, _ = make()
    r = client.post(URL, json={"query": "Demo bar Granada"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"].startswith("Bar La Parra")
    assert 0 <= body["score"] <= 100
    assert 1 <= len(body["findings"]) <= 5
    assert {"code", "title", "detail", "impact"} == set(body["findings"][0])
    assert "penalty" not in body["findings"][0]
    assert len(body["locked_checks"]) == 4
    assert body["attribution"] == "Google Maps"


def test_unknown_business_returns_404_in_spanish():
    client, _ = make()
    r = client.post(URL, json={"query": "negocio que no existe"})
    assert r.status_code == 404
    assert "No encontramos" in r.json()["detail"]


def test_query_is_validated():
    client, _ = make()
    assert client.post(URL, json={"query": "ab"}).status_code == 422
    assert client.post(URL, json={"query": "x" * 121}).status_code == 422
    assert client.post(URL, json={}).status_code == 422


def test_provider_failure_returns_503_without_leaking_details():
    client, _ = make(FakePlaces(fail=True))
    r = client.post(URL, json={"query": "demo"})
    assert r.status_code == 503
    assert "fallo simulado" not in r.text


def test_repeated_query_reuses_the_place_id_but_asks_google_for_the_content_again():
    client, places = make()
    first = client.post(URL, json={"query": "Demo  BAR"})
    second = client.post(URL, json={"query": "demo bar"})
    assert first.json() == second.json()
    assert places.calls == 1  # one text search: the identifier is remembered
    assert places.id_calls == [DEMO.place_id, DEMO.place_id]  # the content is not


def test_content_changed_at_google_shows_up_in_a_repeated_query():
    places = FakePlaces()
    client, _ = make(places)
    assert client.post(URL, json={"query": "demo bar"}).json()["name"] == DEMO.name
    places._places["demo"] = replace(DEMO, name="Bar La Parra, nuevo rótulo", photo_count=0)
    body = client.post(URL, json={"query": "demo bar"}).json()
    assert body["name"] == "Bar La Parra, nuevo rótulo"
    assert "no_photos" in [f["code"] for f in body["findings"]]
    assert places.calls == 1


def test_remembered_place_id_expires():
    client, places = make(place_id_cache_seconds=0)
    client.post(URL, json={"query": "demo bar"})
    client.post(URL, json={"query": "demo bar"})
    assert places.calls == 2


def test_place_id_that_google_no_longer_knows_is_searched_again_and_then_not_found():
    places = FakePlaces()
    client, _ = make(places)
    assert client.post(URL, json={"query": "demo bar"}).status_code == 200
    places._places.clear()  # the business is gone from Google
    r = client.post(URL, json={"query": "demo bar"})
    assert r.status_code == 404
    assert "No encontramos" in r.json()["detail"]
    assert places.calls == 2  # the stale identifier was dropped and the text searched again


def test_rate_limit_per_client_counts_every_diagnosis_repeated_ones_included():
    client, places = make(diagnosis_rate_limit=2)
    assert client.post(URL, json={"query": "demo uno"}).status_code == 200
    assert client.post(URL, json={"query": "demo uno"}).status_code == 200
    r = client.post(URL, json={"query": "demo uno"})
    assert r.status_code == 429
    assert isinstance(r.json()["detail"], str)
    assert (places.calls, len(places.id_calls)) == (1, 2)  # the third never reached Google


def test_default_rate_limit_is_ten_per_hour():
    settings = Settings(_env_file=None)
    assert (settings.diagnosis_rate_limit, settings.diagnosis_rate_window_seconds) == (10, 3600)
    assert settings.place_id_cache_seconds == 30 * 86400


def test_app_refuses_to_start_without_a_places_source():
    import pytest

    with pytest.raises(RuntimeError):
        create_app(Settings(_env_file=None))
