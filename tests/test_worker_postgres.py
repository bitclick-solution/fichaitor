"""Worker against a real PostgreSQL: queue, roles and the sync_profile job end to end.

Skipped unless the three URLs are set (apply db/schema.sql and db/schema_worker.sql first,
and create the development users of db/dev-init.sql):

    docker compose up -d db
    FICHAITOR_TEST_OWNER_URL=postgresql://fichaitor:dev@127.0.0.1:5433/fichaitor \
    FICHAITOR_TEST_DATABASE_URL=postgresql://fichaitor_api_dev:dev@127.0.0.1:5433/fichaitor \
    FICHAITOR_TEST_WORKER_URL=postgresql://fichaitor_worker_dev:dev@127.0.0.1:5433/fichaitor \
    uv run pytest -q tests/test_worker_postgres.py

Everything these tests create is marked with the prefix "pytest-" and removed afterwards.
Google is the fake adapter: no real call is made.
"""

import asyncio
import json
import os
import uuid
from datetime import UTC, date, datetime, timedelta

import asyncpg
import pytest

from app.adapters.crypto.sealed import generate_keypair, seal
from app.adapters.google_profile.fake import FakeGoogleProfile
from app.adapters.job_queue.postgres import PostgresJobQueue
from app.adapters.worker_store.postgres import PostgresWorkerStore
from app.domain.profile_summary import ProfileHours, Review, ReviewSet
from app.ports.google_profile import GoogleProfileError, ReconnectRequired
from app.worker.loop import Worker
from app.worker.registry import HandlerRegistry
from app.worker.sync_profile import SyncProfile

OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")
API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
WORKER_URL = os.environ.get("FICHAITOR_TEST_WORKER_URL")

pytestmark = pytest.mark.skipif(
    not (OWNER_URL and API_URL and WORKER_URL), reason="sin base de datos de pruebas configurada"
)

PUBLIC, PRIVATE = generate_keypair()
REFRESH = "1//pytest-refresh-token-secreto"
ACCESS = "ya29.pytest-access-token-secreto"
LOCATION = "locations/pytest-222"
LOCK_TIMEOUT = 300.0


def run(coro):
    return asyncio.run(coro)


async def owner(sql: str, *args):
    conn = await asyncpg.connect(OWNER_URL)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def cleanup():
    await owner("DELETE FROM job WHERE kind LIKE 'pytest-%' OR idempotency_key LIKE 'pytest-%'")
    await owner("DELETE FROM profile_summary WHERE google_sub LIKE 'pytest-%'")
    await owner("DELETE FROM review WHERE google_sub LIKE 'pytest-%'")  # a sync stores them
    await owner("DELETE FROM google_connection WHERE google_sub LIKE 'pytest-%'")


@pytest.fixture(autouse=True)
def clean():
    run(cleanup())
    yield
    run(cleanup())


@pytest.fixture
def kind() -> str:
    # A kind of its own per test: the workers of a test only take that test's jobs,
    # never a real job that happens to be in the same database.
    return f"pytest-{uuid.uuid4().hex}"


@pytest.fixture
def sub() -> str:
    return f"pytest-{uuid.uuid4().hex}"


async def job_rows(kind: str):
    return await owner(
        "SELECT id::text, state, attempts, max_attempts, locked_by, locked_at, last_error,"
        " run_at, finished_at, payload::text AS payload, now() AS now"
        " FROM job WHERE kind = $1 ORDER BY created_at",
        kind,
    )


async def add_connection(
    sub: str, *, token: str = REFRESH, age_minutes: int = 0, location: str = LOCATION
) -> None:
    await owner(
        """
        INSERT INTO google_connection
            (google_sub, email, refresh_token_enc, key_version, scopes, locations, connected_at)
        VALUES ($1, 'pytest@example.com', $2, 1, '{}', $3::jsonb,
                now() - make_interval(mins => $4))
        """,
        sub,
        seal(PUBLIC, token),
        json.dumps(
            [
                {
                    "account": "accounts/pytest-111",
                    "location": location,
                    "title": "Bar de pruebas",
                    "place_id": "p-test",
                    "verified": True,
                }
            ]
        ),
        age_minutes,
    )


# --- queue ----------------------------------------------------------------------


