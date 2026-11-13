"""The paywall, end to end without a database (docs/contrato-web-api.md, «Cobro y paywall»;
docs/admin-facturacion.md, steps 6 and 8): the routes of the API on a MemoryStore, the
billing process on the same tables (MemoryBillingStore) and Stripe's fake double
(app/adapters/stripe/fake.py). Never the real Stripe.

The same tables and privileges against PostgreSQL are in test_billing_schema_postgres.py.
"""

import asyncio
import hashlib
import hmac
import json
import time
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.adapters.billing_store.memory import MemoryBillingStore
from app.adapters.conversation_store.memory import MemoryLocation
from app.adapters.google_oauth.fake import FakeGoogleOAuth
from app.adapters.job_queue.memory import MemoryJobQueue
from app.adapters.places.fake import FakePlaces
from app.adapters.store.memory import MemoryStore
from app.adapters.stripe.fake import FakePayments
from app.api.main import create_app
from app.billing.events import StripeEvents
from app.billing.requests import BillingRequests, checkout_urls, is_stripe_url
from app.config import Settings
from app.domain.billing import PriceKey, SeatStatus, TaxTreatment
from app.domain.jobs import Job
from app.domain.plans import Plan
from app.domain.proposals import OUTCOME_PLAN_REQUIRED
from app.domain.proposals import ProposalState as S
from app.ports.billing_store import EVENT_JOB, REQUEST_JOB
from app.ports.google_oauth import Identity
from app.ports.payments import Address, CustomerDetails
from tests import test_multinegocio_conversation_service as conv
from tests.test_me import START, Clock
from tests.test_multinegocio_api import CALLBACK, ME, PUBLIC, activation, by_id, profile

HEADER = {"X-Requested-With": "fichaitor"}
BILLING = "/api/v1/me/billing"
WEBHOOK = "/api/v1/webhooks/stripe"
SECRET = "whsec_" + "s" * 32
LAUNCHED = datetime(2026, 1, 1, tzinfo=UTC)
PROFILE = {
    "legal_name": "Bar La Parra S.L.",
    "tax_id": "B-1234567-4",
    "address": {
        "line1": "Calle Mayor 1",
        "postal_code": "18001",
        "city": "Granada",
        "province": "Granada",
        "country": "es",
    },
    "invoice_email": "facturas@laparra.es",
}


def run(coro):
    return asyncio.run(coro)


