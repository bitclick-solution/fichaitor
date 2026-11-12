"""GET /api/v1/me/diagnosis: computed from the facts the worker stored. No database.

The route never talks to Google Places: every test here runs with a places gateway
that fails the test if it is touched.
"""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.adapters.crypto.sealed import generate_keypair
from app.adapters.google_oauth.fake import FakeGoogleOAuth
from app.adapters.job_queue.memory import MemoryJobQueue
from app.adapters.store.memory import MemoryStore
from app.api.main import create_app, national_holidays
from app.application.profile_sync import SYNC_PROFILE_KIND
from app.config import Settings
from app.domain.calendar import Holiday
from app.domain.profile_summary import ProfileFacts
from app.ports.google_oauth import Identity, ProfileLocation
from app.ports.store import StoredSummary

START = "/api/v1/google/oauth/start"
CALLBACK = "/api/v1/google/oauth/callback"
ME = "/api/v1/me"
SELECT = "/api/v1/me/location"
DIAGNOSIS = "/api/v1/me/diagnosis"
SUMMARY = "/api/v1/me/summary"
HEADER = {"X-Requested-With": "fichaitor"}
PUBLIC, _ = generate_keypair()
T0 = 1_800_000_000.0  # 2027-01-15 08:00 UTC
NOW = datetime.fromtimestamp(T0, UTC)
HOUR = 3600

SUB = "google-sub-1"
BAR = ProfileLocation("accounts/1", "locations/1", "Bar La Parra", "ChIJ-conexion", True)
SHOP = ProfileLocation("accounts/1", "locations/2", "Tienda", "", False)
SOON = Holiday(NOW.date() + timedelta(days=12), "Festivo de prueba", "national")

FACTS = ProfileFacts(
    title="Bar La Parra",
    address="Calle Elvira 1, 18010 Granada",
    category="Bar",
    additional_categories=1,
    description_length=400,
    has_phone=True,
    has_website=True,
    service_count=3,
    is_open=True,
    place_id="ChIJ-ficha",
    maps_url="https://maps.google.com/?cid=42",
    special_dates=(),
    photo_count=25,
    latest_photo_at=NOW - timedelta(days=30),
    attribute_count=4,
)


def stored(status="ok", synced_ago=timedelta(minutes=5), status_ago=None, **changes):
    synced_at = None if synced_ago is None else NOW - synced_ago
    status_at = NOW - status_ago if status_ago is not None else (synced_at or NOW)
    values = {
        "status": status,
        "status_at": status_at,
        "synced_at": synced_at,
        "review_count": 13,
        "average_rating": 4.7,
        "unanswered_count": 13,
        "unanswered_low_count": 1,
        "last_review_at": datetime(2025, 2, 6, 18, 30, tzinfo=UTC),
        "has_regular_hours": True,
        "upcoming_special_hours": 0,
        "google_changed": True,
        "google_changed_fields": ("specialHours",),
        "facts": FACTS,
    }
    return StoredSummary(**{**values, **changes})


def never_synced(status: str) -> StoredSummary:
    return StoredSummary(
        status=status,
        status_at=NOW,
        synced_at=None,
        review_count=None,
        average_rating=None,
        unanswered_count=None,
        unanswered_low_count=None,
        last_review_at=None,
        has_regular_hours=None,
        upcoming_special_hours=None,
        google_changed=None,
    )


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


class ForbiddenPlaces:
    """Google Places must not be asked anything by the routes behind a session."""

    async def find_id(self, query: str) -> str | None:
        raise AssertionError("la ruta con sesión ha buscado en Google Places")

    async def get(self, place_id: str):
        raise AssertionError("la ruta con sesión ha consultado Google Places")


