"""Key dates end to end, in memory: the planner that creates the questions, the chat
that answers them, the panel routes, the door that turns a confirmation into an action
and the executor. No database and no real call to Google or Telegram."""

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from test_conversation_service import ACC, START, World
from test_reviews_api import BAR, NOW, PRIVATE, SUB, summary
from test_reviews_api import HEADER as CSRF
from test_reviews_api import World as ApiWorld

from app.adapters.proposal_actions.special_hours import (
    ProposalActionsByType,
    SpecialHoursActions,
)
from app.adapters.store.memory import MemoryStore, OffboardingRow
from app.application.conversation_service import ActionOutcome
from app.domain import actions
from app.domain.calendar import Calendar, Holiday
from app.domain.channel_jobs import PLAN_KEY_DATES, plan_key_dates_run_at
from app.domain.conversation import timezone_for
from app.domain.jobs import Job
from app.domain.profile_summary import ProfileFacts
from app.domain.proposals import Proposal, ProposalType
from app.domain.proposals import ProposalState as S
from app.ports import proposal_actions as pa
from app.ports.conversation_store import KeyDateProfile
from app.ports.proposal_actions import ActionRequest, InMemoryProposalActions
from app.worker.channel_planner import ChannelPlanner
from app.worker.plan_key_dates import PlanKeyDates
from app.worker.set_special_hours import KIND as HOURS_KIND
from app.worker.set_special_hours import SetSpecialHours

KEYS = "/api/v1/me/key-dates"
MADRID = timezone_for("Europe/Madrid")


def run(coro):
    return asyncio.run(coro)


# =============================================================================
# The planner and the chat
# =============================================================================

CAL = Calendar(
    holidays=(
        Holiday(date(2026, 11, 16), "Festivo de prueba", "AN"),
        Holiday(date(2026, 11, 17), "Festivo nacional", "national"),
        Holiday(date(2026, 11, 16), "Festivo de Madrid", "MD"),
    )
)


class Profiles:
    def __init__(self, *profiles):
        self.profiles = list(profiles)

    async def key_date_profiles(self):
        return self.profiles


def profile(region="AN", address="", special=()):
    return KeyDateProfile(ACC, "loc_bar1", region, address, tuple(special))


@pytest.fixture
def w():
    world = World()
    world.link()
    return world


def plan(w, *profiles):
    job = PlanKeyDates(Profiles(*profiles), w.service, CAL, clock=lambda: w.now)
    run(job(Job("j1", PLAN_KEY_DATES, {})))


def key_date_proposals(w):
    return [p for p in w.store.proposals.values() if p.type is ProposalType.KEY_DATE]


def test_the_planner_creates_one_proposal_per_profile_and_day(w):
    plan(w, profile())
    proposals = key_date_proposals(w)
    assert sorted(p.source_ref for p in proposals) == ["loc_bar1:2026-11-16", "loc_bar1:2026-11-17"]
    assert all(p.payload["location_id"] == "loc_bar1" for p in proposals)
    plan(w, profile())  # the next day, or a retry: nothing new
    assert len(key_date_proposals(w)) == 2


def test_the_region_comes_from_the_address_when_the_sync_did_not_store_it(w):
    plan(w, profile(region="", address="Calle Mayor 1, 28013 Madrid"))
    names = sorted(p.payload["name"] for p in key_date_proposals(w))
    assert names == ["Festivo de Madrid", "Festivo nacional"]


def test_without_a_region_only_national_holidays(w):
    plan(w, profile(region="", address="Sin código postal"))
    assert [p.payload["name"] for p in key_date_proposals(w)] == ["Festivo nacional"]


def test_a_day_with_special_hours_in_google_is_not_asked(w):
    plan(w, profile(special=[date(2026, 11, 16)]))
    assert [p.payload["date"] for p in key_date_proposals(w)] == ["2026-11-17"]


def test_the_question_goes_out_and_the_answer_names_the_profile(w):
    plan(w, profile())
    assert w.last().key == "holiday.ask"
    w.press("Cierro")
    (request,) = w.requests
    assert request.type == "set_special_hours" and request.location_id == "loc_bar1"


