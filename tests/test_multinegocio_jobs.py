"""Step 6b of docs/multinegocio.md, the jobs and the channel: the planner spreads the
syncs of the profiles over their slot, `plan_key_dates` asks once per profile and day,
`propose_review` proposes in the lane of each profile, and Telegram reads the quoted
message and keeps one second between two messages to the same chat."""

import asyncio
from datetime import UTC, date, datetime, timedelta

import httpx

from app.adapters.conversation_store.memory import MemoryConversationStore, MemoryLocation
from app.adapters.job_queue.memory import MemoryJobQueue
from app.adapters.messaging.fake import FakeChannel
from app.adapters.messaging.telegram import TelegramChannel, parse_update
from app.application.conversation_service import ConversationService, ServiceConfig
from app.domain import channel_jobs
from app.domain.calendar import Calendar, Holiday
from app.domain.channel_messages import InboundKind, MessageKind, OutboundMessage
from app.domain.jobs import Job
from app.domain.proposals import ProposalType
from app.ports.conversation_store import KeyDateProfile, ReviewTarget
from app.ports.messaging import MessagingUnavailable
from app.ports.proposal_actions import InMemoryProposalActions
from app.worker.channel_planner import ChannelPlanner, spread_run_at
from app.worker.plan_key_dates import PlanKeyDates
from app.worker.propose_review import ProposeReview

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)  # 11:00 in Madrid: a day slot
ACC = "acc-varias"


def run(coro):
    return asyncio.run(coro)


# --- the planner ------------------------------------------------------------------------


class Linked:
    def __init__(self, profiles):
        self.profiles = profiles

    async def linked_profiles(self):
        return self.profiles


PROFILES = [(f"sub-{n}", f"locations/{n}") for n in range(40)]


def test_the_planner_spreads_the_syncs_over_the_slot_and_never_duplicates_a_key():
    clock = {"now": NOW}
    queue = MemoryJobQueue(lambda: clock["now"])
    planner = ChannelPlanner(queue, Linked(PROFILES), lambda: clock["now"])
    assert run(planner.plan_once())["sync_profile"] == 40
    assert run(planner.plan_once())["sync_profile"] == 0  # the same slot: the same keys
    syncs = [j for j in queue.jobs.values() if j.kind == "sync_profile"]
    keys = [j.idempotency_key for j in syncs]
    assert len(set(keys)) == 40
    slot_start = datetime.fromtimestamp(
        int(NOW.timestamp()) // 1800 * 1800, UTC
    )  # the 30 minute slot `now` falls in
    run_ats = [j.run_at for j in syncs]
    assert all(slot_start <= at < slot_start + timedelta(minutes=30) for at in run_ats)
    assert len({at for at in run_ats}) > 30  # spread, not all in the same minute
    # The next slot: new keys, and the same offset for the same profile.
    clock["now"] += timedelta(minutes=30)
    assert run(planner.plan_once())["sync_profile"] == 40
    by_profile: dict[tuple, list[datetime]] = {}
    for job in queue.jobs.values():
        if job.kind == "sync_profile":
            by_profile.setdefault(tuple(job.payload.values()), []).append(job.run_at)
    for first, second in by_profile.values():
        assert second - first == timedelta(minutes=30)


def test_the_offset_of_a_profile_is_fixed_and_within_its_slot():
    night = channel_jobs.sync_slot(datetime(2026, 11, 12, 23, 0, tzinfo=UTC), _madrid())
    assert night.startswith("n")
    at = spread_run_at("sub", "locations/1", night)
    start = datetime.fromtimestamp(int(night[1:]) * 3 * 3600, UTC)
    assert start <= at < start + timedelta(hours=3)
    assert spread_run_at("sub", "locations/1", night) == at
    assert spread_run_at("sub", "locations/2", night) != at


def _madrid():
    from app.domain.conversation import timezone_for

    return timezone_for("Europe/Madrid")


# --- the key dates ----------------------------------------------------------------------

CAL = Calendar(holidays=(Holiday(date(2026, 11, 16), "Festivo nacional", "national"),))


def _service(store: MemoryConversationStore) -> ConversationService:
    return ConversationService(
        store,
        {"telegram": FakeChannel("telegram")},
        InMemoryProposalActions(),
        ServiceConfig(panel_url="https://panel.test/app", signup_url="https://alta.test"),
        clock=lambda: NOW,
    )


class KeyDates:
    def __init__(self, *profiles):
        self.profiles = list(profiles)

    async def key_date_profiles(self):
        return self.profiles