class World:
    def __init__(self, locations=None, holidays=(SOON,), store_summary=None, **overrides) -> None:
        self.google = FakeGoogleOAuth(locations=locations or [BAR])
        self.store = MemoryStore()
        self.clock = Clock()
        self.jobs = MemoryJobQueue()
        settings = Settings(
            _env_file=None,
            oauth_state_secret="secreto-de-pruebas",
            token_public_key=PUBLIC,
            **{"cookie_secure": False, **overrides},
        )
        self.app = create_app(
            settings,
            ForbiddenPlaces(),
            self.store,
            self.google,
            clock=self.clock,
            jobs=self.jobs,
            holidays=holidays,
        )
        if store_summary is not None:
            self.store.put_summary(SUB, "locations/1", store_summary)

    def connect_as(self, sub=SUB, email="dueno@example.com", locations=None) -> TestClient:
        self.google.identity_value = Identity(sub=sub, email=email)
        if locations is not None:
            self.google.profile_locations = locations
        client = TestClient(self.app, follow_redirects=False)
        r = client.get(START)
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        assert client.get(CALLBACK, params={"code": "c", "state": state}).status_code == 303
        return client

    def select(self, client: TestClient, title: str | None = None) -> None:
        locations = client.get(ME).json()["locations"]
        chosen = next(loc for loc in locations if title in (None, loc["title"]))
        r = client.post(SELECT, json={"location_id": chosen["id"]}, headers=HEADER)
        assert r.status_code == 200

    def owner(self) -> TestClient:
        client = self.connect_as()
        self.select(client)
        return client

    def sync_jobs(self) -> list:
        return [j for j in self.jobs.jobs.values() if j.kind == SYNC_PROFILE_KIND]


# --- guards -------------------------------------------------------------------


def test_diagnosis_without_session_is_401():
    r = TestClient(World().app).get(DIAGNOSIS)
    assert r.status_code == 401


def test_diagnosis_without_a_selected_location_is_409():
    r = World().connect_as().get(DIAGNOSIS)
    assert r.status_code == 409
    assert isinstance(r.json()["detail"], str)


# --- the diagnosis --------------------------------------------------------------


def test_diagnosis_comes_from_the_stored_facts_with_the_public_shape_plus_status():
    world = World(store_summary=stored())
    r = world.owner().get(DIAGNOSIS)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "place_id",
        "name",
        "address",
        "category",
        "score",
        "label",
        "findings",
        "locked_checks",
        "maps_url",
        "attribution",
        "status",
        "synced_at",
        "stale",
        "location_id",
    }
    assert body["name"] == "Bar La Parra"
    assert body["address"] == "Calle Elvira 1, 18010 Granada"
    assert body["category"] == "Bar"
    assert body["place_id"] == "ChIJ-ficha"
    assert body["maps_url"] == "https://maps.google.com/?cid=42"
    assert body["attribution"] == "Datos de tu Perfil de Empresa de Google"
    assert body["locked_checks"] == []
    assert body["status"] == "ok"
    assert body["synced_at"] == "2027-01-15T07:55:00Z"
    assert body["stale"] is False

    by_code = {f["code"]: f for f in body["findings"]}
    assert list(by_code) == ["unanswered_reviews", "no_holiday_hours", "google_changed"]
    assert by_code["unanswered_reviews"] == {
        "code": "unanswered_reviews",
        "title": "Tienes 13 reseñas sin responder",
        "detail": "Una es de 1 o 2 estrellas. "
        "Una crítica sin respuesta es lo que más frena a quien te compara.",
        "impact": "alto",
    }
    assert by_code["no_holiday_hours"]["title"] == "Sin horario para el festivo del 27 de enero"
    assert by_code["no_holiday_hours"]["detail"].startswith(
        "El 27 de enero es festivo nacional y no has indicado si abres."
    )
    assert by_code["google_changed"]["detail"].startswith("Ha cambiado: Horarios especiales.")
    assert body["score"] == 100 - 15 - 8 - 8
    assert body["label"] == "Mejorable"


def test_place_id_falls_back_to_the_one_of_the_connection_and_name_to_its_title():
    facts = replace(FACTS, title="", place_id="", maps_url="")
    body = World(store_summary=stored(facts=facts)).owner().get(DIAGNOSIS).json()
    assert body["place_id"] == "ChIJ-conexion"
    assert body["name"] == "Bar La Parra"
    assert body["maps_url"] == ""


def test_holidays_are_judged_on_the_day_of_the_request_not_of_the_sync():
    world = World(store_summary=stored(facts=replace(FACTS, special_dates=(SOON.date,))))
    client = world.owner()
    assert "no_holiday_hours" not in [f["code"] for f in client.get(DIAGNOSIS).json()["findings"]]

    world = World(store_summary=stored())
    client = world.owner()
    world.clock.now += 13 * 24 * HOUR  # the holiday has passed
    assert "no_holiday_hours" not in [f["code"] for f in client.get(DIAGNOSIS).json()["findings"]]


