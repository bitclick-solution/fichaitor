"""The limits of the free plan (decisions of the owner, 2026-11-13; app/domain/plans.py):
25 replies drafted by the model per Google profile, once and for ever, and no key dates.
Pro and Pro WhatsApp, without those limits. The plan is read per profile at the moment
of each action. No database: MemoryStore. The counter in PostgreSQL is in
test_plan_limits_postgres.py.
"""

import asyncio
from datetime import timedelta

import pytest

from app.adapters.conversation_store.memory import MemoryConversationStore, MemoryLocation
from app.application.conversation_service import REVIEW_WRITE_ASK
from app.domain import channel_jobs
from app.domain.channel_messages import button_title
from app.domain.jobs import Job
from app.domain.plans import (
    FEATURE_AI_REPLIES,
    FEATURE_KEY_DATES,
    FEATURES,
    Plan,
    PlanRequired,
    check_key_dates,
)
from app.domain.proposals import ProposalType
from app.domain.quota import allowance_key
from app.ports.conversation_store import KeyDateProfile, ReviewTarget
from app.worker.plan_key_dates import PlanKeyDates
from app.worker.propose_review import ProposeReview
from tests import test_multinegocio_api as mn
from tests import test_multinegocio_jobs as jobs
from tests.test_reviews_api import HEADER, NOW, World, draft_url, reply_url

ACTIVATION = "/api/v1/me/locations/{}/activation"


def run(coro):
    return asyncio.run(coro)


# --- the domain ---------------------------------------------------------------------


def test_the_key_of_the_allowance_is_an_hmac_of_the_google_profile():
    key = allowance_key("pimienta", "locations/123")
    assert len(key) == 64 and all(c in "0123456789abcdef" for c in key)
    assert key == allowance_key("pimienta", "locations/123")  # the same profile, any account
    assert key != allowance_key("otra", "locations/123")  # without the pepper, not traceable
    assert key != allowance_key("pimienta", "locations/124")


def test_what_each_plan_includes():
    assert FEATURES[Plan.GRATIS].lifetime_ai_replies == 25
    assert not FEATURES[Plan.GRATIS].key_dates and not FEATURES[Plan.GRATIS].whatsapp
    assert FEATURES[Plan.PRO].lifetime_ai_replies is None and FEATURES[Plan.PRO].key_dates
    assert FEATURES[Plan.PRO_WHATSAPP].whatsapp
    with pytest.raises(PlanRequired) as caught:
        check_key_dates(Plan.GRATIS)
    assert caught.value.feature == FEATURE_KEY_DATES
    check_key_dates(Plan.PRO)
    check_key_dates(None)  # not active: answered apart


# --- replies drafted by the model --------------------------------------------------


def _use_up(world: World, location_id: str, n: int) -> None:
    account_id = next(r.account_id for r in world.store.locations if r.id == location_id)
    for _ in range(n):
        assert run(world.store.take_ai_reply(account_id, location_id, 25))


def _location(client) -> dict:
    return client.get("/api/v1/me").json()["locations"][0]


def test_a_free_profile_drafts_25_replies_for_ever_and_then_gets_a_402():
    world = World()
    client = world.owner()
    location = _location(client)
    assert location["plan"] == "gratis"
    assert location["usage"] == {"ai_replies": {"used": 0, "limit": 25, "lifetime": True}}
    assert location["features"] == {"key_dates": False, "whatsapp": False}

    low = world.review_id("g-low")
    assert client.post(draft_url(low), headers=HEADER).status_code == 200
    assert _location(client)["usage"]["ai_replies"]["used"] == 1
    _use_up(world, location["id"], 24)
    calls = len(world.writer.reply_calls)

    r = client.post(draft_url(world.review_id("g-two")), headers=HEADER)
    assert r.status_code == 402
    assert r.json() == {
        "detail": (
            "Has usado tus 25 respuestas con IA gratuitas. Pásate a Pro para tenerlas "
            "ilimitadas. Puedes seguir escribiendo tus respuestas."
        ),
        "code": "plan_required",
        "feature": FEATURE_AI_REPLIES,
    }
    assert len(world.writer.reply_calls) == calls  # the model was not asked
    assert _location(client)["usage"]["ai_replies"] == {"used": 25, "limit": 25, "lifetime": True}
    # A draft already stored is served: the model is not asked again.
    again = client.post(draft_url(low), headers=HEADER)
    assert again.status_code == 200 and again.json()["cached"] is True
    # And the owner still writes and publishes his own replies, without a cap.
    published = client.post(
        reply_url(world.review_id("g-two")), json={"text": "Gracias."}, headers=HEADER
    )
    assert published.status_code == 202


def test_the_cap_does_not_come_back_next_month_nor_years_later():
    world = World()
    client = world.owner()
    location = _location(client)
    _use_up(world, location["id"], 25)
    rid = world.review_id("g-low")
    assert client.post(draft_url(rid), headers=HEADER).status_code == 402
    world.clock.now = NOW.timestamp() + 20 * 86400  # next month (the session still holds)
    assert client.post(draft_url(rid), headers=HEADER).status_code == 402


