"""Multinegocio, step 4 (docs/multinegocio.md, 13): the routes of the API and their
contract (sections 5 and 12). No database: MemoryStore. The same routes against
PostgreSQL are in test_multinegocio_api_postgres.py.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.adapters.crypto.sealed import generate_keypair
from app.adapters.google_oauth.fake import FakeGoogleOAuth
from app.adapters.job_queue.memory import MemoryJobQueue
from app.adapters.memory_tables import ReviewRow
from app.adapters.places.fake import FakePlaces
from app.adapters.store.memory import MemoryStore, OffboardingRow
from app.api.main import create_app
from app.config import Settings
from app.domain.profile_summary import ProfileFacts
from app.domain.proposals import OUTCOME_LOCATION_INACTIVE, Proposal, ProposalState, ProposalType
from app.ports.google_oauth import Identity, ProfileLocation
from app.ports.store import StoredSummary
from tests.test_me import START, Clock

CALLBACK = "/api/v1/google/oauth/callback"
ME = "/api/v1/me"
SELECT = "/api/v1/me/location"
HEADER = {"X-Requested-With": "fichaitor"}
READS = (
    "/api/v1/me/summary",
    "/api/v1/me/diagnosis",
    "/api/v1/me/sentiment",
    "/api/v1/me/reviews",
    "/api/v1/me/key-dates",
)
NOW = datetime.fromtimestamp(Clock().now, UTC)
PUBLIC, _ = generate_keypair()


def activation(location_id: str) -> str:
    return f"/api/v1/me/locations/{location_id}/activation"


def profile(n: int, title: str | None = None) -> ProfileLocation:
    return ProfileLocation("accounts/1", f"locations/{n}", title or f"Bar {n}", "", True)


def summary(**changes) -> StoredSummary:
    values = {
        "status": "ok",
        "status_at": NOW,
        "synced_at": NOW,
        "review_count": 6,
        "average_rating": 4.5,
        "unanswered_count": 2,
        "unanswered_low_count": 1,
        "last_review_at": NOW,
        "has_regular_hours": True,
        "upcoming_special_hours": 0,
        "google_changed": False,
        "facts": ProfileFacts(regular_hours=((0, "09:00", "14:00"),)),
        "reviews_synced_at": NOW,
    }
    return StoredSummary(**{**values, **changes})


class World:
    """The API on a MemoryStore, with the jobs it enqueues in sight."""

    def __init__(self) -> None:
        self.google = FakeGoogleOAuth()
        self.store = MemoryStore()
        self.jobs = MemoryJobQueue()
        self.clock = Clock()
        settings = Settings(
            _env_file=None,
            oauth_state_secret="secreto-de-pruebas",
            token_public_key=PUBLIC,
            cookie_secure=False,
        )
        self.app = create_app(
            settings, FakePlaces(), self.store, self.google, clock=self.clock, jobs=self.jobs
        )

    def owner(self, sub: str, locations: list[ProfileLocation]) -> TestClient:
        self.google.identity_value = Identity(sub=sub, email=f"{sub}@example.com")
        self.google.profile_locations = locations
        client = TestClient(self.app, follow_redirects=False)
        r = client.get(START)
        state = r.headers["location"].split("state=")[1].split("&")[0]
        assert client.get(CALLBACK, params={"code": "c", "state": state}).status_code == 303
        return client

    def ids(self, client: TestClient) -> list[str]:
        return [loc["id"] for loc in client.get(ME).json()["locations"]]

    def account_id(self, sub: str) -> str:
        return self.store.accounts[sub].id

    def key_date(self, sub: str, location_id: str, day: str) -> str:
        proposal = Proposal(
            id=f"prop_{uuid.uuid4().hex[:16]}",
            account_id=self.account_id(sub),
            type=ProposalType.KEY_DATE,
            source_ref=f"{location_id}:{day}",
            state=ProposalState.SENT,
            priority=10,
            created_at=NOW,
            scheduled_for=NOW,
            payload={"kind": "holiday", "date": day, "name": "Fiesta", "location_id": location_id},
            step="holiday.ask",
            step_token="abc123",
        )
        self.store.key_date_proposals[proposal.id] = proposal
        return proposal.id

    def synced(self) -> list[str]:
        return [j.payload["location"] for j in self.jobs.jobs.values()]


def activate(client: TestClient, location_id: str, plan: str | None = None, **kwargs):
    body = {"plan": plan} if plan is not None else {}
    return client.post(activation(location_id), json=body, headers=HEADER, **kwargs)


def by_id(body: dict) -> dict[str, dict]:
    return {loc["id"]: loc for loc in body["locations"]}


# --- GET /me ----------------------------------------------------------------


def test_me_has_the_shape_of_the_multinegocio_contract():
    world = World()
    client = world.owner("ana", [profile(1, "A"), profile(2, "B")])
    bar, shop = world.ids(client)
    assert activate(client, bar, "gratis").status_code == 200
    world.store.put_summary("ana", "locations/1", summary(unanswered_count=3))
    world.key_date("ana", bar, (NOW + timedelta(days=5)).date().isoformat())
    world.key_date("ana", bar, (NOW - timedelta(days=5)).date().isoformat())  # past

    body = client.get(ME).json()
    assert set(body) == {
        "email",
        "locations",
        "selected_location_id",
        "connected",
        "pending_deletion",
        "plans",
        "pricing",
        "billing",
    }
    active, inactive = body["locations"]
    assert set(active) == {
        "id",
        "title",
        "place_id",
        "verified",
        "active",
        "plan",
        "active_since",
        "status",
        "pending",
        "usage",
        "features",
    }
    assert (active["id"], active["active"], active["plan"]) == (bar, True, "gratis")
    assert active["usage"] == {"ai_replies": {"used": 0, "limit": 25, "lifetime": True}}
    assert active["features"] == {"key_dates": False, "whatsapp": False}
    assert active["status"] == "ok" and active["active_since"].endswith("Z")
    assert active["pending"] == {"reviews": 3, "key_dates": 1}
    assert inactive == {
        "id": shop,
        "title": "B",
        "place_id": None,
        "verified": True,
        "active": False,
        "plan": None,
        "active_since": None,
        "status": None,
        "pending": None,
        "usage": None,
        "features": None,
    }
    assert body["selected_location_id"] is None  # activating is not choosing
    assert body["plans"] == {
        "active": 1,
        "free_available": False,
        "available": {"pro": 3, "pro_whatsapp": 0},
    }
    assert body["pricing"] == {
        "currency": "EUR",
        "period": "year",
        "charging": False,
        "plans": {
            "pro": {"first": 3500, "additional": 3325},
            "pro_whatsapp": {"first": 5900, "additional": 5605},
        },
        "total": 0,
        "if_activate": {"pro": 3500, "pro_whatsapp": 5900},
        "if_deactivate": {bar: 0},
    }
    assert body["billing"] == {
        "profile_complete": False,
        "blocked": None,
        "seats": [],
        "new_seat_price": {"pro": 3500, "pro_whatsapp": 5900},
    }


def test_an_active_profile_without_a_summary_is_pending_and_counts_nothing():
    world = World()
    client = world.owner("ana", [profile(1)])
    (bar,) = world.ids(client)
    (loc,) = activate(client, bar).json()["locations"]
    assert loc["status"] == "pendiente"
    assert loc["pending"] == {"reviews": 0, "key_dates": 0}
    world.store.put_summary("ana", "locations/1", summary(status="needs_reconnect"))
    assert client.get(ME).json()["locations"][0]["status"] == "needs_reconnect"


def test_a_new_account_has_the_beta_room_and_nothing_to_pay():
    world = World()
    client = world.owner("ana", [profile(1)])
    body = client.get(ME).json()
    assert body["plans"] == {
        "active": 0,
        "free_available": True,
        "available": {"pro": 3, "pro_whatsapp": 0},
    }
    assert body["pricing"]["total"] == 0 and body["pricing"]["if_deactivate"] == {}


# --- activation -------------------------------------------------------------


def test_activation_without_a_plan_takes_pro_in_the_beta_and_asks_for_a_sync():
    # Paywall (2026-11-13): while nothing is charged, an activation without a plan takes
    # Pro while the beta entitlement has room, then Gratis (test_paywall.py).
    world = World()
    client = world.owner("ana", [profile(n) for n in range(1, 4)])
    first, second, _ = world.ids(client)

    r = client.post(activation(first), headers=HEADER)  # no body at all
    assert r.status_code == 200
    assert r.json() == client.get(ME).json()
    assert by_id(r.json())[first]["plan"] == "pro"
    assert world.synced() == ["locations/1"]

    body = activate(client, second).json()
    assert by_id(body)[second]["plan"] == "pro"
    assert body["pricing"]["total"] == 6825
    assert body["pricing"]["if_activate"] == {"pro": 10150, "pro_whatsapp": 12550}
    assert body["pricing"]["if_deactivate"] == {first: 3500, second: 3500}
    assert body["plans"]["available"] == {"pro": 1, "pro_whatsapp": 0}
    assert world.synced() == ["locations/1", "locations/2"]


def test_activating_an_active_profile_with_another_plan_changes_its_plan():
    world = World()
    client = world.owner("ana", [profile(1)])
    (bar,) = world.ids(client)
    since = by_id(activate(client, bar, "gratis").json())[bar]["active_since"]
    body = activate(client, bar, "pro").json()
    assert by_id(body)[bar]["plan"] == "pro"
    assert by_id(body)[bar]["active_since"] == since
    assert body["plans"]["free_available"] is True
    # Without a plan, an active profile keeps its own.
    assert by_id(activate(client, bar).json())[bar]["plan"] == "pro"


def test_past_the_limit_is_402_and_nothing_changes():
    world = World()
    client = world.owner("ana", [profile(n) for n in range(1, 6)])
    ids = world.ids(client)
    for location_id in ids[:4]:
        assert activate(client, location_id).status_code == 200
    before = client.get(ME).json()
    r = activate(client, ids[4])
    assert r.status_code == 402
    # The paywall: no_seat, with the seat to buy (nothing to buy during the beta).
    assert r.json()["code"] == "no_seat" and r.json()["charging"] is False
    # No paid seat yet (the beta ones are not): a new one would be the first, 35,00.
    assert r.json()["offer"] == {"plan": "pro", "price": 3500}
    r = activate(client, ids[3], "pro")  # the 3 Pro of the beta are taken
    assert r.status_code == 402
    assert activate(client, ids[0], "pro_whatsapp").status_code == 402
    assert client.get(ME).json() == before


def test_the_free_plan_of_a_google_profile_is_taken_once_across_accounts():
    world = World()
    ana = world.owner("ana", [profile(1)])
    luis = world.owner("luis", [profile(1)])  # the same Google profile
    (ana_bar,), (luis_bar,) = world.ids(ana), world.ids(luis)
    assert by_id(activate(ana, ana_bar, "gratis").json())[ana_bar]["plan"] == "gratis"
    r = activate(luis, luis_bar, "gratis")
    assert r.status_code == 409
    assert r.json() == {
        "detail": (
            "Esta ficha ya está activa en el plan Gratis en otra cuenta. Actívala con un "
            "plan de pago o pide a esa cuenta que la desactive."
        ),
        "code": "free_taken",
    }
    # Without a plan, the next one with room.
    assert by_id(activate(luis, luis_bar).json())[luis_bar]["plan"] == "pro"


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_activation_routes_need_the_header_a_session_and_a_profile_of_the_account(method):
    world = World()
    ana = world.owner("ana", [profile(1)])
    luis = world.owner("luis", [profile(2)])
    (ana_bar,), (luis_bar,) = world.ids(ana), world.ids(luis)

    assert ana.request(method, activation(ana_bar)).status_code == 403
    assert (
        TestClient(world.app).request(method, activation(ana_bar), headers=HEADER).status_code
        == 401
    )
    for stolen in (luis_bar, "loc_noexiste", "no-es-un-id", "loc_" + "a" * 40):
        r = ana.request(method, activation(stolen), headers=HEADER)
        assert r.status_code == 404, stolen
        assert r.json() == {"detail": "Esa ficha no está en tu cuenta."}
    assert by_id(luis.get(ME).json())[luis_bar]["active"] is False


def test_an_unknown_plan_is_422():
    world = World()
    client = world.owner("ana", [profile(1)])
    (bar,) = world.ids(client)
    assert activate(client, bar, "oro").status_code == 422
    r = client.post(activation(bar), json={"plan": "pro", "precio": 0}, headers=HEADER)
    assert r.status_code == 422
    assert by_id(client.get(ME).json())[bar]["active"] is False


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_activation_routes_are_closed_during_a_scheduled_leave(method):
    world = World()
    client = world.owner("ana", [profile(1)])
    (bar,) = world.ids(client)
    assert activate(client, bar).status_code == 200
    world.store.offboardings.append(
        OffboardingRow(
            "off1", world.account_id("ana"), "ficha", NOW, NOW + timedelta(hours=48), None
        )
    )
    r = client.request(method, activation(bar), headers=HEADER)
    assert r.status_code == 409
    assert r.json()["code"] == "offboarding_pending" and isinstance(r.json()["detail"], str)
    body = client.get(ME).json()
    (loc,) = body["locations"]
    assert loc["active"] is True  # nothing changed
    assert loc["status"] is None and loc["pending"] is None  # nothing is read meanwhile


def test_the_offboarding_refusal_of_the_store_is_409_with_its_code():
    # The leave scheduled between the guard and the write: the database refuses it.
    world = World()
    client = world.owner("ana", [profile(1)])
    (bar,) = world.ids(client)

    async def refused(*args, **kwargs):
        from app.ports.store import OffboardingPending

        raise OffboardingPending

    world.store.activate = refused
    r = activate(client, bar)
    assert r.status_code == 409 and r.json()["code"] == "offboarding_pending"


# --- deactivation -----------------------------------------------------------


def test_deactivating_the_selected_profile_moves_the_preference_to_the_oldest_active():
    world = World()
    client = world.owner("ana", [profile(1), profile(2), profile(3)])
    first, second, third = world.ids(client)
    assert activate(client, third).status_code == 200
    assert client.post(SELECT, json={"location_id": third}, headers=HEADER).status_code == 200
    assert activate(client, second).status_code == 200
    assert activate(client, first).status_code == 200
    pending = world.key_date("ana", third, (NOW + timedelta(days=5)).date().isoformat())

    r = client.delete(activation(third), headers=HEADER)
    assert r.status_code == 200
    assert r.json() == client.get(ME).json()
    assert r.json()["selected_location_id"] == second  # activated before first
    assert by_id(r.json())[third]["active"] is False
    proposal = world.store.key_date_proposals[pending]
    assert proposal.state is ProposalState.CANCELLED
    assert proposal.outcome_reason == OUTCOME_LOCATION_INACTIVE

    # The reads without ?location= follow the preference.
    assert client.get("/api/v1/me/summary").json()["location_id"] == second
    # An inactive profile is not served, also when named.
    r = client.get("/api/v1/me/summary", params={"location": third})
    assert r.status_code == 409 and r.json() == {"detail": "Esa ficha no está activa."}


def test_deactivating_the_last_active_profile_is_allowed_and_again_is_no_error():
    world = World()
    client = world.owner("ana", [profile(1)])
    (bar,) = world.ids(client)
    assert activate(client, bar).status_code == 200
    assert client.post(SELECT, json={"location_id": bar}, headers=HEADER).status_code == 200
    body = client.delete(activation(bar), headers=HEADER).json()
    assert body["selected_location_id"] is None
    assert body["plans"]["active"] == 0 and body["plans"]["free_available"] is True
    assert client.delete(activation(bar), headers=HEADER).status_code == 200
    r = client.get("/api/v1/me/summary")
    assert r.status_code == 409
    assert r.json() == {"detail": "Primero elige con qué ficha quieres trabajar."}


# --- ?location= on the reads --------------------------------------------------


@pytest.mark.parametrize("path", READS)
def test_reads_answer_for_the_profile_named_and_say_which_it_is(path):
    world = World()
    client = world.owner("ana", [profile(1), profile(2)])
    bar, shop = world.ids(client)
    for location_id in (bar, shop):
        assert activate(client, location_id).status_code == 200
    assert client.post(SELECT, json={"location_id": bar}, headers=HEADER).status_code == 200
    world.store.put_summary("ana", "locations/2", summary())

    r = client.get(path, params={"location": shop})
    assert r.status_code == 200, r.text
    assert r.json()["location_id"] == shop
    r = client.get(path)  # the preference
    assert r.json()["location_id"] == bar


@pytest.mark.parametrize("path", READS)
def test_reads_of_another_account_unknown_or_inactive_profiles_are_refused(path):
    world = World()
    ana = world.owner("ana", [profile(1), profile(2)])
    luis = world.owner("luis", [profile(3)])
    bar, shop = world.ids(ana)
    (luis_bar,) = world.ids(luis)
    assert activate(ana, bar).status_code == 200
    assert activate(luis, luis_bar).status_code == 200

    not_found = {"detail": "Esa ficha no está en tu cuenta."}
    for location_id in (luis_bar, "loc_noexiste", "../loc", ""):
        r = ana.get(path, params={"location": location_id})
        assert (r.status_code, r.json()) == (404, not_found), location_id
    r = ana.get(path, params={"location": shop})
    assert (r.status_code, r.json()) == (409, {"detail": "Esa ficha no está activa."})
    # No ?location= and no preference: as before.
    r = ana.get(path)
    assert r.status_code == 409
    assert r.json() == {"detail": "Primero elige con qué ficha quieres trabajar."}


@pytest.mark.parametrize("path", READS)
def test_reads_are_closed_during_a_scheduled_leave_even_with_a_location(path):
    world = World()
    client = world.owner("ana", [profile(1)])
    (bar,) = world.ids(client)
    assert activate(client, bar).status_code == 200
    world.store.offboardings.append(
        OffboardingRow(
            "off1", world.account_id("ana"), "cuenta", NOW, NOW + timedelta(hours=48), None
        )
    )
    r = client.get(path, params={"location": bar})
    assert r.status_code == 409 and isinstance(r.json()["detail"], str)


def test_reviews_are_listed_per_profile():
    world = World()
    client = world.owner("ana", [profile(1), profile(2)])
    bar, shop = world.ids(client)
    for location_id in (bar, shop):
        activate(client, location_id)
    for n, review_id in ((1, "rev_00000000000000a1"), (2, "rev_00000000000000b2")):
        world.store.put_summary("ana", f"locations/{n}", summary())
        world.store.tables.reviews[review_id] = ReviewRow(
            review_id, "ana", f"locations/{n}", f"g{n}", 4, "Bien", NOW
        )
    r = client.get("/api/v1/me/reviews", params={"location": shop})
    assert [x["id"] for x in r.json()["reviews"]] == ["rev_00000000000000b2"]
    r = client.get("/api/v1/me/reviews", params={"location": bar, "filter": "all"})
    assert [x["id"] for x in r.json()["reviews"]] == ["rev_00000000000000a1"]


def test_a_key_date_of_an_active_profile_is_answered_whatever_the_preference():
    world = World()
    client = world.owner("ana", [profile(1), profile(2)])
    bar, shop = world.ids(client)
    for location_id in (bar, shop):
        activate(client, location_id)
    world.store.put_summary("ana", "locations/2", summary())
    day = (NOW + timedelta(days=7)).date()
    while day.weekday() != 0:  # a Monday: the usual hours of the summary
        day += timedelta(days=1)
    proposal_id = world.key_date("ana", shop, day.isoformat())

    listed = client.get("/api/v1/me/key-dates", params={"location": shop}).json()
    assert listed["location_id"] == shop
    (item,) = listed["dates"]
    assert item["location_id"] == shop
    assert item["regular_hours"] == [{"open": "09:00", "close": "14:00"}]
    assert client.get("/api/v1/me/key-dates", params={"location": bar}).json()["dates"] == []

    # No preference at all: the proposal names its profile.
    assert client.get(ME).json()["selected_location_id"] is None
    r = client.post(
        f"/api/v1/me/key-dates/{proposal_id}/answer", json={"kind": "closed"}, headers=HEADER
    )
    assert r.status_code == 202, r.text
    assert r.json()["location_id"] == shop
    assert r.json()["regular_hours"] == [{"open": "09:00", "close": "14:00"}]


def test_a_key_date_of_a_deactivated_profile_is_not_found():
    world = World()
    client = world.owner("ana", [profile(1), profile(2)])
    bar, shop = world.ids(client)
    for location_id in (bar, shop):
        activate(client, location_id)
    day = (NOW + timedelta(days=7)).date().isoformat()
    proposal_id = world.key_date("ana", shop, day)
    client.delete(activation(shop), headers=HEADER)
    url = f"/api/v1/me/key-dates/{proposal_id}"
    assert client.post(f"{url}/answer", json={"kind": "closed"}, headers=HEADER).status_code == 404
    assert client.post(f"{url}/dismiss", headers=HEADER).status_code == 404


# --- POST /me/location: only the preference (step 9) --------------------------


def test_choosing_an_inactive_profile_is_a_409_that_sends_to_activate_it():
    world = World()
    client = world.owner("ana", [profile(1), profile(2)])
    bar, shop = world.ids(client)
    r = client.post(SELECT, json={"location_id": bar}, headers=HEADER)
    assert r.status_code == 409
    assert r.json() == {
        "detail": "Esa ficha no está activa. Actívala primero en «Tus negocios».",
        "code": "location_inactive",
    }
    me = client.get(ME).json()
    # Nothing was activated, nothing chosen and nothing synced.
    assert me["selected_location_id"] is None and me["plans"]["active"] == 0
    assert all(loc["active"] is False for loc in me["locations"])
    assert world.synced() == []


def test_choosing_an_active_profile_only_moves_the_preference():
    world = World()
    client = world.owner("ana", [profile(1), profile(2)])
    bar, shop = world.ids(client)
    for location_id in (bar, shop):
        assert activate(client, location_id).status_code == 200
    body = client.post(SELECT, json={"location_id": bar}, headers=HEADER).json()
    assert body["selected_location_id"] == bar
    body = client.post(SELECT, json={"location_id": shop}, headers=HEADER).json()
    assert body["selected_location_id"] == shop
    assert (by_id(body)[bar]["plan"], by_id(body)[shop]["plan"]) == ("pro", "pro")
    assert body["plans"]["active"] == 2
    # Once deactivated, it cannot be the preference any more.
    client.delete(activation(bar), headers=HEADER)
    assert client.post(SELECT, json={"location_id": bar}, headers=HEADER).status_code == 409


def test_choosing_a_profile_of_another_account_is_the_same_404_as_an_unknown_one():
    world = World()
    luis = world.owner("luis", [profile(9)])
    (theirs,) = world.ids(luis)
    activate(luis, theirs)
    client = world.owner("ana", [profile(1)])
    for location_id in (theirs, "loc_doesnotexist"):
        r = client.post(SELECT, json={"location_id": location_id}, headers=HEADER)
        assert r.status_code == 404 and r.json() == {"detail": "Esa ficha no está en tu cuenta."}
