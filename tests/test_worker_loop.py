"""Worker loop, sync_profile job and configuration. In memory: no database, no Google."""

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta

import pytest

from app.adapters.crypto.sealed import generate_keypair, seal
from app.adapters.google_profile.fake import FakeGoogleProfile
from app.adapters.job_queue.memory import MemoryJobQueue
from app.adapters.worker_store.memory import MemoryWorkerStore
from app.domain.jobs import DONE, FAILED, PENDING, RUNNING, JobError, PermanentJobError
from app.domain.profile_summary import (
    MediaSummary,
    ProfileDetails,
    ProfileHours,
    Review,
    ReviewSet,
)
from app.ports.google_profile import GoogleProfileError, GoogleRejected, ReconnectRequired
from app.ports.worker_store import ConnectedLocation, StoredConnection
from app.worker import log
from app.worker.config import ConfigError, WorkerSettings
from app.worker.loop import Worker
from app.worker.registry import HandlerRegistry
from app.worker.sync_profile import KIND, SyncProfile

PUBLIC, PRIVATE = generate_keypair()
REFRESH = "1//refresh-token-muy-secreto"
ACCESS = "ya29.access-token-muy-secreto"
SUB = "pytest-sub-1"
LOCATION = "locations/222"
NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make_worker(handler, *, kind="k", queue=None, clock=None, job_timeout=5.0, worker_id="w1"):
    clock = clock or Clock()
    queue = queue or MemoryJobQueue(clock)
    registry = HandlerRegistry()
    registry.register(kind, handler)
    worker = Worker(
        queue, registry, worker_id=worker_id, poll_interval=0.01, job_timeout=job_timeout
    )
    return worker, queue, clock


def only_job(queue: MemoryJobQueue):
    (job,) = queue.jobs.values()
    return job


# --- queue rules (memory double; the PostgreSQL one has its own tests) ---------


def test_enqueue_with_the_same_idempotency_key_does_not_duplicate():
    async def scenario():
        queue = MemoryJobQueue()
        assert await queue.enqueue("k", {"n": 1}, idempotency_key="clave") is True
        assert await queue.enqueue("k", {"n": 2}, idempotency_key="clave") is False
        assert await queue.enqueue("k", {}) is True  # no key: always a new job
        assert await queue.enqueue("k", {}) is True
        return queue

    assert len(run(scenario()).jobs) == 3


def test_a_claimed_job_is_not_given_to_anyone_else():
    async def scenario():
        queue = MemoryJobQueue()
        await queue.enqueue("k", {})
        first = await queue.claim("w1", ["k"], 300)
        second = await queue.claim("w2", ["k"], 300)
        return first, second

    first, second = run(scenario())
    assert first is not None and first.attempts == 1
    assert second is None


def test_only_known_kinds_are_claimed():
    async def scenario():
        queue = MemoryJobQueue()
        await queue.enqueue("otro", {})
        return await queue.claim("w1", ["k"], 300), await queue.claim("w1", [], 300)

    assert run(scenario()) == (None, None)


# --- loop -----------------------------------------------------------------------


def test_a_successful_job_ends_done():
    seen = []

    async def handler(job):
        seen.append(job.payload)

    async def scenario():
        worker, queue, _ = make_worker(handler)
        await queue.enqueue("k", {"n": 1})
        assert await worker.run_once() is True
        assert await worker.run_once() is False  # nothing left
        return queue

    job = only_job(run(scenario()))
    assert seen == [{"n": 1}]
    assert (job.state, job.attempts, job.locked_by, job.last_error) == (DONE, 1, None, None)
    assert job.finished_at is not None


def test_a_temporary_failure_is_retried_with_growing_waits_until_it_works():
    calls = []

    async def handler(job):
        calls.append(job.attempts)
        if job.attempts < 3:
            raise JobError("Google no devolvió datos (estado 503)")

    async def scenario():
        worker, queue, clock = make_worker(handler)
        await queue.enqueue("k", {})
        waits = []
        await worker.run_once()
        job = only_job(queue)
        assert job.state == PENDING and job.last_error == "Google no devolvió datos (estado 503)"
        waits.append((job.run_at - clock.now).total_seconds())
        assert await worker.run_once() is False  # not due yet
        clock.advance(waits[-1])
        await worker.run_once()
        waits.append((job.run_at - clock.now).total_seconds())
        clock.advance(waits[-1])
        await worker.run_once()
        return queue, waits

    queue, waits = run(scenario())
    job = only_job(queue)
    assert calls == [1, 2, 3]
    assert waits == [30, 60]
    assert job.state == DONE