def test_two_workers_never_run_the_same_job(kind):
    jobs, workers = 40, 8

    async def scenario():
        pool = await asyncpg.create_pool(WORKER_URL, min_size=workers, max_size=workers)
        try:
            queue = PostgresJobQueue(pool)
            for n in range(jobs):
                await queue.enqueue(kind, {"n": n})

            running: set[str] = set()
            overlaps: list[str] = []
            executions: list[str] = []

            async def worker(name: str) -> None:
                while True:
                    job = await queue.claim(name, [kind], LOCK_TIMEOUT)
                    if job is None:
                        return
                    if job.id in running:
                        overlaps.append(job.id)
                    running.add(job.id)
                    executions.append(job.id)
                    await asyncio.sleep(0.005)  # give the others a chance to grab it
                    running.discard(job.id)
                    assert await queue.complete(job, name) is True

            await asyncio.gather(*(worker(f"pytest-w{n}") for n in range(workers)))
            return overlaps, executions
        finally:
            await pool.close()

    overlaps, executions = run(scenario())
    assert overlaps == []
    assert len(executions) == jobs
    assert len(set(executions)) == jobs  # each job ran exactly once
    rows = run(job_rows(kind))
    assert {r["state"] for r in rows} == {"done"}
    assert {r["attempts"] for r in rows} == {1}


def test_two_simultaneous_claims_of_one_job_give_it_to_one_worker_only(kind):
    async def scenario():
        pool = await asyncpg.create_pool(WORKER_URL, min_size=6, max_size=6)
        try:
            queue = PostgresJobQueue(pool)
            winners = []
            for _ in range(10):
                await queue.enqueue(kind, {})
                claimed = await asyncio.gather(
                    *(queue.claim(f"pytest-w{n}", [kind], LOCK_TIMEOUT) for n in range(6))
                )
                winners.append([job for job in claimed if job is not None])
            return winners
        finally:
            await pool.close()

    for winners in run(scenario()):
        assert len(winners) == 1


def test_the_same_idempotency_key_is_enqueued_once(kind):
    key = f"pytest-{uuid.uuid4().hex}"

    async def scenario():
        worker_pool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=4)
        api_pool = await asyncpg.create_pool(API_URL, min_size=1, max_size=4)
        try:
            api_queue = PostgresJobQueue(api_pool)  # the API enqueues with INSERT only
            first = await api_queue.enqueue(kind, {"n": 1}, idempotency_key=key)
            second = await api_queue.enqueue(kind, {"n": 2}, idempotency_key=key)
            third = await PostgresJobQueue(worker_pool).enqueue(kind, {"n": 3}, idempotency_key=key)
            racing = await asyncio.gather(
                *(api_queue.enqueue(kind, {"n": n}, idempotency_key=key + "-b") for n in range(8))
            )
            no_key = [await api_queue.enqueue(kind, {}) for _ in range(2)]
            return first, second, third, racing, no_key
        finally:
            await worker_pool.close()
            await api_pool.close()

    first, second, third, racing, no_key = run(scenario())
    assert (first, second, third) == (True, False, False)
    assert sorted(racing) == [False] * 7 + [True]
    assert no_key == [True, True]
    rows = run(job_rows(kind))
    assert len(rows) == 4
    assert json.loads(rows[0]["payload"]) == {"n": 1}  # the first one stands
    assert {r["state"] for r in rows} == {"pending"}


def test_retry_waits_longer_each_time_and_ends_failed_at_max_attempts(kind):
    async def scenario():
        pool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=2)
        try:
            queue = PostgresJobQueue(pool)
            await queue.enqueue(kind, {}, max_attempts=2)

            job = await queue.claim("pytest-w1", [kind], LOCK_TIMEOUT)
            assert await queue.fail(job, "pytest-w1", "fallo temporal", 120) is True
            (row,) = await job_rows(kind)
            assert (row["state"], row["attempts"], row["last_error"]) == (
                "pending",
                1,
                "fallo temporal",
            )
            assert row["locked_by"] is None and row["finished_at"] is None
            assert timedelta(seconds=110) < row["run_at"] - row["now"] <= timedelta(seconds=120)
            assert await queue.claim("pytest-w1", [kind], LOCK_TIMEOUT) is None  # not due yet

            await owner("UPDATE job SET run_at = now() WHERE kind = $1", kind)
            job = await queue.claim("pytest-w1", [kind], LOCK_TIMEOUT)
            assert job.attempts == 2 and job.is_last_attempt
            assert await queue.fail(job, "pytest-w1", "x" * 5000, None) is True
            assert await queue.claim("pytest-w1", [kind], LOCK_TIMEOUT) is None
        finally:
            await pool.close()

    run(scenario())
    (row,) = run(job_rows(kind))
    assert (row["state"], row["attempts"]) == ("failed", 2)
    assert row["finished_at"] is not None
    assert 0 < len(row["last_error"]) <= 500


