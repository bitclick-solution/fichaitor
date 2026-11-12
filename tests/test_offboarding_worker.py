"""Leaving, on the worker's side: the email job, making a leave effective, the daily job
the worker enqueues for itself, and jobs of an account whose leave is scheduled.
In memory: no database, no SMTP and no Google."""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import pytest

from app.adapters.crypto.sealed import generate_keypair, seal
from app.adapters.google_profile.fake import FakeGoogleProfile
from app.adapters.job_queue.memory import MemoryJobQueue
from app.adapters.mailer.fake import FakeMailer
from app.adapters.memory_tables import MemoryTables
from app.adapters.sentiment_model.fake import FakeSentimentModel
from app.adapters.worker_store.memory import MemoryWorkerStore
from app.domain import actions
from app.domain.jobs import DONE, FAILED, PENDING, Job, JobError, PermanentJobError
from app.domain.profile_summary import Review, ReviewSet
from app.domain.sentiment import SentimentPolicy
from app.ports.google_profile import GoogleProfileError, GoogleRejected
from app.ports.mailer import MailerError, MailRejected
from app.ports.worker_store import (
    ConnectedLocation,
    EffectiveLeave,
    StoredConnection,
    TokenToRevoke,
)
from app.worker.analyze_sentiment import AnalyzeSentiment
from app.worker.apply_offboarding import ApplyOffboarding
from app.worker.loop import DailyJob, Worker
from app.worker.registry import HandlerRegistry
from app.worker.reply_review import ReplyReview
from app.worker.retention import Retention
from app.worker.send_mail import SendMail
from app.worker.sync_profile import SyncProfile

PUBLIC, PRIVATE = generate_keypair()
NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
ADDRESS = "dueno@example.com"
CODE = "042137"
TOKEN = "token-de-restauracion-muy-secreto"
LEAVE_ID = "11111111-1111-1111-1111-111111111111"
SUB = "pytest-sub-1"
LOCATION = "locations/222"


def run(coro):
    return asyncio.run(coro)


# --- send_mail -------------------------------------------------------------------


class MailStore:
    def __init__(self) -> None:
        self.scrubbed: list[str] = []
        self.code_mail: dict[str, str] = {}
        self.swept: list[str] = []

    async def scrub_job(self, job_id: str) -> None:
        self.scrubbed.append(job_id)

    async def scrub_finished_jobs(self, kind: str) -> int:
        self.swept.append(kind)
        return 2

    async def set_code_mail(self, code_id: str, state: str) -> None:
        self.code_mail[code_id] = state


def code_job(**changes) -> Job:
    payload = {
        "template": "offboarding_code",
        "to": ADDRESS,
        "scope": "ficha",
        "code": CODE,
        "minutes": 10,
        "code_id": "code-1",
    }
    return Job("job-1", "send_mail", {**payload, **changes})


def notice_job() -> Job:
    payload = {
        "template": "offboarding_scheduled",
        "to": ADDRESS,
        "scope": "cuenta",
        "delete_at": (NOW + timedelta(hours=48)).isoformat(),
        "token": TOKEN,
    }
    return Job("job-2", "send_mail", payload)


def test_the_code_email_is_sent_recorded_and_the_payload_emptied(caplog):
    store, mailer = MailStore(), FakeMailer()
    with caplog.at_level(logging.INFO, logger="fichaitor.worker"):
        run(SendMail(store, mailer, "https://fichaitor.test")(code_job()))
    (mail,) = mailer.sent
    assert mail.to == ADDRESS and CODE in mail.text
    assert store.code_mail == {"code-1": "sent"} and store.scrubbed == ["job-1"]
    worker_log = "".join(
        r.getMessage() + json.dumps(getattr(r, "fields", {}))
        for r in caplog.records
        if r.name == "fichaitor.worker"
    )
    assert CODE not in worker_log and ADDRESS not in worker_log