def test_a_job_that_keeps_failing_ends_failed_with_its_error():
    exhausted = []

    class Handler:
        async def __call__(self, job):
            raise JobError(f"fallo {job.attempts}")

        async def on_exhausted(self, job):
            exhausted.append(job.attempts)

    async def scenario():
        worker, queue, clock = make_worker(Handler())
        await queue.enqueue("k", {}, max_attempts=3)
        for _ in range(3):
            await worker.run_once()
            clock.advance(3600)
        assert await worker.run_once() is False
        return queue

    job = only_job(run(scenario()))
    assert (job.state, job.attempts, job.last_error) == (FAILED, 3, "fallo 3")
    assert job.finished_at is not None
    assert exhausted == [3]  # told once, on the last attempt


def test_a_permanent_error_is_not_retried():
    exhausted = []

    class Handler:
        async def __call__(self, job):
            raise PermanentJobError("no tiene arreglo")

        async def on_exhausted(self, job):
            exhausted.append(job)

    async def scenario():
        worker, queue, clock = make_worker(Handler())
        await queue.enqueue("k", {})
        await worker.run_once()
        clock.advance(86400)
        assert await worker.run_once() is False
        return queue

    job = only_job(run(scenario()))
    assert (job.state, job.attempts, job.last_error) == (FAILED, 1, "no tiene arreglo")
    assert exhausted == []  # the handler dealt with it before raising


def test_an_unexpected_exception_stores_its_type_but_never_its_message():
    async def handler(job):
        raise RuntimeError(f"algo con {REFRESH} dentro")

    async def scenario():
        worker, queue, _ = make_worker(handler)
        await queue.enqueue("k", {})
        await worker.run_once()
        return queue

    job = only_job(run(scenario()))
    assert job.state == PENDING
    assert "RuntimeError" in job.last_error
    assert REFRESH not in job.last_error


def test_a_job_that_hangs_is_cut_off_and_retried():
    async def handler(job):
        await asyncio.sleep(60)

    async def scenario():
        worker, queue, _ = make_worker(handler, job_timeout=0.05)
        await queue.enqueue("k", {})
        await worker.run_once()
        return queue

    job = only_job(run(scenario()))
    assert job.state == PENDING and "tiempo máximo" in job.last_error


def test_a_job_whose_worker_died_is_recovered_by_another_worker():
    seen = []

    async def handler(job):
        seen.append(job.attempts)

    async def scenario():
        clock = Clock()
        queue = MemoryJobQueue(clock)
        await queue.enqueue("k", {})
        dead = await queue.claim("muerto", ["k"], 300)  # takes it and never comes back
        worker, _, _ = make_worker(handler, queue=queue, clock=clock, worker_id="vivo")
        assert await worker.run_once() is False  # the lock is still fresh
        clock.advance(301)
        assert await worker.run_once() is True
        # The dead worker wakes up late: it no longer holds the job.
        assert await queue.complete(dead, "muerto") is False
        assert await queue.fail(dead, "muerto", "tarde", None) is False
        return queue

    job = only_job(run(scenario()))
    assert seen == [2]
    assert job.state == DONE


def test_a_job_abandoned_on_its_last_attempt_ends_failed():
    async def scenario():
        clock = Clock()
        queue = MemoryJobQueue(clock)
        await queue.enqueue("k", {}, max_attempts=1)
        await queue.claim("muerto", ["k"], 300)
        clock.advance(301)
        assert await queue.claim("vivo", ["k"], 300) is None
        return queue

    job = only_job(run(scenario()))
    assert job.state == FAILED and "dejó de responder" in job.last_error


def test_stop_lets_the_job_in_progress_finish_and_then_leaves():
    async def scenario():
        started = asyncio.Event()
        stop = asyncio.Event()

        async def handler(job):
            started.set()
            await asyncio.sleep(0.05)

        worker, queue, _ = make_worker(handler)
        await queue.enqueue("k", {"n": 1})
        await queue.enqueue("k", {"n": 2})
        task = asyncio.create_task(worker.run(stop))
        await started.wait()
        stop.set()  # what SIGINT / SIGTERM do
        await asyncio.wait_for(task, timeout=2)
        return queue

    states = sorted(j.state for j in run(scenario()).jobs.values())
    assert states == [DONE, PENDING]  # the first one finished; the second was never started
    assert RUNNING not in states