def test_the_planner_respects_the_quiet_hours(w):
    w.at(datetime(2026, 11, 12, 21, 30, tzinfo=UTC))  # 22:30 in Madrid
    sent = len(w.sent())
    plan(w, profile())
    assert len(w.sent()) == sent  # created, not sent
    assert {p.state for p in key_date_proposals(w)} == {S.SCHEDULED}
    w.at(datetime(2026, 11, 13, 8, 0, tzinfo=UTC))  # 9:00 in Madrid
    w.tick()
    assert w.last().key == "holiday.ask"


def test_nochebuena_is_asked_like_a_holiday_with_its_own_text(w):
    from app.application.conversation_service import KeyDateInput

    w.run(
        w.service.submit_key_date(
            ACC,
            KeyDateInput(
                "loc_bar1:2026-12-24",
                "season",
                date(2026, 12, 24),
                "Nochebuena",
                location_id="loc_bar1",
                ask_hours=True,
            ),
        )
    )
    message = w.last()
    assert message.text == (
        "El jueves 24 de diciembre es Nochebuena. ¿Abres?\n"
        "Si no me dices nada, tu ficha mostrará el horario normal."
    )
    assert message.titles == ("Abro normal", "Cierro", "Otro horario")
    w.say("de 10 a 14")
    w.press("Sí, enviar")
    assert w.requests[0].payload["special_hours"] == [
        {"date": "2026-12-24", "open": "10:00", "close": "14:00"}
    ]


def test_three_periods_are_not_guessed(w):
    plan(w, profile())
    w.say("de 9 a 11 y de 12 a 14 y de 17 a 20")
    assert w.last().key == "hours.clarify_1" and not w.requests


def test_a_profile_no_longer_selected_ends_the_proposal_with_a_clear_text(w):
    w.actions.reject_with = pa.REJECT_TARGET_GONE
    plan(w, profile())
    proposal_id = next(p.id for p in key_date_proposals(w) if p.payload["date"] == "2026-11-16")
    w.press("Cierro")
    assert w.keys()[-2:] == ["keydate.gone", "holiday.ask"]  # then the next day is asked
    assert w.proposal(proposal_id).state is S.DISMISSED


@pytest.mark.parametrize(
    ("error", "key"), [("too_late", "hours.too_late"), ("no_regular_hours", "hours.no_regular")]
)
def test_the_executor_failure_is_told_with_its_reason(w, error, key):
    plan(w, profile())
    proposal_id = next(p.id for p in key_date_proposals(w) if p.payload["date"] == "2026-11-16")
    w.press("Abro normal")
    w.run(w.service.handle_action_result(proposal_id, ActionOutcome(False, error)))
    assert w.last().key == key and "lunes 16 de noviembre" in w.last().text
    assert w.proposal(proposal_id).state is S.FAILED


def test_the_planner_enqueues_the_daily_planning_once_a_day():
    from app.adapters.job_queue.memory import MemoryJobQueue

    now = {"t": datetime(2026, 11, 12, 6, 0, tzinfo=UTC)}  # 7:00 in Madrid
    queue = MemoryJobQueue(lambda: now["t"])
    planner = ChannelPlanner(queue, Profiles(), lambda: now["t"], plan_kind=PLAN_KEY_DATES)

    class NoLinked:
        async def linked_profiles(self):
            return []

    planner._profiles = NoLinked()
    assert run(planner.plan_once())["plan_key_dates"] == 1
    assert run(planner.plan_once())["plan_key_dates"] == 0
    job = next(j for j in queue.jobs.values() if j.kind == PLAN_KEY_DATES)
    assert job.run_at == datetime(2026, 11, 12, 8, 5, tzinfo=UTC)  # 9:05 in Madrid
    now["t"] += timedelta(days=1)
    assert run(planner.plan_once())["plan_key_dates"] == 1
    late = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)
    assert plan_key_dates_run_at(late, MADRID) == late