def test_the_cap_survives_another_account_with_the_same_google_profile():
    # Deleting the account (or withdrawing the access) and connecting again, even with
    # another Google account that manages the same profile, does not give the 25 back:
    # the counter is keyed by the Google profile, not by the account.
    world = World()
    client = world.owner()
    location = _location(client)
    _use_up(world, location["id"], 25)
    assert client.delete(ACTIVATION.format(location["id"]), headers=HEADER).status_code == 200
    other = world.connect_as(sub="otra-identidad", email="socia@example.com")
    mine = _location(other)
    assert mine["id"] != location["id"]
    activated = other.post(ACTIVATION.format(mine["id"]), json={"plan": "gratis"}, headers=HEADER)
    assert activated.status_code == 200
    assert _location(other)["usage"]["ai_replies"] == {"used": 25, "limit": 25, "lifetime": True}
    account_id = next(r.account_id for r in world.store.locations if r.id == mine["id"])
    assert run(world.store.take_ai_reply(account_id, mine["id"], 25)) is False
    # Nothing in the counter names the profile, the account or an email.
    assert all(len(k) == 64 for k in world.store.free_allowance)
    assert not any("locations/" in k for k in world.store.free_allowance)


def test_a_pro_profile_has_no_cap_and_spends_nothing_of_the_free_allowance():
    world = World()
    client = world.connect_as()
    location = _location(client)
    activated = client.post(ACTIVATION.format(location["id"]), json={"plan": "pro"}, headers=HEADER)
    assert activated.status_code == 200
    world.synced()
    _use_up(world, location["id"], 25)
    r = client.post(draft_url(world.review_id("g-low")), headers=HEADER)
    assert r.status_code == 200
    shown = _location(client)
    # The draft in Pro was not counted: the 25 of the free plan stay as they were.
    assert shown["usage"] == {"ai_replies": {"used": 25, "limit": None, "lifetime": True}}
    assert shown["features"] == {"key_dates": True, "whatsapp": False}


def test_an_inactive_profile_has_no_usage_nor_features():
    world = World()
    client = world.connect_as()
    location = _location(client)
    assert location["active"] is False
    assert location["usage"] is None and location["features"] is None


# --- key dates ----------------------------------------------------------------------


def _key_date_world(plan: str):
    world = mn.World()
    client = world.owner("ana", [mn.profile(1)])
    (bar,) = world.ids(client)
    assert mn.activate(client, bar, plan).status_code == 200
    day = (mn.NOW + timedelta(days=7)).date().isoformat()
    return client, world.key_date("ana", bar, day), world.key_date("ana", bar, "2099-01-02")


def test_a_free_profile_cannot_answer_nor_dismiss_a_key_date():
    client, proposal_id, other = _key_date_world("gratis")
    url = f"/api/v1/me/key-dates/{proposal_id}"
    expected = {
        "detail": (
            "Los festivos y las fechas clave están en Pro. Pásate a Pro para que te "
            "preguntemos por ellos y cambiemos el horario en Google."
        ),
        "code": "plan_required",
        "feature": FEATURE_KEY_DATES,
    }
    r = client.post(f"{url}/answer", json={"kind": "closed"}, headers=HEADER)
    assert (r.status_code, r.json()) == (402, expected)
    r = client.post(f"/api/v1/me/key-dates/{other}/dismiss", headers=HEADER)
    assert (r.status_code, r.json()) == (402, expected)


def test_a_pro_profile_answers_and_dismisses_key_dates():
    client, proposal_id, other = _key_date_world("pro")
    url = f"/api/v1/me/key-dates/{proposal_id}"
    assert client.post(f"{url}/answer", json={"kind": "closed"}, headers=HEADER).status_code == 202
    assert client.post(f"/api/v1/me/key-dates/{other}/dismiss", headers=HEADER).status_code == 204


def test_plan_key_dates_skips_the_free_profiles():
    store = MemoryConversationStore()
    store.locations["loc_a"] = MemoryLocation(jobs.ACC, "A")
    store.locations["loc_b"] = MemoryLocation(jobs.ACC, "B")
    profiles = jobs.KeyDates(
        KeyDateProfile(jobs.ACC, "loc_a", "AN", "", (), Plan.GRATIS),
        KeyDateProfile(jobs.ACC, "loc_b", "AN", "", (), Plan.PRO),
    )
    job = PlanKeyDates(profiles, jobs._service(store), jobs.CAL, clock=lambda: jobs.NOW)
    run(job(Job("j1", channel_jobs.PLAN_KEY_DATES, {})))
    assert {p.location_id for p in store.proposals.values()} == {"loc_b"}


# --- the bot, once the free plan used its drafts ----------------------------------------


class CappedReplies:
    async def draft(self, account_id, review_id):
        raise PlanRequired(FEATURE_AI_REPLIES, 25)


def test_the_bot_tells_the_review_and_offers_to_write_the_reply_without_a_draft():
    store = MemoryConversationStore()
    store.locations["loc_a"] = MemoryLocation(jobs.ACC, "Bar A")
    service = jobs._service(store)
    target = ReviewTarget(
        stars=2,
        text="Tardaron mucho.",
        created_at=jobs.NOW - timedelta(hours=1),
        unanswered=True,
        live=True,
        targets=((jobs.ACC, "loc_a"),),
    )
    job = ProposeReview(jobs.Targets(target), CappedReplies(), service, lambda: jobs.NOW)
    run(job(Job("p1", channel_jobs.PROPOSE_REVIEW, {"review_id": "rev_00000000000000ab"})))
    (proposal,) = store.proposals.values()
    assert proposal.type is ProposalType.REVIEW_REPLY
    assert proposal.step == REVIEW_WRITE_ASK and proposal.payload["reply"] == ""
    key, text = service._first_message(proposal)
    assert key == "review.new.write"
    assert "Tardaron mucho." in text and "Propongo" not in text
    assert button_title("write") == "Escribir respuesta"