def test_the_loop_survives_a_queue_outage_without_logging_the_connection_string(caplog):
    caplog.set_level(logging.DEBUG)

    class BrokenQueue(MemoryJobQueue):
        failures = 2

        async def claim(self, *args):
            if self.failures:
                self.failures -= 1
                raise ConnectionError("postgresql://usuario:clave@host/db")
            return await super().claim(*args)

    async def scenario():
        done = asyncio.Event()
        stop = asyncio.Event()

        async def handler(job):
            done.set()

        worker, queue, _ = make_worker(handler, queue=BrokenQueue())
        await queue.enqueue("k", {})
        task = asyncio.create_task(worker.run(stop))
        await asyncio.wait_for(done.wait(), timeout=2)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    run(scenario())
    fields = [r.__dict__.get("fields", {}) for r in caplog.records]
    assert {"error": "ConnectionError"} in fields
    assert "clave" not in caplog.text + json.dumps(fields)


# --- sync_profile ---------------------------------------------------------------


def connection(*, token=REFRESH, locations=(("accounts/111", LOCATION),)) -> StoredConnection:
    return StoredConnection(
        refresh_token_enc=seal(PUBLIC, token),
        key_version=1,
        locations=tuple(ConnectedLocation(a, loc) for a, loc in locations),
    )


def reviews_fixture() -> ReviewSet:
    def at(day):
        return datetime(2026, 10, day, tzinfo=UTC)

    return ReviewSet(
        reviews=(
            Review(5, at(1), replied=True),
            Review(1, at(20), replied=False, text="Mal servicio"),
            Review(4, at(5), replied=False),
        ),
        total=3,
        average=3.3,
    )


def make_sync(google: FakeGoogleProfile | None = None, *, store=None, max_attempts=3):
    store = store or MemoryWorkerStore()
    if SUB not in store.connections:
        store.add_connection(SUB, connection())
    google = google or FakeGoogleProfile(
        reviews=reviews_fixture(),
        hours=ProfileHours(True, (date(2026, 12, 25), date(2026, 1, 1))),
        changed_fields=("specialHours",),
        access_token=ACCESS,
    )
    clock = Clock()
    handler = SyncProfile(store, google, PRIVATE, clock)
    worker, queue, _ = make_worker(handler, kind=KIND, clock=clock)

    async def go(payload=None, rounds=1):
        if payload is None:
            payload = {"google_sub": SUB, "location": LOCATION}
        await queue.enqueue(KIND, payload, max_attempts=max_attempts)
        for _ in range(rounds):
            await worker.run_once()
            clock.advance(3600)

    return go, queue, store, google


def test_sync_profile_decrypts_the_token_reads_google_and_stores_the_summary():
    go, queue, store, google = make_sync()
    run(go())
    assert only_job(queue).state == DONE
    assert google.refreshed_with == [REFRESH]  # decrypted with the private key
    assert [what for what, _ in google.reads] == [
        "reviews",
        "details",
        "google_updated",
        "attributes",
        "media",
    ]
    stored = store.summaries[(SUB, LOCATION)]
    assert stored.status == "ok" and stored.synced_at == NOW
    s = stored.summary
    assert (s.review_count, s.average_rating) == (3, 3.3)
    assert (s.unanswered_count, s.unanswered_low_count) == (2, 1)
    assert s.last_review_at == datetime(2026, 10, 20, tzinfo=UTC)
    assert s.has_regular_hours is True
    assert s.upcoming_special_hours == 1
    assert (s.google_changed, s.google_changed_fields) == (True, ("specialHours",))