def test_without_the_key_dates_the_planner_enqueues_what_it_did():
    from app.adapters.job_queue.memory import MemoryJobQueue

    class NoLinked:
        async def linked_profiles(self):
            return []

    queue = MemoryJobQueue(lambda: START)
    created = run(ChannelPlanner(queue, NoLinked(), lambda: START).plan_once())
    assert "plan_key_dates" not in created


# =============================================================================
# The door: a confirmation by the chat becomes an action
# =============================================================================


class HoursStore:
    def __init__(self, result="act_0000000000000001", error=None):
        self.result, self.error, self.calls = result, error, []

    async def create_special_hours_action(self, account_id, location_id, change, now):
        self.calls.append((account_id, location_id, change))
        if self.error:
            raise self.error
        return self.result


def request(**changes):
    values = {
        "account_id": ACC,
        "proposal_id": "prop_0000000000000001",
        "type": "set_special_hours",
        "source_ref": "loc_bar1:2026-12-08",
        "payload": {"special_hours": [{"date": "2026-12-08", "closed": True}]},
        "idempotency_key": "prop_0000000000000001:set_special_hours:1",
        "confirmed_at": START,
        "confirmed_channel": "telegram",
        "confirmed_event_id": "e1",
        "location_id": "loc_bar1",
    }
    return ActionRequest(**{**values, **changes})


def test_the_door_creates_the_action_for_the_profile_of_the_proposal():
    store = HoursStore()
    receipt = run(SpecialHoursActions(store).create(request()))
    assert receipt.action_id == "act_0000000000000001"
    ((account, location, change),) = store.calls
    assert (account, location, change.day, change.kind) == (
        ACC,
        "loc_bar1",
        date(2026, 12, 8),
        "closed",
    )


@pytest.mark.parametrize(
    ("changes", "store", "code"),
    [
        (
            {
                "payload": {
                    "special_hours": [{"date": "2026-12-08", "open": "20:00", "close": "2:00"}]
                }
            },
            HoursStore(),
            pa.REJECT_INVALID,
        ),
        ({"location_id": ""}, HoursStore(), pa.REJECT_TARGET_GONE),
        ({}, HoursStore(result=None), pa.REJECT_TARGET_GONE),
        ({"type": "reply_review"}, HoursStore(), pa.REJECT_UNAVAILABLE),
    ],
)
def test_the_door_refuses_what_it_must(changes, store, code):
    with pytest.raises(pa.ActionRejected) as caught:
        run(SpecialHoursActions(store).create(request(**changes)))
    assert caught.value.code == code


def test_one_door_per_type():
    replies, hours = InMemoryProposalActions(), InMemoryProposalActions()
    doors = ProposalActionsByType({"reply_review": replies, "set_special_hours": hours})
    run(doors.create(request()))
    assert hours.requests and not replies.requests
    with pytest.raises(pa.ActionRejected):
        run(ProposalActionsByType({}).create(request()))


# =============================================================================
# The panel
# =============================================================================

DAY = (NOW + timedelta(days=3)).date()  # 2027-01-18, a Monday
WEEKLY = ((0, "09:00", "14:00"), (0, "17:00", "20:00"), (1, "09:00", "14:00"))