def test_a_job_scheduled_for_later_is_not_taken_before_its_time(kind):
    async def scenario():
        pool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=2)
        try:
            queue = PostgresJobQueue(pool)
            await queue.enqueue(kind, {"n": "later"}, run_at=datetime.now(UTC) + timedelta(hours=1))
            await queue.enqueue(kind, {"n": "now"})
            first = await queue.claim("pytest-w1", [kind], LOCK_TIMEOUT)
            second = await queue.claim("pytest-w1", [kind], LOCK_TIMEOUT)
            return first, second
        finally:
            await pool.close()

    first, second = run(scenario())
    assert first.payload == {"n": "now"}
    assert second is None


def test_a_running_job_whose_worker_died_is_recovered(kind):
    async def scenario():
        pool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=2)
        try:
            queue = PostgresJobQueue(pool)
            await queue.enqueue(kind, {})
            dead = await queue.claim("pytest-muerto", [kind], LOCK_TIMEOUT)
            assert await queue.claim("pytest-vivo", [kind], LOCK_TIMEOUT) is None  # lock is fresh

            await owner(
                "UPDATE job SET locked_at = now() - interval '10 minutes' WHERE kind = $1", kind
            )
            again = await queue.claim("pytest-vivo", [kind], LOCK_TIMEOUT)
            assert again is not None and again.id == dead.id and again.attempts == 2

            # The worker presumed dead wakes up: it cannot close a job it no longer holds.
            assert await queue.complete(dead, "pytest-muerto") is False
            assert await queue.fail(dead, "pytest-muerto", "tarde", None) is False
            assert await queue.complete(again, "pytest-vivo") is True
        finally:
            await pool.close()

    run(scenario())
    (row,) = run(job_rows(kind))
    assert (row["state"], row["attempts"], row["locked_by"]) == ("done", 2, None)


def test_a_job_abandoned_on_its_last_attempt_ends_failed(kind):
    async def scenario():
        pool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=2)
        try:
            queue = PostgresJobQueue(pool)
            await queue.enqueue(kind, {}, max_attempts=1)
            await queue.claim("pytest-muerto", [kind], LOCK_TIMEOUT)
            await owner(
                "UPDATE job SET locked_at = now() - interval '10 minutes' WHERE kind = $1", kind
            )
            assert await queue.claim("pytest-vivo", [kind], LOCK_TIMEOUT) is None
        finally:
            await pool.close()

    run(scenario())
    (row,) = run(job_rows(kind))
    assert row["state"] == "failed" and "dejó de responder" in row["last_error"]
    assert row["finished_at"] is not None and row["locked_by"] is None


# --- roles ----------------------------------------------------------------------


async def denied(url: str, sql: str, *args) -> bool:
    conn = await asyncpg.connect(url)
    try:
        await conn.execute(sql, *args)
    except asyncpg.InsufficientPrivilegeError:
        return True
    finally:
        await conn.close()
    return False