def test_the_notice_carries_the_restore_link_built_from_the_public_url():
    store, mailer = MailStore(), FakeMailer()
    run(SendMail(store, mailer, "https://fichaitor.test/")(notice_job()))
    (mail,) = mailer.sent
    assert f"https://fichaitor.test/app#restaurar={TOKEN}" in mail.text
    assert "14 de noviembre de 2026" in mail.text
    assert store.scrubbed == ["job-2"] and store.code_mail == {}


def test_the_restored_email_is_sent_and_its_payload_emptied():
    store, mailer = MailStore(), FakeMailer()
    payload = {"template": "offboarding_restored", "to": ADDRESS, "scope": "cuenta"}
    run(SendMail(store, mailer, "https://fichaitor.test")(Job("job-3", "send_mail", payload)))
    (mail,) = mailer.sent
    assert mail.to == ADDRESS and mail.subject == "Has restaurado Fichaitor"
    assert store.scrubbed == ["job-3"] and store.code_mail == {}


def test_a_temporary_failure_keeps_the_payload_for_the_retry_and_says_nothing_sensitive():
    store = MailStore()
    handler = SendMail(store, FakeMailer(fail=MailerError), "https://fichaitor.test")
    with pytest.raises(JobError) as caught:
        run(handler(code_job()))
    assert not isinstance(caught.value, PermanentJobError)
    assert store.scrubbed == [] and store.code_mail == {}
    assert CODE not in str(caught.value) and ADDRESS not in str(caught.value)
    # Out of attempts: the owner is told and what the job carried is erased.
    run(handler.on_exhausted(code_job()))
    assert store.code_mail == {"code-1": "failed"} and store.scrubbed == ["job-1"]


@pytest.mark.parametrize(
    ("mailer", "job"),
    [
        (FakeMailer(fail=MailRejected), code_job()),
        (None, code_job()),
        (FakeMailer(), code_job(template="otra")),
        (FakeMailer(), code_job(code="")),
        (FakeMailer(), code_job(scope="todo")),
    ],
)
def test_a_failure_for_good_is_recorded_and_the_payload_emptied_at_once(mailer, job):
    store = MailStore()
    with pytest.raises(PermanentJobError) as caught:
        run(SendMail(store, mailer, "https://fichaitor.test")(job))
    assert store.code_mail == {"code-1": "failed"} and store.scrubbed == ["job-1"]
    assert CODE not in str(caught.value) and ADDRESS not in str(caught.value)
    if isinstance(mailer, FakeMailer):
        assert mailer.sent == []


def test_what_a_dead_worker_left_in_finished_jobs_is_swept():
    store = MailStore()
    assert run(SendMail(store, FakeMailer(), "x").on_lost("send_mail")) == 2
    assert store.swept == ["send_mail"]


def test_through_the_worker_loop_nothing_sensitive_reaches_last_error():
    clock = [NOW]
    queue = MemoryJobQueue(lambda: clock[0])
    registry = HandlerRegistry()
    registry.register("send_mail", SendMail(MailStore(), FakeMailer(fail=MailerError), "x"))
    worker = Worker(queue, registry, worker_id="w1")

    async def go():
        await queue.enqueue("send_mail", code_job().payload, max_attempts=2)
        assert await worker.run_once()
        clock[0] += timedelta(hours=1)
        assert await worker.run_once()

    run(go())
    (job,) = queue.jobs.values()
    assert job.state == FAILED and job.attempts == 2
    assert CODE not in job.last_error and ADDRESS not in job.last_error


# --- apply_offboarding -------------------------------------------------------------

REFRESH_OLD, REFRESH_NEW = "1//refresh-antiguo-secreto", "1//refresh-nuevo-secreto"


