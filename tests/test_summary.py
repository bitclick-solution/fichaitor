"""Panel summary and the sync it asks the worker for. No database: MemoryStore."""

import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.adapters.crypto.sealed import generate_keypair
from app.adapters.google_oauth.fake import FakeGoogleOAuth
from app.adapters.job_queue.memory import MemoryJobQueue
from app.adapters.places.fake import DEMO, FakePlaces
from app.adapters.store.memory import MemoryStore
from app.api.main import create_app
from app.api.summary_out import field_label
from app.application.profile_sync import SYNC_PROFILE_KIND, sync_key
from app.config import Settings
from app.ports.google_oauth import Identity, ProfileLocation
from app.ports.store import StoredSummary
from app.worker import sync_profile

START = "/api/v1/google/oauth/start"
CALLBACK = "/api/v1/google/oauth/callback"
ME = "/api/v1/me"
SELECT = "/api/v1/me/location"
SUMMARY = "/api/v1/me/summary"
HEADER = {"X-Requested-With": "fichaitor"}
PUBLIC, _ = generate_keypair()
T0 = 1_800_000_000.0
NOW = datetime.fromtimestamp(T0, UTC)
MINUTE = 60
HOUR = 3600

SUB = "google-sub-1"
BAR = ProfileLocation("accounts/1", "locations/1", "Bar La Parra (ejemplo)", DEMO.place_id, True)
SHOP = ProfileLocation("accounts/1", "locations/2", "Tienda", "", False)

PENDING = {
    "status": "pendiente",
    "synced_at": None,
    "review_count": None,
    "average_rating": None,
    "unanswered_count": None,
    "unanswered_low_count": None,
    "last_review_at": None,
    "has_regular_hours": None,
    "upcoming_special_hours": None,
    "google_changed": None,
    "google_changed_fields": None,
    "google_changed_labels": None,
    "stale": False,
}


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
    }
    return StoredSummary(**{**values, **changes})


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


class BrokenQueue:
    async def enqueue(self, kind, payload, **_):
        raise RuntimeError(f"cola caída para {payload['google_sub']}")


class World:
    def __init__(self, locations=None, jobs=None, **overrides) -> None:
        self.google = FakeGoogleOAuth(locations=locations or [BAR])
        self.store = MemoryStore()
        self.clock = Clock()
        self.jobs = jobs or MemoryJobQueue()
        settings = Settings(
            _env_file=None,
            oauth_state_secret="secreto-de-pruebas",
            token_public_key=PUBLIC,
            **{"cookie_secure": False, **overrides},
        )
        self.app = create_app(
            settings, FakePlaces(), self.store, self.google, clock=self.clock, jobs=self.jobs
        )

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

    def owner_with_profile(self) -> TestClient:
        client = self.connect_as()
        self.select(client)
        return client

    def sync_jobs(self) -> list:
        return [j for j in self.jobs.jobs.values() if j.kind == SYNC_PROFILE_KIND]


# --- guards -----------------------------------------------------------------


def test_summary_needs_a_session_and_a_selected_profile():
    world = World()
    assert TestClient(world.app).get(SUMMARY).status_code == 401
    client = world.connect_as()
    r = client.get(SUMMARY)
    assert r.status_code == 409
    assert isinstance(r.json()["detail"], str)
    assert world.sync_jobs() == []


# --- enqueuing --------------------------------------------------------------


def test_the_api_uses_the_kind_the_worker_handles():
    assert SYNC_PROFILE_KIND == sync_profile.KIND


def test_choosing_a_profile_enqueues_its_sync_once_per_window():
    world = World(locations=[BAR, SHOP])
    client = world.connect_as()
    assert world.sync_jobs() == []

    world.select(client, BAR.title)
    world.clock.now += 5 * MINUTE
    world.select(client, BAR.title)
    assert [j.payload for j in world.sync_jobs()] == [
        {"google_sub": SUB, "location": "locations/1"}
    ]

    world.select(client, SHOP.title)  # another profile has its own window
    assert [j.payload["location"] for j in world.sync_jobs()] == ["locations/1", "locations/2"]

    world.clock.now += 15 * MINUTE
    world.select(client, BAR.title)
    assert [j.payload["location"] for j in world.sync_jobs()] == [
        "locations/1",
        "locations/2",
        "locations/1",
    ]