def test_the_api_role_can_enqueue_and_read_summaries_and_nothing_else(kind, sub):
    async def scenario():
        await owner("INSERT INTO job (kind, payload) VALUES ($1, '{\"secreto\": 1}')", kind)
        await owner(
            "INSERT INTO profile_summary (google_sub, location, status) VALUES ($1, $2, 'ok')",
            sub,
            LOCATION,
        )
        insert_summary = (
            "INSERT INTO profile_summary (google_sub, location, status)"
            f" VALUES ('{sub}', 'locations/pytest-otra', 'ok')"
        )
        checks = {
            "read payload": "SELECT payload FROM job",
            "read ids": "SELECT id FROM job",
            "count jobs": "SELECT count(*) FROM job",
            "update job": f"UPDATE job SET state = 'done' WHERE kind = '{kind}'",
            "delete job": f"DELETE FROM job WHERE kind = '{kind}'",
            "forge state": f"INSERT INTO job (kind, state) VALUES ('{kind}', 'done')",
            "forge attempts": f"INSERT INTO job (kind, attempts) VALUES ('{kind}', 99)",
            "forge lock": f"INSERT INTO job (kind, locked_by) VALUES ('{kind}', 'yo')",
            "returning": f"INSERT INTO job (kind) VALUES ('{kind}') RETURNING payload",
            "insert summary": insert_summary,
            "update summary": "UPDATE profile_summary SET status = 'error'",
            "delete summary": "DELETE FROM profile_summary",
        }
        results = {name: await denied(API_URL, sql) for name, sql in checks.items()}

        conn = await asyncpg.connect(API_URL)
        try:
            status = await conn.fetchval(
                "SELECT status FROM profile_summary WHERE google_sub = $1", sub
            )
        finally:
            await conn.close()
        return results, status

    results, status = run(scenario())
    assert results == dict.fromkeys(results, True)
    assert status == "ok"
    assert len(run(job_rows(kind))) == 1  # none of the forged inserts got in


def test_the_worker_role_reads_connections_but_no_contacts(kind, sub):
    async def scenario():
        await add_connection(sub)
        checks = {
            "waitlist": "SELECT * FROM waitlist_entry",
            "waitlist count": "SELECT count(*) FROM waitlist_entry",
            "waitlist insert": "INSERT INTO waitlist_entry (channel, contact, consent_version)"
            " VALUES ('email', 'pytest@example.com', 'x')",
            "funnel": "SELECT * FROM funnel_event",
            "connection email": "SELECT email FROM google_connection",
            "connection all": "SELECT * FROM google_connection",
            "connection insert": "INSERT INTO google_connection"
            " (google_sub, email, refresh_token_enc, key_version, scopes)"
            " VALUES ('pytest-x', '', '', 1, '{}')",
            "connection update": "UPDATE google_connection SET refresh_token_enc = ''",
            "connection delete": "DELETE FROM google_connection",
            "job delete": f"DELETE FROM job WHERE kind = '{kind}'",
            "summary delete": "DELETE FROM profile_summary",
        }
        results = {name: await denied(WORKER_URL, sql) for name, sql in checks.items()}
        pool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=1)
        try:
            found = await PostgresWorkerStore(pool).latest_connection(sub)
        finally:
            await pool.close()
        return results, found

    results, found = run(scenario())
    assert results == dict.fromkeys(results, True)
    assert found is not None and found.locations[0].location == LOCATION


# --- sync_profile, end to end -----------------------------------------------------


def reviews_fixture() -> ReviewSet:
    def at(day):
        return datetime(2026, 10, day, tzinfo=UTC)

    return ReviewSet(
        reviews=(
            Review(5, at(1), replied=True),
            Review(1, at(20), replied=False, text="Mal servicio"),
            Review(2, at(7), replied=False),
            Review(4, at(5), replied=False),
        ),
        total=13,
        average=4.7,
    )


async def summary_row(sub: str):
    rows = await owner(
        "SELECT * FROM profile_summary WHERE google_sub = $1 AND location = $2", sub, LOCATION
    )
    return dict(rows[0]) if rows else None


async def run_sync(
    kind: str, sub: str, google: FakeGoogleProfile, *, rounds=1, max_attempts=3, enqueue=True
):
    """Enqueue one sync job as the API would and let a real worker loop run it.
    Before every round the pending job is made due, so no test waits for a backoff."""
    worker_pool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=2)
    api_pool = await asyncpg.create_pool(API_URL, min_size=1, max_size=1)
    try:
        registry = HandlerRegistry()
        registry.register(kind, SyncProfile(PostgresWorkerStore(worker_pool), google, PRIVATE))
        worker = Worker(PostgresJobQueue(worker_pool), registry, worker_id="pytest-sync")
        if enqueue:
            await PostgresJobQueue(api_pool).enqueue(
                kind, {"google_sub": sub, "location": LOCATION}, max_attempts=max_attempts
            )
        for _ in range(rounds):
            await owner("UPDATE job SET run_at = now() WHERE kind = $1 AND state = 'pending'", kind)
            await worker.run_once()
    finally:
        await worker_pool.close()
        await api_pool.close()