def test_the_api_loads_the_national_holidays_of_the_calendar_by_default():
    holidays = national_holidays()
    assert holidays and all(h.scope == "national" and h.verified for h in holidays)
    assert date(2026, 12, 8) in {h.date for h in holidays}


def test_diagnosis_has_no_rate_limit_and_asks_for_no_sync_while_fresh():
    world = World(store_summary=stored(), diagnosis_rate_limit=1)
    client = world.owner()
    before = len(world.sync_jobs())
    for _ in range(12):
        assert client.get(DIAGNOSIS).status_code == 200
    assert len(world.sync_jobs()) == before


def test_each_account_gets_the_diagnosis_of_its_own_profile():
    world = World(store_summary=stored())
    ana = world.owner()
    luis = world.connect_as("sub-luis", "luis@example.com", [BAR])
    world.select(luis)
    assert ana.get(DIAGNOSIS).status_code == 200
    r = luis.get(DIAGNOSIS)  # same Google profile, another account: nothing stored for it
    assert r.status_code == 404 and r.json()["status"] == "pendiente"


# --- states ---------------------------------------------------------------------


def test_without_data_yet_the_answer_is_pending_and_a_sync_is_requested():
    world = World()
    client = world.owner()
    r = client.get(DIAGNOSIS)
    assert r.status_code == 404
    assert r.json() == {
        "status": "pendiente",
        "detail": "Estamos leyendo tu ficha. Vuelve a intentarlo en unos segundos.",
        "location_id": client.get(ME).json()["selected_location_id"],
    }
    assert len(world.sync_jobs()) == 1  # the one of selecting; asking again adds none
    assert client.get(SUMMARY).json()["status"] == "pendiente"


def test_a_summary_written_before_the_facts_existed_is_pending_and_synced_again():
    world = World()
    client = world.connect_as()
    world.select(client)
    world.store.put_summary(SUB, "locations/1", stored(facts=None))
    world.clock.now += HOUR  # another sync window
    r = client.get(DIAGNOSIS)
    assert r.status_code == 404 and r.json()["status"] == "pendiente"
    assert len(world.sync_jobs()) == 2
    assert client.get(SUMMARY).json()["status"] == "ok"  # the summary is still served


def test_needs_reconnect_and_error_without_any_data_are_told_apart():
    for status, text in (("needs_reconnect", "Vuelve a conectar"), ("error", "Prueba más tarde")):
        world = World(store_summary=never_synced(status))
        r = world.owner().get(DIAGNOSIS)
        assert r.status_code == 404
        assert r.json()["status"] == status
        assert text in r.json()["detail"]


def test_a_failed_sync_keeps_serving_the_last_diagnosis_with_its_status():
    for status in ("needs_reconnect", "error"):
        world = World(store_summary=stored(status, synced_ago=timedelta(hours=2)))
        body = world.owner().get(DIAGNOSIS).json()
        assert body["status"] == status
        assert body["synced_at"] == "2027-01-15T06:00:00Z"
        assert body["score"] == 69


def test_an_old_diagnosis_is_stale_and_a_sync_is_requested():
    world = World(store_summary=stored(synced_ago=timedelta(hours=7)))
    client = world.owner()
    world.clock.now += HOUR
    before = len(world.sync_jobs())
    body = client.get(DIAGNOSIS).json()
    assert body["stale"] is True and body["status"] == "ok"
    assert len(world.sync_jobs()) == before + 1


# --- demo mode: no database and no worker ---------------------------------------


def test_demo_mode_serves_a_diagnosis_right_after_selecting_a_profile():
    settings = Settings(
        _env_file=None,
        fake_google=True,
        fake_places=True,
        oauth_state_secret="secreto-de-pruebas",
        token_public_key=PUBLIC,
        cookie_secure=False,
    )
    app = create_app(settings, ForbiddenPlaces(), MemoryStore())
    client = TestClient(app, follow_redirects=False)
    r = client.get(START)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    assert client.get(CALLBACK, params={"code": "c", "state": state}).status_code == 303
    location = client.get(ME).json()["locations"][0]
    client.post(SELECT, json={"location_id": location["id"]}, headers=HEADER)

    r = client.get(DIAGNOSIS)
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == location["title"]
    assert body["attribution"] == "Datos de tu Perfil de Empresa de Google"
    assert body["findings"][0]["code"] == "unanswered_reviews"
    assert body["findings"][0]["title"] == "Tienes 9 reseñas sin responder"
    assert client.get(SUMMARY).json()["status"] == "ok"
