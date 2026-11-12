"""The owner removes Fichaitor's access in their Google account: `invalid_grant` when the
token is refreshed. In memory, with the fake Google and a mocked HTTP transport: no real
call to Google is ever made. The database side is in test_revocacion_google_postgres.py.
"""

import asyncio
import logging
from datetime import UTC, date, datetime

import httpx
import pytest

from app.adapters.crypto.sealed import generate_keypair, seal
from app.adapters.google_profile.fake import FakeGoogleProfile
from app.adapters.google_profile.real import RealGoogleProfile
from app.adapters.job_queue.memory import MemoryJobQueue
from app.adapters.memory_tables import MemoryTables
from app.adapters.worker_store.memory import MemoryWorkerStore
from app.domain import actions
from app.domain.jobs import FAILED, PENDING
from app.domain.profile_summary import Review, ReviewSet
from app.domain.special_hours import KIND_CLOSED, checked
from app.ports.google_profile import (
    GoogleProfileError,
    GoogleRejected,
    GrantRevoked,
    ReconnectRequired,
)
from app.ports.worker_store import ConnectedLocation, StoredConnection
from app.worker.loop import Worker
from app.worker.registry import HandlerRegistry
from app.worker.reply_review import KIND as REPLY_KIND
from app.worker.reply_review import ReplyReview
from app.worker.set_special_hours import KIND as HOURS_KIND
from app.worker.set_special_hours import SetSpecialHours
from app.worker.sync_profile import KIND as SYNC_KIND
from app.worker.sync_profile import SyncProfile

PUBLIC, PRIVATE = generate_keypair()
REFRESH = "1//refresh-token-secreto"
ACCESS = "ya29.access-token-secreto"
SUB = "pytest-sub-1"
ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"
GBP_ACCOUNT = "accounts/111"
LOCATION = "locations/222"
NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
DAY = date(2026, 12, 8)


def run(coro):
    return asyncio.run(coro)


# --- the adapter: only invalid_grant is "revoked for good" ------------------------


def adapter(status: int, body: dict) -> tuple[RealGoogleProfile, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    return RealGoogleProfile("id", "secret", client=client, retry_delay=0), seen


@pytest.mark.parametrize("description", ["Token has been expired or revoked.", "Bad Request", None])
def test_invalid_grant_on_refresh_is_a_revoked_grant_and_is_not_retried(description):
    body = {"error": "invalid_grant"}
    if description:
        body["error_description"] = description
    google, seen = adapter(400, body)
    with pytest.raises(GrantRevoked) as raised:
        run(google.refresh_access_token(REFRESH))
    assert isinstance(raised.value, ReconnectRequired)  # still "connect again" for the panel
    assert len(seen) == 1 and REFRESH not in str(raised.value)


@pytest.mark.parametrize(("status", "body"), [(401, {"error": "unauthorized"}), (401, {})])
def test_a_401_without_invalid_grant_asks_to_reconnect_but_deletes_nothing(status, body):
    google, _ = adapter(status, body)
    with pytest.raises(ReconnectRequired) as raised:
        run(google.refresh_access_token(REFRESH))
    assert not isinstance(raised.value, GrantRevoked)


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (503, {}, GoogleProfileError),  # passing: tried again
        (429, {"error": "rate_limit_exceeded"}, GoogleProfileError),  # quota: tried again
        (401, {"error": "invalid_client"}, GoogleRejected),  # our credentials, not the owner's
        (400, {"error": "invalid_request"}, GoogleRejected),
    ],
)
def test_passing_failures_quota_and_our_own_errors_are_not_a_revoked_grant(status, body, error):
    google, _ = adapter(status, body)
    with pytest.raises(error) as raised:
        run(google.refresh_access_token(REFRESH))
    assert not isinstance(raised.value, ReconnectRequired)


# --- sync_profile ----------------------------------------------------------------


def stored_connection(token: str = REFRESH, connection_id: str = "c-new") -> StoredConnection:
    return StoredConnection(
        seal(PUBLIC, token),
        1,
        (ConnectedLocation(GBP_ACCOUNT, LOCATION),),
        connection_id=connection_id,
    )


class World:
    def __init__(self) -> None:
        self.now = NOW
        self.queue = MemoryJobQueue(lambda: self.now)
        self.tables = MemoryTables()
        self.store = MemoryWorkerStore(self.queue, self.tables)
        self.store.add_connection(SUB, stored_connection("old-token", "c-old"))
        self.store.add_connection(SUB, stored_connection())
        review = Review(5, NOW, False, "Muy bien.", review_id="g-1")
        self.google = FakeGoogleProfile(reviews=ReviewSet((review,)), access_token=ACCESS)
        registry = HandlerRegistry()
        registry.register(SYNC_KIND, SyncProfile(self.store, self.google, PRIVATE, self.clock))
        registry.register(REPLY_KIND, ReplyReview(self.store, self.google, PRIVATE, self.clock))
        registry.register(HOURS_KIND, SetSpecialHours(self.store, self.google, PRIVATE, self.clock))
        self.worker = Worker(self.queue, registry, worker_id="w1", poll_interval=0.01)

    def clock(self) -> datetime:
        return self.now

    async def sync(self) -> None:
        await self.queue.enqueue(SYNC_KIND, {"google_sub": SUB, "location": LOCATION})
        await self.worker.run_once()

    def jobs(self, kind: str):
        return [j for j in self.queue.jobs.values() if j.kind == kind]