def sign(payload: bytes, secret: str = SECRET, at: int | None = None) -> str:
    at = int(time.time()) if at is None else at
    mac = hmac.new(secret.encode(), f"{at}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={at},v1={mac}"


def event_body(event_id: str, type_: str, object_id: str, livemode: bool = False) -> bytes:
    return json.dumps(
        {
            "id": event_id,
            "object": "event",
            "type": type_,
            "livemode": livemode,
            "created": int(time.time()),
            "data": {"object": {"id": object_id, "object": type_.split(".")[0]}},
        }
    ).encode()


class World:
    """The API, the billing process and the fake Stripe, on the same memory tables."""

    def __init__(self, launched: bool = True, webhook: bool = True) -> None:
        self.google = FakeGoogleOAuth()
        self.store = MemoryStore()
        self.store.launch_at = LAUNCHED if launched else None
        self.billing = MemoryBillingStore(self.store)
        self.clock = Clock()
        settings = Settings(
            _env_file=None,
            oauth_state_secret="secreto-de-pruebas",
            token_public_key=PUBLIC,
            cookie_secure=False,
            stripe_webhook_secret=SECRET if webhook else "",
        )
        self.app = create_app(
            settings,
            FakePlaces(),
            self.store,
            self.google,
            clock=self.clock,
            jobs=MemoryJobQueue(),
            billing=self.billing,
        )
        self.payments = FakePayments()
        self.requests = BillingRequests(self.billing, self.payments, "https://fichaitor.es")
        self.events = StripeEvents(self.billing, self.payments)
        self._event = 0

    def owner(self, sub: str = "ana", locations=None) -> TestClient:
        self.google.identity_value = Identity(sub=sub, email=f"{sub}@example.com")
        self.google.profile_locations = locations or [profile(1, "Bar La Parra"), profile(2)]
        client = TestClient(self.app, follow_redirects=False)
        r = client.get(START)
        state = r.headers["location"].split("state=")[1].split("&")[0]
        assert client.get(CALLBACK, params={"code": "c", "state": state}).status_code == 303
        return client

    def ids(self, client: TestClient) -> list[str]:
        return [loc["id"] for loc in client.get(ME).json()["locations"]]

    def process(self) -> None:
        """The billing process takes every job the API enqueued."""
        while self.billing.jobs:
            kind, payload = self.billing.jobs.pop(0)
            handler = self.requests if kind == REQUEST_JOB else self.events
            run(handler(Job(f"j{len(self.billing.jobs)}", kind, payload)))

    def stripe(self, type_: str, object_id: str, client: TestClient | None = None) -> None:
        """Stripe sends a signed event to the webhook; the billing process handles it."""
        self._event += 1
        body = event_body(f"evt_test{self._event:04d}", type_, object_id)
        sender = client or TestClient(self.app)
        r = sender.post(WEBHOOK, content=body, headers={"Stripe-Signature": sign(body)})
        assert r.status_code == 200
        self.process()

    def buy(self, client: TestClient, location_id: str, plan: str = "pro") -> str:
        """The owner buys a seat and pays in Checkout. Returns the subscription id."""
        r = client.post(
            f"{BILLING}/seats", json={"plan": plan, "location_id": location_id}, headers=HEADER
        )
        assert r.status_code == 202, r.json()
        request_id = r.json()["request_id"]
        self.process()
        ready = client.get(f"{BILLING}/requests/{request_id}").json()
        assert ready["state"] == "lista", ready
        session_id = ready["url"].rsplit("/", 1)[1]
        sub = self.payments.complete_checkout(session_id)
        self.stripe("checkout.session.completed", session_id)
        return sub.id

    def with_profile(self, client: TestClient) -> None:
        assert client.put(f"{BILLING}/profile", json=PROFILE, headers=HEADER).status_code == 200


# --- fiscal profile -------------------------------------------------------------------


def test_the_profile_is_empty_until_saved_and_then_has_the_tax_of_its_address():
    world = World()
    client = world.owner()
    r = client.get(f"{BILLING}/profile")
    assert r.status_code == 200
    assert r.json() == {
        "legal_name": None,
        "tax_id": None,
        "address": {
            "line1": None,
            "line2": None,
            "postal_code": None,
            "city": None,
            "province": None,
            "country": None,
        },
        "invoice_email": None,
        "tax_treatment": None,
        "complete": False,
    }
    r = client.put(f"{BILLING}/profile", json=PROFILE, headers=HEADER)
    assert r.status_code == 200
    body = r.json()
    assert body["tax_id"] == "B12345674" and body["address"]["country"] == "ES"
    assert body["tax_treatment"] == "iva_21" and body["complete"] is True
    assert client.get(f"{BILLING}/profile").json() == body
    assert client.get(ME).json()["billing"]["profile_complete"] is True


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"tax_id": "B12345675"}, "tax_id_invalid"),
        ({"address": {**PROFILE["address"], "postal_code": "99999"}}, "postal_code_invalid"),
        ({"address": {**PROFILE["address"], "country": "US"}}, "country_not_supported"),
    ],
)
def test_a_profile_that_cannot_be_invoiced_is_a_422_with_its_code(change, code):
    client = World().owner()
    r = client.put(f"{BILLING}/profile", json={**PROFILE, **change}, headers=HEADER)
    assert r.status_code == 422 and r.json()["code"] == code
    assert isinstance(r.json()["detail"], str)
    assert client.get(f"{BILLING}/profile").json()["complete"] is False