def test_sync_key_changes_with_profile_identity_and_window_and_hides_identifiers():
    key = sync_key(SUB, "locations/1", T0, 900)
    assert key == sync_key(SUB, "locations/1", T0 + 1, 900)
    assert key != sync_key(SUB, "locations/1", T0 + 900, 900)
    assert key != sync_key(SUB, "locations/2", T0, 900)
    assert key != sync_key("otro-sub", "locations/1", T0, 900)
    assert SUB not in key
    assert len(sync_key("s" * 255, "l" * 255, T0, 900)) <= 200


def test_a_failed_enqueue_never_fails_the_route_and_logs_no_identifiers(caplog):
    world = World(jobs=BrokenQueue())
    with caplog.at_level(logging.WARNING):
        client = world.connect_as()
        world.select(client)
        r = client.get(SUMMARY)
    assert r.status_code == 200
    assert r.json() == PENDING
    assert "RuntimeError" in caplog.text
    assert SUB not in caplog.text
    assert "locations/1" not in caplog.text


# --- the summary ------------------------------------------------------------


def test_no_summary_yet_is_pendiente_with_nulls_and_asks_for_a_sync():
    world = World()
    client = world.owner_with_profile()
    assert client.get(SUMMARY).json() == PENDING
    assert len(world.sync_jobs()) == 1  # the one of the choice; same window

    world.clock.now += 20 * MINUTE
    assert client.get(SUMMARY).json() == PENDING
    assert client.get(SUMMARY).json() == PENDING
    assert len(world.sync_jobs()) == 2


def test_summary_has_the_exact_shape_of_the_contract():
    world = World()
    client = world.owner_with_profile()
    world.store.put_summary(
        SUB,
        "locations/1",
        stored(synced_ago=timedelta(0), synced_at=datetime(2026, 11, 11, 16, 58, 39, 123456, UTC)),
    )
    world.clock.now = datetime(2026, 11, 11, 17, 0, tzinfo=UTC).timestamp()
    r = client.get(SUMMARY)
    assert r.status_code == 200
    assert r.json() == {
        "status": "ok",
        "synced_at": "2026-11-11T16:58:39Z",
        "review_count": 13,
        "average_rating": 4.7,
        "unanswered_count": 13,
        "unanswered_low_count": 1,
        "last_review_at": "2025-02-06",
        "has_regular_hours": True,
        "upcoming_special_hours": 0,
        "google_changed": True,
        "google_changed_fields": ["specialHours"],
        "google_changed_labels": ["Horarios especiales"],
        "stale": False,
    }


def test_a_fresh_summary_does_not_enqueue_and_an_old_one_is_stale_and_does():
    world = World(summary_stale_hours=6)
    client = world.owner_with_profile()
    world.clock.now += HOUR  # out of the window of the choice
    world.store.put_summary(SUB, "locations/1", stored(synced_ago=timedelta(hours=-1)))

    world.clock.now += 5 * HOUR  # the summary is 5 hours old
    assert client.get(SUMMARY).json()["stale"] is False
    assert len(world.sync_jobs()) == 1

    world.clock.now += 2 * HOUR  # 7 hours old
    body = client.get(SUMMARY).json()
    assert body["stale"] is True
    assert body["status"] == "ok"
    assert body["review_count"] == 13
    assert len(world.sync_jobs()) == 2
    client.get(SUMMARY)
    assert len(world.sync_jobs()) == 2  # once per window


def test_stale_hours_are_configurable():
    world = World(summary_stale_hours=1)
    client = world.owner_with_profile()
    world.store.put_summary(SUB, "locations/1", stored(synced_ago=timedelta(hours=2)))
    assert client.get(SUMMARY).json()["stale"] is True


def test_failed_statuses_keep_the_last_figures_and_are_retried_after_the_window():
    for status in ("needs_reconnect", "error"):
        world = World()
        client = world.owner_with_profile()
        world.clock.now += HOUR
        failed_at = datetime.fromtimestamp(world.clock.now, UTC)
        world.store.put_summary(
            SUB,
            "locations/1",
            stored(status=status, synced_ago=timedelta(hours=1), status_at=failed_at),
        )
        body = client.get(SUMMARY).json()
        assert body["status"] == status
        assert body["review_count"] == 13
        assert body["stale"] is False
        assert len(world.sync_jobs()) == 1  # just failed: no new attempt yet

        world.clock.now += 16 * MINUTE
        assert client.get(SUMMARY).json()["status"] == status
        assert len(world.sync_jobs()) == 2