def test_sync_profile_stores_the_summary_and_updates_the_same_row(kind, sub):
    today = datetime.now(UTC).date()
    google = FakeGoogleProfile(
        reviews=reviews_fixture(),
        hours=ProfileHours(True, (today + timedelta(days=30), date(2020, 1, 1))),
        changed_fields=("specialHours", "title"),
        access_token=ACCESS,
    )

    async def scenario():
        await add_connection(sub, token="pytest-token-antiguo", age_minutes=60)
        await add_connection(sub)  # a reconnection: the newest row is the valid one
        await run_sync(kind, sub, google)
        first = await summary_row(sub)
        await run_sync(
            kind, sub, FakeGoogleProfile(reviews=ReviewSet(()), hours=ProfileHours(False))
        )
        second = await summary_row(sub)
        count = await owner("SELECT count(*) AS n FROM profile_summary WHERE google_sub = $1", sub)
        return first, second, count[0]["n"]

    first, second, count = run(scenario())
    assert google.refreshed_with == [REFRESH]  # the newest token, decrypted

    assert first["status"] == "ok"
    assert first["review_count"] == 13
    assert float(first["average_rating"]) == 4.7
    assert (first["unanswered_count"], first["unanswered_low_count"]) == (3, 2)
    assert first["last_review_at"] == datetime(2026, 10, 20, tzinfo=UTC)
    assert first["has_regular_hours"] is True
    assert first["upcoming_special_hours"] == 1
    assert first["google_changed"] is True
    assert first["google_changed_fields"] == ["specialHours", "title"]
    assert first["synced_at"] is not None

    assert count == 1  # one row per profile: the second sync replaced the first
    assert (second["review_count"], second["average_rating"]) == (0, None)
    assert (second["unanswered_count"], second["last_review_at"]) == (0, None)
    assert second["has_regular_hours"] is False
    assert (second["google_changed"], second["google_changed_fields"]) == (False, [])
    assert second["synced_at"] >= first["synced_at"]
    assert {r["state"] for r in run(job_rows(kind))} == {"done"}


def test_a_temporary_failure_is_retried_until_it_works(kind, sub):
    google = FakeGoogleProfile(reviews=reviews_fixture(), read_failures=2)

    async def scenario():
        await add_connection(sub)
        await run_sync(kind, sub, google, rounds=1)
        middle = (await job_rows(kind))[0], await summary_row(sub)
        await run_sync(kind, sub, google, rounds=2, enqueue=False)
        return middle

    (job, summary) = run(scenario())
    assert (job["state"], job["attempts"]) == ("pending", 1)
    assert job["run_at"] > job["now"]  # waiting for its retry
    assert summary is None
    (row,) = run(job_rows(kind))
    assert (row["state"], row["attempts"], row["last_error"]) == ("done", 3, None)
    assert run(summary_row(sub))["status"] == "ok"


def test_needs_reconnect_is_not_retried_and_shows_in_the_summary(kind, sub):
    google = FakeGoogleProfile(reviews=reviews_fixture(), access_token=ACCESS)

    async def scenario():
        await add_connection(sub)
        await run_sync(kind, sub, google)  # a first good sync
        google.refresh_error = ReconnectRequired("permiso revocado")
        await run_sync(kind, sub, google, rounds=3)

    run(scenario())
    rows = run(job_rows(kind))
    assert [(r["state"], r["attempts"]) for r in rows] == [("done", 1), ("failed", 1)]
    assert len(google.refreshed_with) == 2  # the failed job tried exactly once
    summary = run(summary_row(sub))
    assert summary["status"] == "needs_reconnect"
    assert summary["review_count"] == 13  # last good figures kept
    assert summary["status_at"] > summary["synced_at"]


def test_running_out_of_attempts_leaves_the_summary_in_error(kind, sub):
    google = FakeGoogleProfile(read_error=GoogleProfileError("Google no devolvió datos (503)"))

    async def scenario():
        await add_connection(sub)
        await run_sync(kind, sub, google, rounds=3, max_attempts=2)

    run(scenario())
    (row,) = run(job_rows(kind))
    assert (row["state"], row["attempts"]) == ("failed", 2)
    summary = run(summary_row(sub))
    assert summary["status"] == "error" and summary["synced_at"] is None