def test_canarias_is_not_subject_and_the_eu_is_reverse_charge():
    client = World().owner()
    canarias = {**PROFILE, "address": {**PROFILE["address"], "postal_code": "35001"}}
    assert (
        client.put(f"{BILLING}/profile", json=canarias, headers=HEADER).json()["tax_treatment"]
        == "no_sujeto"
    )
    france = {
        **PROFILE,
        "tax_id": "FR40303265045",
        "address": {**PROFILE["address"], "postal_code": "75001", "country": "FR"},
    }
    assert (
        client.put(f"{BILLING}/profile", json=france, headers=HEADER).json()["tax_treatment"]
        == "isp_ue"
    )


def test_writes_need_the_header_and_a_session():
    world = World()
    client = world.owner()
    assert client.put(f"{BILLING}/profile", json=PROFILE).status_code == 403
    assert client.post(f"{BILLING}/pay").status_code == 403
    anonymous = TestClient(world.app)
    assert anonymous.get(f"{BILLING}/profile").status_code == 401
    assert anonymous.post(f"{BILLING}/pay", headers=HEADER).status_code == 401


# --- buying a seat ------------------------------------------------------------------


def test_during_the_beta_nothing_reaches_stripe():
    world = World(launched=False)
    client = world.owner()
    world.with_profile(client)
    bar = world.ids(client)[0]
    r = client.post(f"{BILLING}/seats", json={"plan": "pro", "location_id": bar}, headers=HEADER)
    assert r.status_code == 409 and r.json()["code"] == "beta_no_charge"
    assert world.billing.jobs == [] and world.payments.calls == []
    # In the beta, the paywall activates with the beta seat: also from Gratis to Pro.
    assert (
        by_id(client.post(activation(bar), json={"plan": "gratis"}, headers=HEADER).json())[bar][
            "plan"
        ]
        == "gratis"
    )
    body = client.post(activation(bar), json={"plan": "pro"}, headers=HEADER).json()
    assert by_id(body)[bar]["plan"] == "pro"
    assert body["pricing"]["charging"] is False


def test_a_seat_needs_the_fiscal_profile_and_a_profile_of_the_account():
    world = World()
    client = world.owner()
    bar = world.ids(client)[0]
    r = client.post(f"{BILLING}/seats", json={"plan": "pro", "location_id": bar}, headers=HEADER)
    assert r.status_code == 409 and r.json()["code"] == "profile_incomplete"
    world.with_profile(client)
    luis = world.owner("luis", [profile(9)])
    (theirs,) = world.ids(luis)
    for location_id in (theirs, "loc_doesnotexist", "x"):
        r = client.post(
            f"{BILLING}/seats", json={"plan": "pro", "location_id": location_id}, headers=HEADER
        )
        assert r.status_code == 404
    r = client.post(f"{BILLING}/seats", json={"plan": "gratis", "location_id": bar}, headers=HEADER)
    assert r.status_code == 422