class LeaveStore:
    """What ApplyOffboarding needs of the store, in memory."""

    def __init__(self, tokens=(REFRESH_OLD, REFRESH_NEW), reconnected=False, due=True) -> None:
        self.tokens = {
            f"conn-{i}": seal(PUBLIC, token) if token is not None else "no-es-un-cifrado"
            for i, token in enumerate(tokens)
        }
        self.reconnected = reconnected
        self.due = due
        self.state: str | None = None
        self.begun = 0

    async def begin_offboarding(self, offboarding_id, now):
        self.begun += 1
        if not self.due or self.state is not None:
            return None
        return EffectiveLeave(offboarding_id, "ficha", SUB)

    async def leave_tokens(self, offboarding_id):
        tokens = [TokenToRevoke(cid, enc, 1) for cid, enc in self.tokens.items()]
        return tokens, self.reconnected

    async def delete_connection(self, connection_id):
        del self.tokens[connection_id]

    async def finish_revocation(self, offboarding_id, state):
        self.state = state


def apply_job(attempts=1, max_attempts=5) -> Job:
    return Job("job-9", "apply_offboarding", {"offboarding_id": LEAVE_ID}, attempts, max_attempts)


def applier(store, google):
    return ApplyOffboarding(store, google, PRIVATE, lambda: NOW)


def worker_log(caplog) -> str:
    return "".join(
        r.getMessage() + json.dumps(getattr(r, "fields", {}), default=str)
        for r in caplog.records
        if r.name == "fichaitor.worker"
    )


def test_every_stored_connection_is_revoked_and_its_token_deleted(caplog):
    store, google = LeaveStore(), FakeGoogleProfile()
    with caplog.at_level(logging.INFO, logger="fichaitor.worker"):
        run(applier(store, google)(apply_job()))
    assert google.revoked == [REFRESH_OLD, REFRESH_NEW]  # all of them, not only the latest
    assert store.tokens == {} and store.state == "revoked"
    logged = worker_log(caplog)
    assert "offboarding_effective" in logged
    assert REFRESH_OLD not in logged and REFRESH_NEW not in logged and SUB not in logged


def test_a_leave_that_was_restored_or_is_not_due_is_left_alone():
    store, google = LeaveStore(due=False), FakeGoogleProfile()
    run(applier(store, google)(apply_job()))
    assert google.revoked == [] and len(store.tokens) == 2 and store.state is None


def test_a_temporary_failure_of_google_is_retried_and_the_token_stays_meanwhile():
    store = LeaveStore()
    google = FakeGoogleProfile()
    google.revoke_error = GoogleProfileError(f"sin respuesta {REFRESH_OLD}", 503)
    with pytest.raises(JobError) as caught:
        run(applier(store, google)(apply_job()))
    assert not isinstance(caught.value, PermanentJobError)
    assert REFRESH_OLD not in str(caught.value) and caught.value.__cause__ is None
    assert len(store.tokens) == 2 and store.state is None
    # Google is back: the retry finishes the job.
    google.revoke_error = None
    run(applier(store, google)(apply_job(attempts=2)))
    assert store.tokens == {} and store.state == "revoked"


def test_on_the_last_attempt_the_tokens_are_deleted_even_if_google_never_answered(caplog):
    store = LeaveStore()
    google = FakeGoogleProfile()
    google.revoke_error = GoogleProfileError("sin respuesta", 503)
    with caplog.at_level(logging.INFO, logger="fichaitor.worker"):
        run(applier(store, google)(apply_job(attempts=5)))  # no error: it gives up
    assert store.tokens == {} and store.state == "failed"
    record = next(r for r in caplog.records if r.getMessage() == "offboarding_effective")
    assert record.levelno == logging.WARNING and record.fields["not_revoked"] == 2


def test_a_revocation_google_refuses_for_good_is_recorded_and_the_token_deleted():
    store = LeaveStore()
    google = FakeGoogleProfile()
    google.revoke_error = GoogleRejected("revocación rechazada (estado 403)", 403)
    run(applier(store, google)(apply_job()))
    assert store.tokens == {} and store.state == "failed"


def test_a_token_that_cannot_be_decrypted_is_deleted_and_counted_as_not_revoked():
    store, google = LeaveStore(tokens=(None, REFRESH_NEW)), FakeGoogleProfile()
    run(applier(store, google)(apply_job()))
    assert google.revoked == [REFRESH_NEW] and store.tokens == {} and store.state == "failed"