def test_plan_key_dates_creates_one_proposal_per_business_and_day():
    store = MemoryConversationStore()
    store.locations["loc_a"] = MemoryLocation(ACC, "A")
    store.locations["loc_b"] = MemoryLocation(ACC, "B")
    service = _service(store)
    profiles = KeyDates(
        KeyDateProfile(ACC, "loc_a", "AN", "", ()), KeyDateProfile(ACC, "loc_b", "AN", "", ())
    )
    job = PlanKeyDates(profiles, service, CAL, clock=lambda: NOW)
    run(job(Job("j1", channel_jobs.PLAN_KEY_DATES, {})))
    run(job(Job("j2", channel_jobs.PLAN_KEY_DATES, {})))  # a retry: nothing new
    proposals = [p for p in store.proposals.values() if p.type is ProposalType.KEY_DATE]
    assert sorted((p.location_id, p.source_ref) for p in proposals) == [
        ("loc_a", "loc_a:2026-11-16"),
        ("loc_b", "loc_b:2026-11-16"),
    ]
    assert all(p.payload["location_id"] == p.location_id for p in proposals)


def test_plan_key_dates_skips_a_business_that_is_no_longer_active():
    store = MemoryConversationStore()
    store.locations["loc_a"] = MemoryLocation(ACC, "A", active=False)
    job = PlanKeyDates(
        KeyDates(KeyDateProfile(ACC, "loc_a", "AN", "", ())), _service(store), CAL, lambda: NOW
    )
    run(job(Job("j1", channel_jobs.PLAN_KEY_DATES, {})))
    assert store.proposals == {}


# --- propose_review ---------------------------------------------------------------------


class Draft:
    text = "Muchas gracias por tu reseña."


class Replies:
    def __init__(self):
        self.asked: list[tuple[str, str]] = []

    async def draft(self, account_id, review_id):
        self.asked.append((account_id, review_id))
        return Draft()


class Targets:
    def __init__(self, target):
        self.target = target

    async def review_targets(self, review_id):
        return self.target


def test_propose_review_proposes_in_the_lane_of_each_active_business_with_it():
    store = MemoryConversationStore()
    store.locations["loc_a"] = MemoryLocation(ACC, "A")
    store.locations["loc_o"] = MemoryLocation("acc-otra", "O")
    service = _service(store)
    target = ReviewTarget(
        stars=5,
        text="Muy bien.",
        created_at=NOW - timedelta(hours=1),
        unanswered=True,
        live=True,
        accounts=(),
        targets=((ACC, "loc_a"), ("acc-otra", "loc_o")),
    )
    replies = Replies()
    job = ProposeReview(Targets(target), replies, service, lambda: NOW)
    run(job(Job("p1", channel_jobs.PROPOSE_REVIEW, {"review_id": "rev_00000000000000ab"})))
    assert {(p.account_id, p.location_id) for p in store.proposals.values()} == {
        (ACC, "loc_a"),
        ("acc-otra", "loc_o"),
    }
    assert replies.asked == [(ACC, "rev_00000000000000ab"), ("acc-otra", "rev_00000000000000ab")]


# --- Telegram -----------------------------------------------------------------------------


def test_a_text_that_quotes_a_message_carries_its_id():
    update = {
        "update_id": 7,
        "message": {
            "message_id": 50,
            "from": {"id": 42, "is_bot": False, "first_name": "Marta"},
            "chat": {"id": 42, "type": "private"},
            "text": "sí",
            "reply_to_message": {"message_id": 31, "text": "Nueva reseña"},
        },
    }
    event = parse_update(update)
    assert event is not None and event.kind is InboundKind.TEXT and event.reply_to == "31"
    del update["message"]["reply_to_message"]
    assert parse_update(update).reply_to == ""
    update["message"]["reply_to_message"] = {"message_id": "31"}  # not a number: ignored
    assert parse_update(update).reply_to == ""


class Clock:
    """A monotonic clock that only moves when the channel sleeps."""

    def __init__(self):
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


def _channel(handler, clock: Clock) -> TelegramChannel:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TelegramChannel("123:abc", client, clock=clock, sleep=clock.sleep)


def _message(text="Hola"):
    return OutboundMessage("chid_1", MessageKind.PROPOSAL, text)


def _ok(request):
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


def test_two_messages_to_the_same_chat_leave_one_second_apart_and_other_chats_do_not_wait():
    clock = Clock()
    channel = _channel(_ok, clock)

    async def go():
        await channel.send("42", _message())
        await channel.send("43", _message())  # another chat: no wait
        await channel.send("42", _message())  # the same chat, at once: one second
        clock.now += 5
        await channel.send("42", _message())  # long after: no wait

    run(go())
    assert clock.sleeps == [1.0]


def test_a_short_retry_after_is_waited_once_and_a_long_one_is_left_for_later():
    clock = Clock()
    calls = {"n": 0}

    def busy_once(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429,
                json={"ok": False, "error_code": 429, "parameters": {"retry_after": 3}},
            )
        return _ok(request)

    run(_channel(busy_once, clock).send("42", _message()))
    assert calls["n"] == 2 and clock.sleeps == [3.0]

    def busy_long(request):
        return httpx.Response(
            429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 60}}
        )

    clock = Clock()
    try:
        run(_channel(busy_long, clock).send("42", _message()))
    except MessagingUnavailable as exc:
        assert exc.retry_after == 60
    else:
        raise AssertionError("debía fallar")
    assert clock.sleeps == []