def test_buying_a_seat_opens_checkout_and_paying_activates_the_profile():
    world = World()
    client = world.owner()
    world.with_profile(client)
    bar, _ = world.ids(client)
    r = client.post(f"{BILLING}/seats", json={"plan": "pro", "location_id": bar}, headers=HEADER)
    assert r.status_code == 202
    request_id = r.json()["request_id"]
    assert client.get(f"{BILLING}/requests/{request_id}").json() == {
        "state": "pendiente",
        "url": None,
        "code": None,
        "detail": None,
    }
    # A second tab: the same request, no second Checkout.
    again = client.post(
        f"{BILLING}/seats", json={"plan": "pro", "location_id": bar}, headers=HEADER
    )
    assert again.status_code == 409
    assert again.json()["code"] == "seat_pending" and again.json()["request_id"] == request_id
    world.process()
    ready = client.get(f"{BILLING}/requests/{request_id}").json()
    assert ready["state"] == "lista" and is_stripe_url(ready["url"])
    assert ready["url"].startswith("https://checkout.stripe.com/")
    # Checkout open and not expired: still the same request.
    assert (
        client.post(
            f"{BILLING}/seats", json={"plan": "pro", "location_id": bar}, headers=HEADER
        ).json()["request_id"]
        == request_id
    )
    (checkout,) = world.payments.checkouts.values()
    assert checkout["price_key"].value == "pro_first_yearly"
    assert checkout["treatment"].value == "iva_21"
    # Stripe got the fiscal data and our account id, nothing of Google.
    (customer,) = world.payments.customers.values()
    assert customer.tax_id == "B12345674" and customer.tax_id_type == "es_cif"

    session_id = ready["url"].rsplit("/", 1)[1]
    world.payments.complete_checkout(session_id)
    world.stripe("checkout.session.completed", session_id)

    body = client.get(ME).json()
    assert by_id(body)[bar]["plan"] == "pro" and by_id(body)[bar]["active"] is True
    (seat,) = body["billing"]["seats"]
    assert seat["plan"] == "pro" and seat["location_id"] == bar and seat["status"] == "active"
    assert seat["payment_method"] == "card" and seat["collection"] == "auto"
    assert seat["cancel_at_period_end"] is False and seat["next_price"] == 3500
    assert body["billing"]["new_seat_price"] == {"pro": 3325, "pro_whatsapp": 5900}
    assert body["billing"]["blocked"] is None
    assert next(r for r in world.store.locations if r.id == bar).plan is Plan.PRO
    # The beta entitlement plus the one paid.
    account = world.store.accounts["ana"]
    assert sorted(e.quantity for e in account.entitlements) == [1, 3]


def test_the_same_event_twice_or_out_of_order_ends_in_the_same_state():
    world = World()
    client = world.owner()
    world.with_profile(client)
    bar, _ = world.ids(client)
    sub_id = world.buy(client, bar)
    before = (dict(world.billing.seat_rows), len(world.billing.paid))
    sub = run(world.payments.subscription(sub_id))
    # invoice.paid arriving after checkout.session.completed, and both again.
    world.stripe("invoice.paid", sub.latest_invoice_id)
    world.stripe("customer.subscription.updated", sub_id)
    world.stripe("invoice.paid", sub.latest_invoice_id)
    after = (dict(world.billing.seat_rows), len(world.billing.paid))
    assert before == after and len(world.billing.paid) == 1


def test_a_second_seat_pays_5_percent_less():
    world = World()
    client = world.owner()
    world.with_profile(client)
    bar, shop = world.ids(client)
    world.buy(client, bar)
    world.buy(client, shop)
    keys = sorted(c["price_key"].value for c in world.payments.checkouts.values())
    assert keys == ["pro_additional_yearly", "pro_first_yearly"]
    plans = {loc["id"]: loc["plan"] for loc in client.get(ME).json()["locations"]}
    assert plans == {bar: "pro", shop: "pro"}