def test_no_connection_shows_as_needs_reconnect_without_calling_google(kind, sub):
    google = FakeGoogleProfile(reviews=reviews_fixture())

    async def scenario():
        await run_sync(kind, sub, google, rounds=2)  # nobody ever connected as that user

    run(scenario())
    (row,) = run(job_rows(kind))
    assert (row["state"], row["attempts"]) == ("failed", 1)
    assert google.refreshed_with == [] and google.reads == []
    summary = run(summary_row(sub))
    assert summary["status"] == "needs_reconnect"
    assert (summary["synced_at"], summary["review_count"]) == (None, None)


def test_a_profile_that_is_not_of_the_latest_connection_shows_as_error(kind, sub):
    google = FakeGoogleProfile(reviews=reviews_fixture(), access_token=ACCESS)

    async def scenario():
        await add_connection(sub, age_minutes=60)
        await run_sync(kind, sub, google)  # a first good sync
        await add_connection(sub, location="locations/pytest-333")  # reconnected without it
        await run_sync(kind, sub, google, rounds=2)

    run(scenario())
    rows = run(job_rows(kind))
    assert [(r["state"], r["attempts"]) for r in rows] == [("done", 1), ("failed", 1)]
    assert len(google.refreshed_with) == 1  # the second job never reached Google
    summary = run(summary_row(sub))
    assert summary["status"] == "error"
    assert summary["review_count"] == 13  # last good figures kept
    assert summary["status_at"] > summary["synced_at"]


def test_a_sync_abandoned_on_its_last_attempt_leaves_the_summary_in_error(kind, sub):
    google = FakeGoogleProfile(reviews=reviews_fixture(), access_token=ACCESS)

    async def scenario():
        await add_connection(sub)
        pool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=2)
        try:
            queue, store = PostgresJobQueue(pool), PostgresWorkerStore(pool)
            registry = HandlerRegistry()
            registry.register(kind, SyncProfile(store, google, PRIVATE))
            worker = Worker(queue, registry, worker_id="pytest-vivo", recover_interval=0)
            payload = {"google_sub": sub, "location": LOCATION}

            await queue.enqueue(kind, payload, max_attempts=1)
            await queue.claim("pytest-muerto", [kind], LOCK_TIMEOUT)  # and it never comes back
            assert await worker.run_once() is False  # the lock is still fresh
            fresh = await summary_row(sub)
            await owner(
                "UPDATE job SET locked_at = now() - interval '10 minutes' WHERE kind = $1", kind
            )
            assert await worker.run_once() is False  # the job is closed, not run
            lost = await summary_row(sub)
            (job,) = await job_rows(kind)

            assert await store.mark_lost_syncs(kind) == 0  # recorded once

            await queue.enqueue(kind, payload)  # a later sync works
            assert await worker.run_once() is True
            good = await summary_row(sub)
            assert await worker.run_once() is False
            assert await store.mark_lost_syncs(kind) == 0  # the old loss is not replayed
            return fresh, lost, job, good, await summary_row(sub)
        finally:
            await pool.close()

    fresh, lost, job, good, final = run(scenario())
    assert fresh is None
    assert job["state"] == "failed" and "dejó de responder" in job["last_error"]
    assert (lost["status"], lost["synced_at"]) == ("error", None)
    assert lost["status_at"] == job["finished_at"]
    assert good["status"] == "ok"
    assert final == good
    assert google.refreshed_with == [REFRESH]  # only the later job reached Google


def test_lost_jobs_with_a_bad_payload_or_of_another_kind_write_no_summary(kind, sub):
    other_kind = f"pytest-{uuid.uuid4().hex}"

    async def scenario():
        for job_kind, payload in [
            (kind, {}),
            (kind, {"google_sub": sub}),
            (kind, {"google_sub": sub, "location": ""}),
            (kind, {"google_sub": sub, "location": 7}),
            (other_kind, {"google_sub": sub, "location": LOCATION}),
        ]:
            await owner(
                "INSERT INTO job (kind, payload, state, attempts, max_attempts, locked_by,"
                " locked_at) VALUES ($1, $2::jsonb, 'running', 1, 1, 'pytest-muerto',"
                " now() - interval '10 minutes')",
                job_kind,
                json.dumps(payload),
            )
        pool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=2)
        try:
            queue = PostgresJobQueue(pool)
            assert await queue.claim("pytest-vivo", [kind, other_kind], LOCK_TIMEOUT) is None
            return await PostgresWorkerStore(pool).mark_lost_syncs(kind)
        finally:
            await pool.close()

    assert run(scenario()) == 0
    assert {r["state"] for r in run(job_rows(kind))} == {"failed"}
    assert {r["state"] for r in run(job_rows(other_kind))} == {"failed"}
    assert run(owner("SELECT 1 FROM profile_summary WHERE google_sub = $1", sub)) == []