class Panel:
    """An owner with a selected profile, a key date proposal created by the chat, and
    the real executor on the shared tables."""

    def __init__(self, state=S.SENT, day=DAY, weekly=WEEKLY):
        self.api = ApiWorld()
        self.client = self.api.owner()
        self.api.store.put_summary(
            SUB, BAR.location, summary(facts=ProfileFacts(regular_hours=weekly))
        )
        account = self.api.store.accounts[SUB]
        self.account_id = account.id
        self.location_id = account.selected_location_id
        # Pro: the free plan has no key dates (app/domain/plans.py).
        changed = self.client.post(
            f"/api/v1/me/locations/{self.location_id}/activation",
            json={"plan": "pro"},
            headers={"X-Requested-With": "fichaitor"},
        )
        assert changed.status_code == 200
        self.proposal_id = self.add(state, day)
        self.api.worker._registry.register(
            HOURS_KIND,
            SetSpecialHours(
                self.api.worker_store,
                self.api.gbp,
                PRIVATE,
                lambda: datetime.fromtimestamp(self.api.clock(), UTC),
            ),
        )

    def add(self, state, day, account_id=None, location_id=None, pid="prop_00000000000000a1"):
        proposal = Proposal(
            id=pid,
            account_id=account_id or self.account_id,
            type=ProposalType.KEY_DATE,
            source_ref=f"{location_id or self.location_id}:{day}",
            state=state,
            priority=10,
            created_at=NOW,
            scheduled_for=NOW,
            payload={
                "kind": "holiday",
                "date": day.isoformat(),
                "name": "Festivo de prueba",
                "location_id": location_id or self.location_id,
            },
            step="holiday.ask",
            expires_at=datetime(day.year, day.month, day.day, tzinfo=UTC),
        )
        self.api.store.key_date_proposals[proposal.id] = proposal
        return proposal.id

    def proposal(self):
        return self.api.store.key_date_proposals[self.proposal_id]

    def answer(self, body, headers=CSRF, pid=None):
        return self.client.post(
            f"{KEYS}/{pid or self.proposal_id}/answer", json=body, headers=headers
        )

    def work(self):
        while run(self.api.worker.run_once()):
            pass


def test_the_list_has_the_shape_of_the_card():
    p = Panel()
    r = p.client.get(KEYS)
    assert r.status_code == 200
    (item,) = r.json()["dates"]
    assert item["id"] == p.proposal_id and item["date"] == "2027-01-18"
    assert item["name"] == "Festivo de prueba"
    assert item["regular_hours"] == [
        {"open": "09:00", "close": "14:00"},
        {"open": "17:00", "close": "20:00"},
    ]
    assert item["status"] == "pendiente" and item["answerable"] is True
    assert item["date_text"] == "lunes 18 de enero"


def test_regular_hours_are_null_without_them_and_empty_on_a_closed_weekday():
    assert Panel(weekly=()).client.get(KEYS).json()["dates"][0]["regular_hours"] is None
    sunday = Panel(day=DAY + timedelta(days=6))
    assert sunday.client.get(KEYS).json()["dates"][0]["regular_hours"] == []


def test_answer_closed_creates_the_action_and_the_executor_sends_it():
    p = Panel()
    r = p.answer({"kind": "closed"})
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "en_curso"
    assert r.json()["answer"] == {"kind": "closed", "ranges": [], "text": "cerrado"}
    proposal = p.proposal()
    assert proposal.state is S.EXECUTING
    assert proposal.confirmed_event_id.startswith("panel:")
    action = p.api.store.tables.actions[proposal.action_id]
    assert action.type == "set_special_hours" and action.special_date == DAY
    assert [a.event for a in p.api.store.tables.audit] == ["special_hours_confirmed"]
    p.work()
    assert action.state == actions.DONE
    (period,) = p.api.gbp.special_of(BAR.location)
    assert period == {"startDate": {"year": 2027, "month": 1, "day": 18}, "closed": True}
    item = p.client.get(KEYS, params={"filter": "all"}).json()["dates"][0]
    assert item["action"]["status"] == "hecha"


def test_answer_custom_ranges():
    p = Panel()
    body = {
        "kind": "custom",
        "ranges": [{"open": "10:00", "close": "14:00"}, {"open": "17:00", "close": "20:30"}],
    }
    r = p.answer(body)
    assert r.status_code == 202
    assert r.json()["answer"]["ranges"] == body["ranges"]
    assert r.json()["answer"]["text"] == "de 10:00 a 14:00 y de 17:00 a 20:30"


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "custom", "ranges": []},
        {"kind": "custom", "ranges": [{"open": "9:00", "close": "10:00"}] * 3},
        {"kind": "custom", "ranges": [{"open": "20:00", "close": "02:00"}]},
        {
            "kind": "custom",
            "ranges": [{"open": "09:00", "close": "14:00"}, {"open": "13:00", "close": "18:00"}],
        },
        {"kind": "custom", "ranges": [{"open": "9h", "close": "14h"}]},
        {"kind": "closed", "ranges": [{"open": "09:00", "close": "14:00"}]},
    ],
)
def test_invalid_ranges_are_refused_with_a_message(body):
    p = Panel()
    r = p.answer(body)
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)
    assert p.proposal().state is S.SENT and not p.api.store.tables.actions