def test_sync_profile_stores_the_facts_the_diagnosis_needs():
    details = ProfileDetails(
        hours=ProfileHours(True, (date(2026, 12, 25), date(2026, 12, 8), date(2026, 1, 1))),
        title="Bar La Parra",
        address="Calle Elvira 1, 18010 Granada",
        category="Bar",
        additional_categories=2,
        description_length=120,
        has_phone=True,
        has_website=False,
        service_count=4,
        is_open=True,
        place_id="ChIJ-ficha",
        maps_url="https://maps.google.com/?cid=42",
    )
    photo = datetime(2026, 6, 15, 9, 30, tzinfo=UTC)
    google = FakeGoogleProfile(
        reviews=reviews_fixture(), details=details, media=MediaSummary(7, photo), attributes=5
    )
    go, _, store, _ = make_sync(google)
    run(go())
    facts = store.summaries[(SUB, LOCATION)].summary.facts
    assert (facts.title, facts.address, facts.category) == (
        "Bar La Parra",
        "Calle Elvira 1, 18010 Granada",
        "Bar",
    )
    assert facts.additional_categories == 2 and facts.description_length == 120
    assert (facts.has_phone, facts.has_website) == (True, False)
    assert facts.service_count == 4 and facts.is_open is True
    assert (facts.place_id, facts.maps_url) == ("ChIJ-ficha", "https://maps.google.com/?cid=42")
    assert facts.special_dates == (date(2026, 12, 8), date(2026, 12, 25))  # past ones dropped
    assert (facts.photo_count, facts.latest_photo_at, facts.attribute_count) == (7, photo, 5)


def test_attributes_and_media_that_google_refuses_do_not_fail_the_sync(caplog):
    caplog.set_level(logging.DEBUG)
    google = FakeGoogleProfile(
        reviews=reviews_fixture(), access_token=ACCESS, optional_error=GoogleRejected("estado 403")
    )
    go, queue, store, _ = make_sync(google)
    run(go())
    assert only_job(queue).state == DONE
    stored = store.summaries[(SUB, LOCATION)]
    assert stored.status == "ok" and stored.summary.review_count == 3
    facts = stored.summary.facts
    assert (facts.photo_count, facts.latest_photo_at, facts.attribute_count) == (None, None, None)
    assert facts.description_length > 0  # the rest of the profile was read
    rejected = [r for r in caplog.records if r.getMessage() == "optional_read_rejected"]
    assert [r.fields["read"] for r in rejected] == ["attributes", "media"]
    assert ACCESS not in caplog.text


def test_a_lost_grant_or_a_temporary_failure_in_attributes_or_media_is_not_swallowed():
    google = FakeGoogleProfile(reviews=reviews_fixture(), optional_error=ReconnectRequired("x"))
    go, queue, store, _ = make_sync(google)
    run(go())
    assert only_job(queue).state == FAILED
    assert store.summaries[(SUB, LOCATION)].status == "needs_reconnect"

    google = FakeGoogleProfile(
        reviews=reviews_fixture(), optional_error=GoogleProfileError("estado 503")
    )
    go, queue, store, _ = make_sync(google)
    run(go())
    assert only_job(queue).state == PENDING  # retried, nothing shown yet
    assert (SUB, LOCATION) not in store.summaries


def test_a_failed_sync_keeps_the_facts_of_the_last_good_one():
    go, _, store, google = make_sync()
    run(go())
    facts = store.summaries[(SUB, LOCATION)].summary.facts
    assert facts is not None
    google.refresh_error = ReconnectRequired("permiso caducado")
    run(go())
    stored = store.summaries[(SUB, LOCATION)]
    assert stored.status == "needs_reconnect" and stored.summary.facts == facts


def test_sync_profile_uses_the_most_recent_connection():
    store = MemoryWorkerStore()
    store.add_connection(SUB, connection(token="token-antiguo"))
    store.add_connection(SUB, connection(token=REFRESH))
    go, _, _, google = make_sync(store=store)
    run(go())
    assert google.refreshed_with == [REFRESH]


def test_a_temporary_google_failure_is_retried_and_then_succeeds():
    google = FakeGoogleProfile(reviews=reviews_fixture(), read_failures=1)
    go, queue, store, _ = make_sync(google)
    run(go(rounds=1))
    job = only_job(queue)
    assert job.state == PENDING and job.attempts == 1
    assert (SUB, LOCATION) not in store.summaries  # a passing failure is not shown to the owner

    async def retry():
        worker_handler = SyncProfile(store, google, PRIVATE, lambda: NOW)
        await worker_handler(await queue.claim("w2", [KIND], 300))

    run(retry())
    assert store.summaries[(SUB, LOCATION)].status == "ok"


def test_needs_reconnect_is_not_retried_and_shows_in_the_summary():
    go, queue, store, google = make_sync()
    run(go())  # a first good sync
    google.refresh_error = ReconnectRequired("permiso caducado")
    run(go(rounds=3))
    failed = [j for j in queue.jobs.values() if j.state == FAILED]
    assert len(failed) == 1 and failed[0].attempts == 1  # one try, no retries
    assert len(google.refreshed_with) == 2
    stored = store.summaries[(SUB, LOCATION)]
    assert stored.status == "needs_reconnect"
    assert stored.summary.review_count == 3  # the last good figures are kept
    assert stored.synced_at == NOW and stored.status_at > NOW