def test_if_the_owner_connected_again_nothing_is_revoked_but_old_tokens_are_deleted():
    store, google = LeaveStore(reconnected=True), FakeGoogleProfile()
    run(applier(store, google)(apply_job()))
    assert getattr(google, "revoked", []) == []
    assert store.tokens == {} and store.state == "superseded"


def test_without_stored_connections_there_is_nothing_to_revoke():
    store, google = LeaveStore(tokens=()), FakeGoogleProfile()
    run(applier(store, google)(apply_job()))
    assert store.state == "none"


def test_applying_twice_revokes_once():
    store, google = LeaveStore(), FakeGoogleProfile()
    handler = applier(store, google)
    run(handler(apply_job()))
    run(handler(apply_job()))
    run(handler.on_exhausted(apply_job()))
    assert google.revoked == [REFRESH_OLD, REFRESH_NEW]


def test_a_payload_without_a_valid_id_fails_for_good():
    handler = applier(LeaveStore(), FakeGoogleProfile())
    for payload in ({}, {"offboarding_id": "x"}, {"offboarding_id": 7}):
        with pytest.raises(PermanentJobError):
            run(handler(Job("j", "apply_offboarding", payload)))


# --- retention: the handler ----------------------------------------------------------


class RetentionStore(LeaveStore):
    def __init__(self) -> None:
        super().__init__()
        self.passes: list[datetime] = []

    async def due_offboardings(self, now):
        return [(LEAVE_ID, False), ("22222222-2222-2222-2222-222222222222", True)]

    async def run_retention(self, now, policy):
        self.passes.append(now)
        return {"review": 3, "session": 1}


def test_the_daily_pass_applies_lost_leaves_deletes_and_logs_only_counts(caplog):
    store, google = RetentionStore(), FakeGoogleProfile()
    handler = Retention(store, applier(store, google), policy=None, clock=lambda: NOW)
    with caplog.at_level(logging.INFO, logger="fichaitor.worker"):
        run(handler(Job("j", "retention", {})))
    assert store.passes == [NOW] and store.state == "revoked"
    record = next(r for r in caplog.records if r.getMessage() == "retention_done")
    assert record.fields["deleted"] == {"review": 3, "session": 1}
    assert record.fields["offboardings_applied"] == 1
    assert SUB not in worker_log(caplog)


def test_one_leave_that_fails_does_not_stop_the_pass():
    store = RetentionStore()
    google = FakeGoogleProfile()
    google.revoke_error = GoogleProfileError("sin respuesta", 503)
    handler = Retention(store, applier(store, google), policy=None, clock=lambda: NOW)
    run(handler(Job("j", "retention", {})))
    assert store.passes == [NOW]


# --- the daily job the worker enqueues for itself ----------------------------------------


def daily_world(clock):
    queue = MemoryJobQueue(lambda: clock[0])
    ran: list[str] = []

    async def handler(job: Job) -> None:
        ran.append(job.id)

    def worker(name: str) -> Worker:
        registry = HandlerRegistry()
        registry.register("retention", handler)
        return Worker(
            queue,
            registry,
            worker_id=name,
            recover_interval=0.0,
            daily=[DailyJob("retention", hour_utc=3)],
            clock=lambda: clock[0],
        )

    return queue, ran, worker


def test_several_workers_enqueue_one_daily_job_between_them():
    clock = [datetime(2026, 11, 12, 1, 0, tzinfo=UTC)]
    queue, ran, worker = daily_world(clock)
    first, second = worker("w1"), worker("w2")

    async def go():
        # Before the hour: enqueued by whoever comes first, and not due yet.
        assert not await first.run_once() and not await second.run_once()
        assert [j.idempotency_key for j in queue.jobs.values()] == ["retention:2026-11-12"]
        (job,) = queue.jobs.values()
        assert job.run_at == datetime(2026, 11, 12, 3, 0, tzinfo=UTC) and job.state == PENDING
        # At the hour: one of them runs it, once.
        clock[0] = datetime(2026, 11, 12, 3, 0, tzinfo=UTC)
        assert await second.run_once()
        assert not await first.run_once() and not await second.run_once()
        assert len(ran) == 1 and job.state == DONE
        # The next day: a new job, again only one.
        clock[0] = datetime(2026, 11, 13, 0, 5, tzinfo=UTC)
        await first.run_once()
        await second.run_once()
        assert sorted(j.idempotency_key for j in queue.jobs.values()) == [
            "retention:2026-11-12",
            "retention:2026-11-13",
        ]

    run(go())


