from fastapi.testclient import TestClient

from app.adapters.places.fake import FakePlaces
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


def test_repeated_query_is_served_from_cache():
    client, places = make()
    client.post(URL, json={"query": "Demo  BAR"})
    client.post(URL, json={"query": "demo bar"})
    assert places.calls == 1


def test_rate_limit_per_client_applies_to_uncached_queries():
    client, places = make(diagnosis_rate_limit=2)
    assert client.post(URL, json={"query": "demo uno"}).status_code == 200
    assert client.post(URL, json={"query": "demo dos"}).status_code == 200
    r = client.post(URL, json={"query": "demo tres"})
    assert r.status_code == 429
    assert places.calls == 2
    assert client.post(URL, json={"query": "demo uno"}).status_code == 200  # cached, still served


def test_app_refuses_to_start_without_a_places_source():
    import pytest

    with pytest.raises(RuntimeError):
        create_app(Settings(_env_file=None))