def test_needs_reconnect_on_a_profile_never_synced_before():
    go, _, store, _ = make_sync(FakeGoogleProfile(refresh_error=ReconnectRequired("x")))
    run(go())
    stored = store.summaries[(SUB, LOCATION)]
    assert (stored.status, stored.summary, stored.synced_at) == ("needs_reconnect", None, None)


def test_error_status_is_written_only_when_attempts_run_out():
    google = FakeGoogleProfile(read_error=GoogleProfileError("Google no devolvió datos (503)"))
    go, queue, store, _ = make_sync(google, max_attempts=2)
    run(go(rounds=1))
    assert (SUB, LOCATION) not in store.summaries
    run(_drain(queue, store, google))
    assert only_job(queue).state == FAILED
    assert store.summaries[(SUB, LOCATION)].status == "error"


async def _drain(queue, store, google):
    handler = SyncProfile(store, google, PRIVATE, lambda: NOW + timedelta(hours=2))
    registry = HandlerRegistry()
    registry.register(KIND, handler)
    queue._clock = lambda: NOW + timedelta(hours=2)
    await Worker(queue, registry, worker_id="w2").run_once()


def test_a_request_google_rejects_is_not_retried_and_is_an_error():
    go, queue, store, _ = make_sync(FakeGoogleProfile(read_error=GoogleRejected("estado 403")))
    run(go(rounds=2))
    job = only_job(queue)
    assert (job.state, job.attempts) == (FAILED, 1)
    assert store.summaries[(SUB, LOCATION)].status == "error"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"google_sub": SUB},
        {"google_sub": "", "location": LOCATION},
        {"google_sub": SUB, "location": 7},
    ],
)
def test_a_bad_payload_fails_for_good_without_calling_google(payload):
    go, queue, store, google = make_sync()
    run(go(payload))
    job = only_job(queue)
    assert (job.state, job.attempts) == (FAILED, 1)
    assert google.refreshed_with == [] and google.reads == []
    assert store.summaries == {}  # no profile to tell about


def test_no_connection_shows_as_needs_reconnect_without_calling_google():
    go, queue, store, google = make_sync()
    run(go({"google_sub": "pytest-desconocido", "location": LOCATION}))
    job = only_job(queue)
    assert (job.state, job.attempts) == (FAILED, 1)
    assert google.refreshed_with == [] and google.reads == []
    stored = store.summaries[("pytest-desconocido", LOCATION)]
    assert (stored.status, stored.status_at) == ("needs_reconnect", NOW)
    assert (stored.summary, stored.synced_at) == (None, None)


def test_a_profile_that_is_not_of_the_connection_shows_as_error_without_calling_google():
    go, queue, store, google = make_sync()
    run(go({"google_sub": SUB, "location": "locations/999"}))
    job = only_job(queue)
    assert (job.state, job.attempts) == (FAILED, 1)
    assert google.refreshed_with == [] and google.reads == []
    stored = store.summaries[(SUB, "locations/999")]
    assert (stored.status, stored.summary, stored.synced_at) == ("error", None, None)
    assert (SUB, LOCATION) not in store.summaries  # the real profile is not touched


def test_a_profile_dropped_by_a_reconnection_keeps_its_last_figures():
    go, _, store, _ = make_sync()
    run(go())  # a first good sync
    store.add_connection(SUB, connection(locations=(("accounts/111", "locations/333"),)))
    run(go())
    stored = store.summaries[(SUB, LOCATION)]
    assert stored.status == "error"
    assert stored.summary.review_count == 3 and stored.synced_at == NOW
    assert stored.status_at > NOW


# --- a worker that died during the last attempt -------------------------------------


def lost_world(*, max_attempts=1):
    """A sync job taken by a worker that never came back, and a live worker next to it."""
    clock = Clock()
    queue = MemoryJobQueue(clock)
    store = MemoryWorkerStore(queue)
    store.add_connection(SUB, connection())
    google = FakeGoogleProfile(reviews=reviews_fixture(), access_token=ACCESS)
    handler = SyncProfile(store, google, PRIVATE, clock)
    registry = HandlerRegistry()
    registry.register(KIND, handler)
    worker = Worker(queue, registry, worker_id="vivo", recover_interval=0)

    async def abandon():
        await queue.enqueue(
            KIND, {"google_sub": SUB, "location": LOCATION}, max_attempts=max_attempts
        )
        await queue.claim("muerto", [KIND], 300)

    return abandon, worker, queue, store, clock, google