def test_a_proposal_not_pending_is_a_conflict():
    p = Panel()
    assert p.answer({"kind": "closed"}).status_code == 202
    again = p.answer({"kind": "regular"})
    assert again.status_code == 409 and isinstance(again.json()["detail"], str)
    assert len(p.api.store.tables.actions) == 1


def test_a_day_that_has_passed_is_a_conflict():
    p = Panel(day=NOW.date() - timedelta(days=1))
    assert p.answer({"kind": "closed"}).status_code == 409
    assert p.client.get(KEYS).json()["dates"] == []


def test_on_the_day_itself_an_expired_one_can_still_be_answered():
    p = Panel(state=S.EXPIRED, day=NOW.date())
    assert p.client.get(KEYS).json()["dates"][0]["answerable"] is True
    assert p.answer({"kind": "closed"}).status_code == 202


def test_dismiss():
    p = Panel()
    r = p.client.post(f"{KEYS}/{p.proposal_id}/dismiss", headers=CSRF)
    assert r.status_code == 204 and r.content == b""
    assert p.proposal().state is S.DISMISSED
    assert p.client.get(KEYS).json()["dates"] == []
    assert p.client.post(f"{KEYS}/{p.proposal_id}/dismiss", headers=CSRF).status_code == 409
    assert p.answer({"kind": "closed"}).status_code == 409


def test_another_accounts_proposal_or_a_bad_id_is_not_found():
    p = Panel()
    other = p.add(
        S.SENT, DAY, account_id="00000000-0000-0000-0000-00000000beef", pid="prop_00000000000000b2"
    )
    assert p.answer({"kind": "closed"}, pid=other).status_code == 404
    assert p.client.post(f"{KEYS}/{other}/dismiss", headers=CSRF).status_code == 404
    assert p.answer({"kind": "closed"}, pid="prop_x").status_code == 404
    assert p.answer({"kind": "closed"}, pid="prop_00000000000000c3").status_code == 404


def test_a_proposal_of_a_profile_no_longer_selected_is_not_answered():
    p = Panel()
    stale = p.add(S.SENT, DAY, location_id="loc_otraficha", pid="prop_00000000000000d4")
    assert p.answer({"kind": "closed"}, pid=stale).status_code == 404
    assert len(p.client.get(KEYS).json()["dates"]) == 1


def test_writes_need_the_protection_header_and_a_session():
    p = Panel()
    assert p.answer({"kind": "closed"}, headers={}).status_code == 403
    assert p.client.post(f"{KEYS}/{p.proposal_id}/dismiss").status_code == 403
    p.client.cookies.clear()
    assert p.client.get(KEYS).status_code == 401
    assert p.proposal().state is S.SENT


def test_everything_is_closed_during_a_scheduled_leave():
    p = Panel()
    p.api.store.offboardings.append(
        OffboardingRow("off1", p.account_id, "ficha", NOW, NOW + timedelta(hours=48), None)
    )
    assert p.client.get(KEYS).status_code == 409
    assert p.answer({"kind": "closed"}).status_code == 409
    assert p.client.post(f"{KEYS}/{p.proposal_id}/dismiss", headers=CSRF).status_code == 409
    assert p.proposal().state is S.SENT


def test_the_memory_store_refuses_a_second_live_change_of_the_same_day():
    store = MemoryStore()
    assert store.key_date_proposals == {}
    p = Panel()
    p.answer({"kind": "closed"})
    second = p.add(S.SENT, DAY, pid="prop_00000000000000e5")
    r = p.answer({"kind": "regular"}, pid=second)
    assert r.status_code == 409
    assert replace(p.proposal()).state is S.EXECUTING
