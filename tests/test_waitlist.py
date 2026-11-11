import pytest
from fastapi.testclient import TestClient

from app.adapters.places.fake import FakePlaces
from app.adapters.store.memory import MemoryStore
from app.api.main import create_app
from app.config import Settings
from app.domain.contact import InvalidContact, normalize_email, normalize_phone

URL = "/api/v1/public/waitlist"
OK = {"channel": "whatsapp", "contact": "600 11 22 33", "consent": True, "consent_version": "v1"}


def make(store: MemoryStore | None = None, **overrides) -> tuple[TestClient, MemoryStore]:
    store = store or MemoryStore()
    app = create_app(Settings(_env_file=None, **overrides), FakePlaces(), store)
    return TestClient(app), store


# --- contact normalization -------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("600112233", "+34600112233"),
        ("600 11 22 33", "+34600112233"),
        ("+34 600-112-233", "+34600112233"),
        ("0034600112233", "+34600112233"),
        ("+44 7700 900123", "+447700900123"),
    ],
)
def test_phone_is_normalized_to_e164(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["12345", "958112233", "hola", "+34", "600112233x"])
def test_invalid_phones_are_rejected(raw):
    with pytest.raises(InvalidContact):
        normalize_phone(raw)


def test_email_is_lowercased_and_validated():
    assert normalize_email("  Dueno@Bar.ES ") == "dueno@bar.es"
    for bad in ["sin-arroba", "a@b", "a b@c.es", "@c.es"]:
        with pytest.raises(InvalidContact):
            normalize_email(bad)


# --- endpoint ---------------------------------------------------------------


def test_signup_is_stored_normalized_with_consent_version():
    client, store = make()
    body = OK | {"place_id": "p1", "business_query": "Bar", "score": 62, "source": "tiktok_v1"}
    r = client.post(URL, json=body)
    assert r.status_code == 201
    assert r.json() == {"status": "ok"}
    saved = store.signups[("whatsapp", "+34600112233")]
    assert saved.consent_version == "v1"
    assert saved.score == 62
    assert saved.place_id == "p1"
    assert ("signup", "tiktok_v1", "p1", 62) in store.events


def test_business_name_stored_is_what_the_owner_typed_never_the_one_from_places():
    client, store = make()
    typed = {"business_query": "  bar la parra   granada ", "business_name": "Bar La Parra"}
    assert client.post(URL, json=OK | typed).status_code == 201
    assert store.signups[("whatsapp", "+34600112233")].business_name == "bar la parra granada"


def test_old_business_name_field_is_accepted_but_not_stored():
    client, store = make()
    r = client.post(URL, json=OK | {"place_id": "p1", "business_name": "Bar La Parra"})
    assert r.status_code == 201
    saved = store.signups[("whatsapp", "+34600112233")]
    assert saved.business_name is None
    assert saved.place_id == "p1"
    assert "Bar La Parra" not in repr(store.__dict__)


def test_business_query_is_optional_and_length_limited():
    client, store = make()
    assert client.post(URL, json=OK | {"business_query": "x" * 201}).status_code == 422
    assert client.post(URL, json=OK | {"business_query": "   "}).status_code == 201
    assert store.signups[("whatsapp", "+34600112233")].business_name is None


def test_consent_is_mandatory():
    client, store = make()
    assert client.post(URL, json=OK | {"consent": False}).status_code == 422
    without = {k: v for k, v in OK.items() if k != "consent"}
    assert client.post(URL, json=without).status_code == 422
    assert store.signups == {}


def test_invalid_contact_returns_422_with_spanish_message():
    client, store = make()
    r = client.post(URL, json=OK | {"contact": "958112233"})
    assert r.status_code == 422
    assert "no parece válido" in r.json()["detail"]
    r = client.post(URL, json=OK | {"channel": "email", "contact": "no-es-correo"})
    assert r.status_code == 422
    assert store.signups == {}


def test_repeated_contact_gets_the_same_answer_and_one_row():
    client, store = make()
    first = client.post(URL, json=OK)
    second = client.post(URL, json=OK | {"contact": "+34 600112233"})
    assert (first.status_code, first.json()) == (second.status_code, second.json())
    assert len(store.signups) == 1


def test_unknown_channel_and_bad_source_are_rejected():
    client, _ = make()
    assert client.post(URL, json=OK | {"channel": "sms"}).status_code == 422
    assert client.post(URL, json=OK | {"source": "<script>"}).status_code == 422
    assert client.post(URL, json=OK | {"score": 101}).status_code == 422


def test_rate_limit_per_client():
    client, store = make(waitlist_rate_limit=2)
    for n in ("600000001", "600000002"):
        assert client.post(URL, json=OK | {"contact": n}).status_code == 201
    assert client.post(URL, json=OK | {"contact": "600000003"}).status_code == 429
    assert len(store.signups) == 2


def test_store_failure_returns_503_and_does_not_leak():
    client, _ = make(MemoryStore(fail=True))
    r = client.post(URL, json=OK)
    assert r.status_code == 503
    assert "fallo simulado" not in r.text
    assert "600" not in r.text


def test_diagnosis_records_funnel_events_with_source():
    client, store = make()
    client.post("/api/v1/public/diagnosis", json={"query": "demo bar", "source": "tiktok_v2"})
    client.post("/api/v1/public/diagnosis", json={"query": "no existe"})
    kinds = [(e[0], e[1]) for e in store.events]
    assert ("diagnosis", "tiktok_v2") in kinds
    assert ("not_found", None) in kinds


def test_diagnosis_still_works_when_event_tracking_fails():
    client, _ = make(MemoryStore(fail=True))
    assert client.post("/api/v1/public/diagnosis", json={"query": "demo bar"}).status_code == 200


def test_app_requires_a_database_outside_fake_mode():
    with pytest.raises(RuntimeError):
        create_app(Settings(_env_file=None, places_api_key="x"))