def test_a_sync_abandoned_on_its_last_attempt_leaves_the_summary_in_error():
    abandon, worker, queue, store, clock, google = lost_world()

    async def scenario():
        await abandon()
        assert await worker.run_once() is False  # the lock is still fresh
        assert store.summaries == {}
        clock.advance(301)
        assert await worker.run_once() is False  # nothing to run: the job is over

    run(scenario())
    job = only_job(queue)
    assert job.state == FAILED and "dejó de responder" in job.last_error
    stored = store.summaries[(SUB, LOCATION)]
    assert (stored.status, stored.synced_at) == ("error", None)
    assert stored.status_at == job.finished_at
    assert google.refreshed_with == []  # nobody called Google for it


def test_a_lost_sync_does_not_hide_a_later_good_one_and_is_recorded_once():
    abandon, worker, queue, store, clock, _ = lost_world()

    async def scenario():
        await abandon()
        clock.advance(301)
        await worker.run_once()
        assert store.summaries[(SUB, LOCATION)].status == "error"
        clock.advance(60)
        await queue.enqueue(KIND, {"google_sub": SUB, "location": LOCATION})
        assert await worker.run_once() is True  # a later sync works
        good_at = store.summaries[(SUB, LOCATION)].status_at
        clock.advance(3600)
        await worker.run_once()
        return good_at

    good_at = run(scenario())
    stored = store.summaries[(SUB, LOCATION)]
    assert (stored.status, stored.status_at) == ("ok", good_at)  # the old loss is not replayed


def test_a_sync_abandoned_with_attempts_left_is_retried_not_marked():
    abandon, worker, queue, store, clock, _ = lost_world(max_attempts=2)

    async def scenario():
        await abandon()
        clock.advance(301)
        assert await worker.run_once() is True

    run(scenario())
    assert only_job(queue).state == DONE
    assert store.summaries[(SUB, LOCATION)].status == "ok"


def test_lost_jobs_are_looked_for_once_per_interval_and_a_failing_hook_stops_nothing(caplog):
    caplog.set_level(logging.DEBUG)
    calls, seen = [], []

    class Handler:
        async def __call__(self, job):
            seen.append(job.payload)

        async def on_lost(self, kind):
            calls.append(kind)
            raise RuntimeError(f"base caída para {SUB}")

    async def scenario():
        queue = MemoryJobQueue()
        registry = HandlerRegistry()
        registry.register("k", Handler())
        worker = Worker(queue, registry, worker_id="w1", recover_interval=0.05)
        await queue.enqueue("k", {"n": 1})
        assert await worker.run_once() is True  # the job runs although the hook failed
        assert await worker.run_once() is False
        await asyncio.sleep(0.06)
        await worker.run_once()

    run(scenario())
    assert seen == [{"n": 1}]
    assert calls == ["k", "k"]  # not on the second, immediate, round
    fields = [r.__dict__.get("fields", {}) for r in caplog.records]
    assert {"error": "RuntimeError", "kind": "k"} in fields
    assert SUB not in caplog.text + json.dumps(fields)


def test_a_token_that_cannot_be_decrypted_fails_for_good():
    _, other_private = generate_keypair()
    store = MemoryWorkerStore()
    store.add_connection(SUB, connection())
    google = FakeGoogleProfile()
    handler = SyncProfile(store, google, other_private, lambda: NOW)
    worker, queue, _ = make_worker(handler, kind=KIND)

    async def scenario():
        await queue.enqueue(KIND, {"google_sub": SUB, "location": LOCATION})
        await worker.run_once()

    run(scenario())
    job = only_job(queue)
    assert job.state == FAILED and "descifrar" in job.last_error
    assert store.connections[SUB][0].refresh_token_enc not in job.last_error
    assert google.refreshed_with == []
    assert store.summaries[(SUB, LOCATION)].status == "error"