def test_a_worker_that_starts_after_the_hour_still_runs_todays_job_once():
    clock = [datetime(2026, 11, 12, 17, 30, tzinfo=UTC)]
    queue, ran, worker = daily_world(clock)

    async def go():
        assert await worker("w1").run_once()
        assert not await worker("w2").run_once()  # a restart: the key is already taken

    run(go())
    assert len(ran) == 1 and len(queue.jobs) == 1


def test_a_failure_to_enqueue_the_daily_job_does_not_stop_the_worker():
    clock = [NOW]
    queue, ran, worker = daily_world(clock)
    w = worker("w1")
    real = queue.enqueue
    calls = []

    async def broken(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("base de datos caída")
        return await real(*args, **kwargs)

    queue.enqueue = broken

    async def go():
        assert not await w.run_once()  # nothing enqueued, nothing broken
        assert await w.run_once()  # tried again and run

    run(go())
    assert len(ran) == 1


# --- jobs of an account whose leave is scheduled ---------------------------------------


def suspended_world():
    clock = [NOW]
    queue = MemoryJobQueue(lambda: clock[0])
    tables = MemoryTables()
    store = MemoryWorkerStore(queue, tables)
    store.add_connection(
        SUB,
        StoredConnection(
            seal(PUBLIC, "1//refresh"), 1, (ConnectedLocation("accounts/1", LOCATION),)
        ),
    )
    reviews = ReviewSet(
        (
            Review(
                rating=1,
                created_at=NOW - timedelta(days=2),
                replied=False,
                text="Mal.",
                review_id="g-low",
            ),
        )
    )
    return store, tables, FakeGoogleProfile(reviews=reviews), lambda: clock[0]


def test_no_job_of_an_account_with_a_scheduled_leave_reads_writes_or_analyses():
    store, tables, google, clock = suspended_world()
    sync = SyncProfile(store, google, PRIVATE, clock)
    target = {"google_sub": SUB, "location": LOCATION}
    run(sync(Job("j1", "sync_profile", target)))  # before the leave: a normal sync
    assert store.summaries and google.reads
    review = next(iter(tables.reviews.values()))
    action = tables.create_action(
        "00000000-0000-0000-0000-000000000001", review, "Lo sentimos.", NOW
    )

    store.suspended.add(SUB)
    reads, refreshed = len(google.reads), len(google.refreshed_with)
    summary_before = dict(store.summaries)
    run(sync(Job("j2", "sync_profile", target)))
    assert len(google.reads) == reads and len(google.refreshed_with) == refreshed
    assert store.summaries == summary_before

    run(
        ReplyReview(store, google, PRIVATE, clock)(
            Job("j3", "reply_review", {"action_id": action.id})
        )
    )
    assert google.replies == [] and google.write_attempts == 0
    assert tables.actions[action.id].state == actions.FAILED
    assert tables.actions[action.id].error_code == actions.ERROR_SUSPENDED
    assert "baja programada" in actions.status_message(actions.FAILED, actions.ERROR_SUSPENDED)
    assert tables.audit[-1].event == actions.EVENT_FAILED and tables.audit[-1].reason == "suspended"

    model = FakeSentimentModel()
    run(
        AnalyzeSentiment(store, model, SentimentPolicy(), clock)(
            Job("j4", "analyze_sentiment", target)
        )
    )
    assert model.classify_calls == [] and model.summary_calls == [] and tables.analyses == {}

    # Restored: the next sync works again.
    store.suspended.clear()
    run(sync(Job("j5", "sync_profile", target)))
    assert len(google.reads) > reads
