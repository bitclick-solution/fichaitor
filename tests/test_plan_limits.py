"""The limits of the free plan (decision of the owner, 2026-11-13; app/domain/plans.py):
25 replies drafted by the model per month and profile, and no key dates. Pro and Pro
WhatsApp, without those limits. The plan is read per profile at the moment of each
action. No database: MemoryStore. The counter in PostgreSQL is in
test_plan_limits_postgres.py.
"""

import asyncio
from datetime import UTC, datetime, timedelta

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
from app.domain.quota import ai_replies_scope, month_window
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


def test_the_month_is_the_calendar_month_in_madrid():
    # 30 November, 23:30 UTC is already 1 December in Madrid.
    start, end = month_window(datetime(2026, 11, 30, 23, 30, tzinfo=UTC))
    assert (start, end) == (
        datetime(2026, 11, 30, 23, 0, tzinfo=UTC),
        datetime(2026, 12, 31, 23, 0, tzinfo=UTC),
    )
    start, end = month_window(datetime(2026, 3, 15, 12, 0, tzinfo=UTC))
    # Summer time starts on 29 March: the month ends at 22:00 UTC.
    assert (start, end) == (
        datetime(2026, 2, 28, 23, 0, tzinfo=UTC),
        datetime(2026, 3, 31, 22, 0, tzinfo=UTC),
    )


def test_what_each_plan_includes():
    assert FEATURES[Plan.GRATIS].monthly_ai_replies == 25
    assert not FEATURES[Plan.GRATIS].key_dates and not FEATURES[Plan.GRATIS].whatsapp
    assert FEATURES[Plan.PRO].monthly_ai_replies is None and FEATURES[Plan.PRO].key_dates
    assert FEATURES[Plan.PRO_WHATSAPP].whatsapp
    with pytest.raises(PlanRequired) as caught:
        check_key_dates(Plan.GRATIS)
    assert caught.value.feature == FEATURE_KEY_DATES
    check_key_dates(Plan.PRO)
    check_key_dates(None)  # not active: answered apart


def test_the_scope_of_the_counter_fits_usage_counter():
    from app.domain.quota import SCOPE

    assert SCOPE.fullmatch(ai_replies_scope("loc_3f2a9b1c0d4e"))


# --- replies drafted by the model --------------------------------------------------


def _use_up(world: World, location_id: str, n: int) -> None:
    start, end = month_window(NOW)
    for _ in range(n):
        assert run(world.store.take_quota_in(ai_replies_scope(location_id), 25, start, end))


def _location(client) -> dict:
    return client.get("/api/v1/me").json()["locations"][0]


def test_a_free_profile_drafts_25_replies_a_month_and_then_gets_a_402():
    world = World()
    client = world.owner()
    location = _location(client)
    assert location["plan"] == "gratis"
    assert location["usage"] == {"ai_replies": {"used": 0, "limit": 25}}
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
            "Has usado las 25 respuestas con IA de este mes. Pásate a Pro para tenerlas "
            "ilimitadas. Puedes seguir escribiendo tus respuestas."
        ),
        "code": "plan_required",
        "feature": FEATURE_AI_REPLIES,
    }
    assert len(world.writer.reply_calls) == calls  # the model was not asked
    assert _location(client)["usage"]["ai_replies"] == {"used": 25, "limit": 25}
    # A draft already stored is served: the model is not asked again.
    again = client.post(draft_url(low), headers=HEADER)
    assert again.status_code == 200 and again.json()["cached"] is True
    # And the owner still writes and publishes his own replies, without a cap.
    published = client.post(
        reply_url(world.review_id("g-two")), json={"text": "Gracias."}, headers=HEADER
    )
    assert published.status_code == 202


def test_the_cap_starts_again_next_month():
    world = World()
    client = world.owner()
    location = _location(client)
    _use_up(world, location["id"], 25)
    rid = world.review_id("g-low")
    assert client.post(draft_url(rid), headers=HEADER).status_code == 402
    world.clock.now = month_window(NOW)[1].timestamp() + 60
    assert client.post(draft_url(rid), headers=HEADER).status_code == 200


def test_a_pro_profile_has_no_cap_and_its_drafts_are_still_counted():
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
    assert shown["usage"] == {"ai_replies": {"used": 26, "limit": None}}
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