def test_a_failure_before_any_good_sync_has_a_status_and_no_figures():
    world = World()
    client = world.owner_with_profile()
    never = stored(
        status="error",
        synced_ago=None,
        review_count=None,
        average_rating=None,
        unanswered_count=None,
        unanswered_low_count=None,
        last_review_at=None,
        has_regular_hours=None,
        upcoming_special_hours=None,
        google_changed=None,
        google_changed_fields=(),
    )
    world.store.put_summary(SUB, "locations/1", never)
    assert client.get(SUMMARY).json() == {
        **PENDING,
        "status": "error",
        "google_changed_fields": [],
        "google_changed_labels": [],
    }


def test_changed_fields_get_spanish_labels_and_unknown_ones_stay_as_they_are():
    assert [
        field_label(f)
        for f in ("specialHours", "regularHours", "title", "phoneNumbers", "websiteUri")
    ] == ["Horarios especiales", "Horario", "Nombre", "Teléfono", "Web"]
    assert field_label("categories") == "Categorías"
    assert field_label("phoneNumbers.primaryPhone") == "Teléfono"
    assert field_label("campoNuevo") == "campoNuevo"

    world = World()
    client = world.owner_with_profile()
    fields = ("title", "campoNuevo", "categories")
    world.store.put_summary(SUB, "locations/1", stored(google_changed_fields=fields))
    body = client.get(SUMMARY).json()
    assert body["google_changed_fields"] == ["title", "campoNuevo", "categories"]
    assert body["google_changed_labels"] == ["Nombre", "campoNuevo", "Categorías"]


# --- isolation --------------------------------------------------------------


def test_an_account_never_reads_the_summary_of_another_even_for_the_same_google_profile():
    world = World()
    ana = world.connect_as("sub-ana", "ana@example.com", [BAR])
    luis = world.connect_as("sub-luis", "luis@example.com", [BAR])  # same Google profile
    world.select(ana)
    world.select(luis)

    world.store.put_summary("sub-ana", BAR.location, stored(review_count=40))
    assert ana.get(SUMMARY).json()["review_count"] == 40
    assert luis.get(SUMMARY).json() == PENDING

    world.store.put_summary("sub-luis", BAR.location, stored(review_count=7))
    assert ana.get(SUMMARY).json()["review_count"] == 40
    assert luis.get(SUMMARY).json()["review_count"] == 7

    payloads = [j.payload for j in world.sync_jobs()]
    assert {"google_sub": "sub-ana", "location": BAR.location} in payloads
    assert {"google_sub": "sub-luis", "location": BAR.location} in payloads


def test_only_the_selected_profile_of_the_account_is_read():
    world = World(locations=[BAR, SHOP])
    client = world.connect_as()
    world.store.put_summary(SUB, SHOP.location, stored(review_count=99))
    world.select(client, BAR.title)
    assert client.get(SUMMARY).json() == PENDING
    world.select(client, SHOP.title)
    assert client.get(SUMMARY).json()["review_count"] == 99


def test_a_profile_google_no_longer_lists_has_no_summary():
    world = World(locations=[BAR, SHOP])
    client = world.connect_as()
    world.select(client, SHOP.title)
    world.store.put_summary(SUB, SHOP.location, stored())
    client = world.connect_as(locations=[BAR])  # reconnects; the shop is gone
    assert client.get(SUMMARY).status_code == 409


# --- development without database -------------------------------------------


def test_fake_google_without_database_shows_the_whole_journey_with_demo_figures():
    settings = Settings(
        _env_file=None,
        fake_google=True,
        fake_places=True,
        oauth_state_secret="secreto-de-pruebas",
        token_public_key=PUBLIC,
        cookie_secure=False,
    )
    client = TestClient(create_app(settings), follow_redirects=False)
    r = client.get(START)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    assert client.get(CALLBACK, params={"code": "c", "state": state}).status_code == 303
    assert client.get(SUMMARY).status_code == 409

    location_id = client.get(ME).json()["locations"][0]["id"]
    assert client.post(SELECT, json={"location_id": location_id}, headers=HEADER).status_code == 200
    body = client.get(SUMMARY).json()
    assert body["status"] == "ok"
    assert body["review_count"] == 13
    assert body["unanswered_count"] == 9
    assert body["unanswered_low_count"] == 2
    assert body["has_regular_hours"] is True
    assert body["upcoming_special_hours"] == 1
    assert body["google_changed"] is False
    assert body["google_changed_labels"] == []
    assert body["stale"] is False