def test_two_payments_for_one_profile_activate_one_seat_and_leave_the_other_free():
    world = World()
    client = world.owner()
    world.with_profile(client)
    bar, _ = world.ids(client)
    first = client.post(
        f"{BILLING}/seats", json={"plan": "pro", "location_id": bar}, headers=HEADER
    )
    world.process()
    url_one = client.get(f"{BILLING}/requests/{first.json()['request_id']}").json()["url"]
    # The first Checkout expires (from our side) while its page is still open.
    rid = first.json()["request_id"]
    world.billing.requests[rid] = replace(
        world.billing.requests[rid], expires_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    second = client.post(
        f"{BILLING}/seats", json={"plan": "pro", "location_id": bar}, headers=HEADER
    )
    assert second.status_code == 202
    world.process()
    url_two = client.get(f"{BILLING}/requests/{second.json()['request_id']}").json()["url"]
    for url in (url_one, url_two):
        session_id = url.rsplit("/", 1)[1]
        world.payments.complete_checkout(session_id)
        world.stripe("checkout.session.completed", session_id)
    seats = sorted(world.billing.seat_rows.values(), key=lambda s: s.created_at)
    assert [s.location_id for s in seats] == [bar, None]
    assert seats[1].review_reason == "duplicate_payment"  # for the backoffice; no refund
    assert world.payments.refunds == {}
    shown = client.get(ME).json()["billing"]["seats"]
    assert [s["location_id"] for s in shown] == [bar, None]


# --- non-payment: the whole account blocked ------------------------------------------


def _blocked_world():
    world = World()
    client = world.owner()
    world.with_profile(client)
    bar, shop = world.ids(client)
    sub_id = world.buy(client, bar)
    invoice = world.payments.renew(sub_id, card="declined")
    world.stripe("invoice.payment_failed", invoice.id)
    return world, client, bar, shop, sub_id, invoice


def test_a_failed_renewal_blocks_the_whole_account_at_once():
    world, client, bar, shop, _, _ = _blocked_world()
    me = client.get(ME)
    assert me.status_code == 200
    blocked = me.json()["billing"]["blocked"]
    assert blocked["reason"] == "payment_failed"
    for path in ("/api/v1/me/summary", "/api/v1/me/reviews", "/api/v1/me/key-dates"):
        r = client.get(path, params={"location": bar})
        assert r.status_code == 402 and r.json()["code"] == "account_blocked", path
    r = client.post("/api/v1/me/location", json={"location_id": bar}, headers=HEADER)
    assert r.status_code == 402 and r.json()["code"] == "account_blocked"
    r = client.post(activation(shop), json={"plan": "gratis"}, headers=HEADER)
    assert r.status_code == 402 and r.json()["code"] == "account_blocked"
    # What still answers: billing, the channels list, deactivating and leaving.
    assert client.get(f"{BILLING}/profile").status_code == 200
    assert client.get(f"{BILLING}/invoices").json() == {"invoices": []}
    assert client.get("/api/v1/me/channels").status_code == 200
    assert client.get("/api/v1/me/offboarding/code").status_code == 200
    r = client.post(f"{BILLING}/seats", json={"plan": "pro", "location_id": shop}, headers=HEADER)
    assert r.status_code == 409 and r.json()["code"] == "account_blocked"
    assert client.delete(activation(shop), headers=HEADER).status_code == 200


def test_paying_the_pending_invoice_unblocks_the_account():
    world, client, bar, _, sub_id, invoice = _blocked_world()
    r = client.post(f"{BILLING}/pay", headers=HEADER)
    assert r.status_code == 202
    world.process()
    ready = client.get(f"{BILLING}/requests/{r.json()['request_id']}").json()
    assert ready["state"] == "lista" and ready["url"] == invoice.hosted_invoice_url
    assert is_stripe_url(ready["url"])
    # The owner pays it on Stripe's page.
    world.payments.invoices[invoice.id] = replace(
        invoice, status="paid", amount_paid=invoice.total, payment_intent_status="succeeded"
    )
    seat = world.payments.seats[sub_id]
    seat.subscription = replace(seat.subscription, status="active", latest_invoice_status="paid")
    world.stripe("invoice.paid", invoice.id)
    assert client.get(ME).json()["billing"]["blocked"] is None
    assert client.get("/api/v1/me/summary", params={"location": bar}).status_code != 402


def test_a_card_declined_on_the_first_purchase_blocks_nothing():
    world = World()
    client = world.owner()
    world.with_profile(client)
    bar, _ = world.ids(client)
    r = client.post(f"{BILLING}/seats", json={"plan": "pro", "location_id": bar}, headers=HEADER)
    world.process()
    session_id = (
        client.get(f"{BILLING}/requests/{r.json()['request_id']}").json()["url"].rsplit("/", 1)[1]
    )
    world.payments.card = "declined"
    sub = world.payments.complete_checkout(session_id)
    world.stripe("invoice.payment_failed", sub.latest_invoice_id)
    body = client.get(ME).json()
    assert body["billing"]["blocked"] is None and body["billing"]["seats"] == []
    assert by_id(body)[bar]["active"] is False


def test_a_dispute_blocks_the_account():
    world = World()
    client = world.owner()
    world.with_profile(client)
    bar, _ = world.ids(client)
    sub_id = world.buy(client, bar)
    invoice_id = run(world.payments.subscription(sub_id)).latest_invoice_id
    dispute = world.payments.open_dispute(invoice_id)
    world.stripe("charge.dispute.created", dispute.id)
    assert client.get(ME).json()["billing"]["blocked"]["reason"] == "dispute"


# --- cancel, resume, portal ----------------------------------------------------------


def test_cancelling_and_resuming_a_seat():
    world = World()
    client = world.owner()
    world.with_profile(client)
    bar, _ = world.ids(client)
    sub_id = world.buy(client, bar)
    (seat,) = client.get(ME).json()["billing"]["seats"]
    r = client.post(f"{BILLING}/seats/{seat['id']}/cancel", headers=HEADER)
    assert r.status_code == 202
    world.process()
    assert client.get(f"{BILLING}/requests/{r.json()['request_id']}").json()["state"] == "lista"
    assert world.payments.seats[sub_id].subscription.cancel_at_period_end is True
    (seat,) = client.get(ME).json()["billing"]["seats"]
    assert seat["cancel_at_period_end"] is True and seat["next_price"] is None
    r = client.post(f"{BILLING}/seats/{seat['id']}/resume", headers=HEADER)
    world.process()
    assert world.payments.seats[sub_id].subscription.cancel_at_period_end is False
    # A seat of another account, or made up: 404.
    luis = world.owner("luis", [profile(9)])
    assert luis.post(f"{BILLING}/seats/{seat['id']}/cancel", headers=HEADER).status_code == 404
    assert client.post(f"{BILLING}/seats/seat_0000000000/cancel", headers=HEADER).status_code == 404
    assert client.post(f"{BILLING}/seats/nope/cancel", headers=HEADER).status_code == 404


def test_portal_and_pay_without_a_customer_fail_with_a_code():
    world = World()
    client = world.owner()
    for path in ("portal", "pay"):
        r = client.post(f"{BILLING}/{path}", headers=HEADER)
        world.process()
        failed = client.get(f"{BILLING}/requests/{r.json()['request_id']}").json()
        assert failed["state"] == "fallida" and failed["code"] == "no_customer"
        assert failed["url"] is None and failed["detail"]


def test_the_portal_is_a_stripe_url():
    world = World()
    client = world.owner()
    world.with_profile(client)
    world.buy(client, world.ids(client)[0])
    r = client.post(f"{BILLING}/portal", headers=HEADER)
    world.process()
    url = client.get(f"{BILLING}/requests/{r.json()['request_id']}").json()["url"]
    assert url.startswith("https://billing.stripe.com/")


def test_a_request_of_another_account_is_a_404():
    world = World()
    ana = world.owner()
    request_id = ana.post(f"{BILLING}/pay", headers=HEADER).json()["request_id"]
    luis = world.owner("luis", [profile(9)])
    for rid in (request_id, "00000000-0000-0000-0000-000000000000", "x"):
        assert luis.get(f"{BILLING}/requests/{rid}").status_code == 404
    assert ana.get(f"{BILLING}/requests/{request_id}").status_code == 200


# --- the 402 of the activation ------------------------------------------------------


def test_without_room_the_activation_offers_a_seat_at_todays_price():
    world = World()
    client = world.owner("ana", [profile(n) for n in range(1, 6)])
    ids = world.ids(client)
    for location_id in ids[:4]:  # 3 Pro of the beta (still granted) and the free one
        assert client.post(activation(location_id), json={}, headers=HEADER).status_code == 200
    r = client.post(activation(ids[4]), json={"plan": "pro_whatsapp"}, headers=HEADER)
    assert r.status_code == 402
    assert r.json() == {
        "detail": (
            "Esta ficha necesita una plaza Pro+: 59,00 € + IVA por un año. Se renueva sola "
            "salvo que la canceles. Pásate a Pro o a Pro+ para activarla."
        ),
        "code": "no_seat",
        "offer": {"plan": "pro_whatsapp", "price": 5900},
        "charging": True,
    }


# --- the webhook ----------------------------------------------------------------------


def test_the_webhook_refuses_a_wrong_or_old_signature_and_keeps_nothing():
    world = World()
    client = TestClient(world.app)
    body = event_body("evt_bad0001", "invoice.paid", "in_x")
    for signature in ("", "t=1,v1=00", sign(body, "whsec_otro" + "x" * 30)):
        r = client.post(WEBHOOK, content=body, headers={"Stripe-Signature": signature})
        assert r.status_code == 400
    old = sign(body, at=int(time.time()) - 301)
    assert client.post(WEBHOOK, content=body, headers={"Stripe-Signature": old}).status_code == 400
    assert world.billing.events == {} and world.billing.jobs == []


def test_the_webhook_keeps_each_event_once_and_discards_the_other_mode():
    world = World()
    client = TestClient(world.app)
    body = event_body("evt_once0001", "invoice.paid", "in_x")
    for _ in range(2):
        r = client.post(WEBHOOK, content=body, headers={"Stripe-Signature": sign(body)})
        assert r.status_code == 200
    assert list(world.billing.events) == ["evt_once0001"]
    assert world.billing.jobs == [(EVENT_JOB, {"event_id": "evt_once0001"})]
    live = event_body("evt_live0001", "invoice.paid", "in_x", livemode=True)
    r = client.post(WEBHOOK, content=live, headers={"Stripe-Signature": sign(live)})
    assert r.status_code == 200 and "evt_live0001" not in world.billing.events


def test_without_its_secret_the_webhook_does_not_exist():
    world = World(webhook=False)
    body = event_body("evt_none0001", "invoice.paid", "in_x")
    r = TestClient(world.app).post(WEBHOOK, content=body, headers={"Stripe-Signature": sign(body)})
    assert r.status_code == 404


def test_a_made_up_object_is_not_found_and_a_foreign_subscription_is_an_orphan():
    world = World()
    client = world.owner()
    world.with_profile(client)
    world.stripe("invoice.paid", "in_doesnotexist")
    assert world.billing.outcomes["evt_test0001"] == "not_found"
    # A subscription of a Stripe customer that is not the one of any account of ours.
    details = CustomerDetails(
        "Otra S.L.", "12345678Z", "es_cif", Address("Calle 1", "18001", "Granada", "ES"), "x@x.es"
    )
    customer_id = run(world.payments.create_customer("otra", details, idempotency_key="k1"))
    stray = world.payments._subscribe(
        customer_id, PriceKey.PRO_FIRST, TaxTreatment.IVA_21, "otra-cuenta", "seat_0000000099"
    )
    world.stripe("customer.subscription.updated", stray.id)
    assert world.billing.outcomes["evt_test0002"] == "orphan"


def test_an_event_of_the_other_mode_reaching_the_process_is_discarded():
    world = World()
    run(
        world.billing.record_stripe_event(
            "evt_x0001", "invoice.paid", "in_x", True, datetime.now(UTC)
        )
    )
    world.process()
    assert world.billing.outcomes["evt_x0001"] == "livemode_mismatch"
    assert world.payments.calls == []


# --- small pieces ---------------------------------------------------------------------


def test_only_https_urls_of_stripe_are_handed_to_the_web():
    assert is_stripe_url("https://checkout.stripe.com/c/pay/cs_1")
    assert is_stripe_url("https://invoice.stripe.com/i/acct/x")
    assert is_stripe_url("https://billing.stripe.com/p/session/x")
    for bad in (
        None,
        "",
        "http://checkout.stripe.com/x",
        "https://checkout.stripe.com.evil.es/x",
        "https://evilstripe.com/x",
        "https://user@checkout.stripe.com/x",
        "https://checkout.stripe.com:8443/x",
        "https://fichaitor.es/app",
        "javascript:alert(1)",
    ):
        assert not is_stripe_url(bad), bad


def test_checkout_comes_back_to_the_panel_with_only_two_parameters():
    ok, cancel = checkout_urls("https://fichaitor.es/", "loc_3f2a9b1c0d4e")
    assert ok == "https://fichaitor.es/app?pago=ok&ficha=loc_3f2a9b1c0d4e"
    assert cancel == "https://fichaitor.es/app?pago=cancelado&ficha=loc_3f2a9b1c0d4e"


def test_a_request_whose_attempts_run_out_fails_as_unavailable():
    world = World()
    client = world.owner()
    request_id = client.post(f"{BILLING}/pay", headers=HEADER).json()["request_id"]
    world.billing.jobs.clear()
    run(world.requests.on_exhausted(Job("j", REQUEST_JOB, {"request_id": request_id})))
    failed = client.get(f"{BILLING}/requests/{request_id}").json()
    assert (failed["state"], failed["code"]) == ("fallida", "unavailable")


def test_seat_state_values_are_the_ones_of_the_contract():
    assert {s.value for s in SeatStatus} >= {"active", "payment_failed", "canceled"}


# --- Telegram: a key date of a profile now in Gratis ----------------------------------


def test_a_key_date_of_a_profile_now_free_is_cancelled_in_the_chat_with_a_kind_word():
    async def go():
        stage = conv.MemoryStage.build()
        store = stage.store
        for location_id in (stage.a, stage.b):
            store.locations[location_id] = replace(store.locations[location_id], plan=Plan.PRO)
        chat = await stage.link()
        holiday = await stage.holiday(stage.a, date(2026, 12, 8))
        await stage.service.tick()
        message = stage.about(chat, holiday)
        # The profile moves to the free plan before the owner answers.
        store.locations[stage.a] = MemoryLocation(stage.account, conv.TITLE_A, plan=Plan.GRATIS)
        await stage.press(chat, message, "Cierro")
        proposal = await stage.proposal(holiday)
        assert proposal.state is S.CANCELLED
        assert proposal.outcome_reason == OUTCOME_PLAN_REQUIRED
        assert stage.actions.requests == []  # Google untouched
        last = stage.last(chat)
        assert last.key == "keydate.plan_required"
        assert "pásate a Pro" in last.text and "https://panel.test/app" in last.text
        assert (await stage.lane(stage.a)) is None

    run(go())


def test_a_key_date_of_a_pro_profile_is_still_confirmed():
    async def go():
        stage = conv.MemoryStage.build()
        store = stage.store
        store.locations[stage.a] = replace(store.locations[stage.a], plan=Plan.PRO)
        chat = await stage.link()
        holiday = await stage.holiday(stage.a, date(2026, 12, 8))
        await stage.service.tick()
        await stage.press(chat, stage.about(chat, holiday), "Cierro")
        assert len(stage.actions.requests) == 1

    run(go())


def test_the_paywall_texts_never_send_to_support():
    from app.api import me, session

    assert "scríbenos" not in me.MSG_NO_ROOM
    assert "Pásate a Pro" in session.MSG_KEY_DATES_PRO


def test_the_jobs_the_api_enqueues_carry_only_ids():
    world = World()
    client = world.owner()
    client.post(f"{BILLING}/pay", headers=HEADER)
    ((kind, payload),) = world.billing.jobs
    assert kind == REQUEST_JOB and set(payload) == {"request_id"}


def test_seats_in_the_me_block_hide_unpaid_checkouts():
    world = World()
    client = world.owner()
    world.with_profile(client)
    bar, _ = world.ids(client)
    client.post(f"{BILLING}/seats", json={"plan": "pro", "location_id": bar}, headers=HEADER)
    world.process()
    assert len(world.billing.seat_rows) == 1  # pending, for the Checkout
    assert client.get(ME).json()["billing"]["seats"] == []