@pytest.mark.parametrize(
    "failure",
    [
        {},
        {"refresh_error": GoogleProfileError(f"eco {REFRESH}")},
        {"refresh_error": ReconnectRequired(f"revocado {REFRESH}")},
        {"read_error": GoogleProfileError(f"eco {ACCESS} {REFRESH}")},
        {"read_error": RuntimeError(f"inesperado {ACCESS}")},
    ],
)
def test_the_decrypted_token_is_stored_nowhere(kind, sub, failure):
    google = FakeGoogleProfile(reviews=reviews_fixture(), access_token=ACCESS, **failure)

    async def scenario():
        await add_connection(sub)
        await run_sync(kind, sub, google, rounds=2, max_attempts=2)
        jobs = await owner("SELECT row_to_json(j)::text AS t FROM job j WHERE kind = $1", kind)
        summaries = await owner(
            "SELECT row_to_json(s)::text AS t FROM profile_summary s WHERE google_sub = $1", sub
        )
        return " ".join(r["t"] for r in [*jobs, *summaries])

    stored = run(scenario())
    assert google.refreshed_with == [REFRESH] * len(google.refreshed_with) != []
    assert stored  # there is something to inspect
    for secret in (REFRESH, ACCESS, PRIVATE):
        assert secret not in stored


def test_sync_profile_stores_the_facts_and_a_failed_sync_keeps_them(kind, sub):
    import json

    from app.domain.profile_summary import MediaSummary, ProfileDetails, facts_from_json

    today = datetime.now(UTC).date()
    photo = datetime(2026, 6, 15, 9, 30, tzinfo=UTC)
    details = ProfileDetails(
        hours=ProfileHours(True, (today + timedelta(days=30), date(2020, 1, 1))),
        title="Bar pytest",
        address="Calle Elvira 1, 18010 Granada",
        category="Bar",
        additional_categories=2,
        description_length=120,
        has_phone=True,
        has_website=False,
        service_count=4,
        place_id="place-pytest-1",
        maps_url="https://maps.google.com/?cid=42",
    )
    google = FakeGoogleProfile(
        reviews=reviews_fixture(),
        details=details,
        media=MediaSummary(7, photo),
        attributes=5,
        access_token=ACCESS,
    )

    async def scenario():
        await add_connection(sub)
        await run_sync(kind, sub, google)
        first = await owner(
            "SELECT facts::text AS facts, jsonb_typeof(facts) AS kind FROM profile_summary"
            " WHERE google_sub = $1",
            sub,
        )
        google.refresh_error = ReconnectRequired("permiso caducado")
        await run_sync(kind, sub, google)
        second = await owner(
            "SELECT status, facts::text AS facts FROM profile_summary WHERE google_sub = $1", sub
        )
        return first[0], second[0]

    first, second = run(scenario())
    assert first["kind"] == "object"
    facts = facts_from_json(json.loads(first["facts"]))
    assert (facts.title, facts.address, facts.category) == (
        "Bar pytest",
        "Calle Elvira 1, 18010 Granada",
        "Bar",
    )
    assert (facts.additional_categories, facts.description_length, facts.service_count) == (
        2,
        120,
        4,
    )
    assert (facts.has_phone, facts.has_website, facts.is_open) == (True, False, True)
    assert (facts.place_id, facts.maps_url) == ("place-pytest-1", "https://maps.google.com/?cid=42")
    assert facts.special_dates == (today + timedelta(days=30),)
    assert (facts.photo_count, facts.latest_photo_at, facts.attribute_count) == (7, photo, 5)
    assert ACCESS not in first["facts"] and REFRESH not in first["facts"]

    assert second["status"] == "needs_reconnect"
    assert json.loads(second["facts"]) == json.loads(first["facts"])