# --- the token never leaks --------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        {"refresh_error": GoogleProfileError(f"eco de {REFRESH}")},
        {"refresh_error": RuntimeError(f"inesperado {REFRESH}")},
        {"refresh_error": ReconnectRequired(f"revocado {REFRESH}")},
        {"read_error": GoogleProfileError(f"eco de {ACCESS} y {REFRESH}")},
        {"read_error": GoogleRejected(f"403 para {ACCESS}")},
        {"read_error": KeyError(ACCESS)},
        {},  # success
    ],
)
def test_the_decrypted_token_is_not_in_logs_errors_or_summaries(failure, caplog):
    caplog.set_level(logging.DEBUG)
    google = FakeGoogleProfile(reviews=reviews_fixture(), access_token=ACCESS, **failure)
    go, queue, store, _ = make_sync(google, max_attempts=2)
    run(go(rounds=2))

    job = only_job(queue)
    assert google.refreshed_with  # the token really was decrypted and used
    logs = caplog.text + json.dumps([r.__dict__.get("fields", {}) for r in caplog.records])
    stored = json.dumps([asdict(s) for s in store.summaries.values()], default=str)
    for secret in (REFRESH, ACCESS, PRIVATE):
        assert secret not in (job.last_error or "")
        assert secret not in logs
        assert secret not in stored
        assert secret not in json.dumps(job.payload)
    if failure:
        assert job.last_error  # there is still something useful to read


def test_log_lines_are_json_with_only_the_given_fields():
    record = logging.LogRecord("fichaitor.worker", logging.INFO, "", 0, "job_done", None, None)
    record.fields = {"job_id": "j1", "kind": "sync_profile", "attempt": 1}
    line = json.loads(log.JsonFormatter().format(record))
    assert line["event"] == "job_done" and line["level"] == "info"
    assert set(line) == {"ts", "level", "event", "job_id", "kind", "attempt"}


def test_job_logs_carry_no_payload(caplog):
    caplog.set_level(logging.DEBUG)
    go, _, _, _ = make_sync()
    run(go())
    fields = [r.__dict__.get("fields", {}) for r in caplog.records]
    assert any(f.get("kind") == KIND for f in fields)
    assert SUB not in caplog.text + json.dumps(fields)


# --- configuration ----------------------------------------------------------------


def settings(**overrides) -> WorkerSettings:
    values = {
        "worker_database_url": "postgresql://u:p@localhost/db",
        "token_private_key": PRIVATE,
        "google_client_id": "id",
        "google_client_secret": "secreto",
    }
    values.update(overrides)
    return WorkerSettings(_env_file=None, **values)


def test_settings_come_from_fichaitor_environment_variables(monkeypatch):
    for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(f"FICHAITOR_{name}", raising=False)
    monkeypatch.setenv("FICHAITOR_WORKER_DATABASE_URL", "postgresql://w@h/db")
    monkeypatch.setenv("FICHAITOR_TOKEN_PRIVATE_KEY", PRIVATE)
    monkeypatch.setenv("FICHAITOR_FAKE_GOOGLE", "true")
    monkeypatch.setenv("FICHAITOR_WORKER_POLL_INTERVAL_SECONDS", "0.5")
    loaded = WorkerSettings(_env_file=None)
    loaded.check()  # with the fake, Google credentials are not needed
    assert loaded.worker_database_url == "postgresql://w@h/db"
    assert loaded.fake_google is True
    assert loaded.worker_poll_interval_seconds == 0.5
    assert loaded.worker_id


def test_valid_settings_pass():
    settings().check()


@pytest.mark.parametrize(
    ("overrides", "missing"),
    [
        ({"worker_database_url": ""}, "FICHAITOR_WORKER_DATABASE_URL"),
        ({"token_private_key": ""}, "FICHAITOR_TOKEN_PRIVATE_KEY"),
        ({"token_private_key": "no-es-una-clave"}, "FICHAITOR_TOKEN_PRIVATE_KEY"),
        ({"google_client_id": ""}, "FICHAITOR_GOOGLE_CLIENT_ID"),
        ({"google_client_secret": ""}, "FICHAITOR_GOOGLE_CLIENT_SECRET"),
        (
            {"worker_job_timeout_seconds": 300, "worker_lock_timeout_seconds": 300},
            "FICHAITOR_WORKER_JOB_TIMEOUT_SECONDS",
        ),
    ],
)
def test_bad_settings_stop_the_worker_without_printing_values(overrides, missing):
    with pytest.raises(ConfigError) as raised:
        settings(**overrides).check()
    assert missing in str(raised.value)
    assert PRIVATE not in str(raised.value)
    assert "no-es-una-clave" not in str(raised.value)