def test_invalid_grant_in_a_sync_marks_the_connection_revoked_and_drops_its_token(caplog):
    caplog.set_level(logging.DEBUG)
    world = World()
    run(world.sync())  # a first good sync
    world.google.refresh_error = GrantRevoked("permiso de Google revocado o caducado (400)")
    world.now = NOW.replace(hour=10)
    run(world.sync())

    failed = world.jobs(SYNC_KIND)[-1]
    assert (failed.state, failed.attempts) == (FAILED, 1)  # never retried
    assert world.store.summaries[(SUB, LOCATION)].status == "needs_reconnect"
    # The connection is gone with its token, and the one it replaced too.
    assert world.store.connections[SUB] == []
    assert world.store.revoked[SUB] == [("c-new", world.now)]
    assert run(world.store.latest_connection(SUB)) is None
    # Nothing is revoked at Google: it already is.
    assert world.google.revoked == []
    events = [r for r in caplog.records if r.getMessage() == "google_grant_revoked"]
    assert len(events) == 1 and events[0].fields["countdown_started"] is True
    assert REFRESH not in caplog.text and "old-token" not in caplog.text


def test_after_a_revoked_grant_the_next_sync_asks_to_reconnect_without_calling_google():
    world = World()
    world.google.refresh_error = GrantRevoked("x")
    run(world.sync())
    world.google.refresh_error = None
    calls = len(world.google.refreshed_with)
    run(world.sync())
    assert len(world.google.refreshed_with) == calls
    assert world.jobs(SYNC_KIND)[-1].state == FAILED
    assert world.store.summaries[(SUB, LOCATION)].status == "needs_reconnect"


def test_a_newer_valid_connection_stops_the_countdown_from_starting(caplog):
    caplog.set_level(logging.DEBUG)
    world = World()
    # The owner connected again while the job ran with the connection now revoked.
    newest = stored_connection("newest", "c-newest")

    async def scenario():
        await world.store.revoke_connection(SUB, "c-old", NOW)
        world.store.add_connection(SUB, newest)
        return await world.store.revoke_connection(SUB, "c-new", NOW)

    orphaned = run(scenario())
    assert orphaned is False
    assert world.store.connections[SUB] == [newest]


@pytest.mark.parametrize(
    "error",
    [
        ReconnectRequired("401"),
        GoogleProfileError("estado 503", 503),
        GoogleProfileError("estado 429", 429),
    ],
)
def test_other_failures_keep_the_connection_and_its_token(error):
    world = World()
    world.google.refresh_error = error
    run(world.sync())
    assert len(world.store.connections[SUB]) == 2
    assert world.store.revoked == {}
    job = world.jobs(SYNC_KIND)[-1]
    assert job.state == (FAILED if isinstance(error, ReconnectRequired) else PENDING)


# --- the executors -------------------------------------------------------------------


def test_invalid_grant_while_replying_marks_the_connection_revoked():
    world = World()
    run(world.sync())
    row = next(iter(world.tables.reviews.values()))
    action = world.tables.create_action(ACCOUNT_ID, row, "Gracias.", NOW)
    world.google.refresh_error = GrantRevoked("x")

    async def go():
        await world.queue.enqueue(REPLY_KIND, {"action_id": action.id})
        await world.worker.run_once()

    run(go())
    assert (action.state, action.error_code) == (actions.FAILED, actions.ERROR_RECONNECT)
    assert world.store.connections[SUB] == [] and SUB in world.store.revoked
    assert world.google.replies == []


def test_invalid_grant_while_setting_special_hours_marks_the_connection_revoked():
    world = World()
    change = checked(DAY, KIND_CLOSED, ())
    action = world.tables.create_special_hours_action(
        ACCOUNT_ID, SUB, LOCATION, DAY, change.to_text(), NOW
    )
    world.google.refresh_error = GrantRevoked("x")

    async def go():
        await world.queue.enqueue(HOURS_KIND, {"action_id": action.id})
        await world.worker.run_once()

    run(go())
    assert (action.state, action.error_code) == (actions.FAILED, actions.ERROR_RECONNECT)
    assert world.store.connections[SUB] == [] and SUB in world.store.revoked
    assert not world.google.special_writes
