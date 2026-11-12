"""Multinegocio, step 5 (docs/multinegocio.md): the worker, in memory. No database, and
Google is the fake adapter: no real call is ever made.

- An action confirmed on a profile that is no longer active fails with
  `location_inactive` before anything reaches Google, and the conversation is told.
- The retention term of a deactivated profile is in the configuration, never above 30.
"""

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.adapters.crypto.sealed import generate_keypair, seal
from app.adapters.google_profile.fake import FakeGoogleProfile
from app.adapters.job_queue.memory import MemoryJobQueue
from app.adapters.memory_tables import MemoryTables
from app.adapters.worker_store.memory import MemoryWorkerStore
from app.domain import actions
from app.domain.channel_jobs import CHANNEL_ACTION_RESULT, result_error
from app.domain.jobs import DONE
from app.domain.offboarding import RetentionPolicy
from app.domain.profile_summary import Review, ReviewSet
from app.domain.special_hours import KIND_CLOSED, checked
from app.ports.worker_store import ConnectedLocation, StoredConnection
from app.retention_config import RetentionSettings
from app.worker.loop import Worker
from app.worker.registry import HandlerRegistry
from app.worker.reply_review import KIND as REPLY_KIND
from app.worker.reply_review import ReplyReview
from app.worker.set_special_hours import KIND as HOURS_KIND
from app.worker.set_special_hours import MAX_ATTEMPTS, SetSpecialHours
from app.worker.sync_profile import KIND as SYNC_KIND
from app.worker.sync_profile import SyncProfile

PUBLIC, PRIVATE = generate_keypair()
SUB = "pytest-sub-1"
ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"
GBP_ACCOUNT = "accounts/111"
LOCATION = "locations/222"
NOW = datetime(2026, 12, 3, 10, 0, tzinfo=UTC)
DAY = date(2026, 12, 8)
REVIEWS = ReviewSet(
    (
        Review(
            rating=1,
            created_at=NOW - timedelta(days=2),
            replied=False,
            text="La comida llegó fría.",
            review_id="g-low",
        ),
    )
)


def run(coro):
    return asyncio.run(coro)


class World:
    """One connected profile, synced, and a worker with both executors."""

    def __init__(self) -> None:
        self.now = NOW
        self.queue = MemoryJobQueue(lambda: self.now)
        self.tables = MemoryTables()
        self.store = MemoryWorkerStore(self.queue, self.tables)
        self.store.add_connection(
            SUB,
            StoredConnection(
                seal(PUBLIC, "1//refresh"), 1, (ConnectedLocation(GBP_ACCOUNT, LOCATION),)
            ),
        )
        self.google = FakeGoogleProfile(reviews=REVIEWS, access_token="ya29.access")
        clock = lambda: self.now  # noqa: E731
        registry = HandlerRegistry()
        registry.register(SYNC_KIND, SyncProfile(self.store, self.google, PRIVATE, clock))
        registry.register(
            REPLY_KIND, ReplyReview(self.store, self.google, PRIVATE, clock, results=self.queue)
        )
        registry.register(
            HOURS_KIND,
            SetSpecialHours(self.store, self.google, PRIVATE, clock, results=self.queue),
        )
        self.worker = Worker(self.queue, registry, worker_id="w1", poll_interval=0.01)

    async def drain(self) -> None:
        for _ in range(10):
            if not await self.worker.run_once():
                return

    async def reply(self):
        await self.queue.enqueue(SYNC_KIND, {"google_sub": SUB, "location": LOCATION})
        await self.drain()
        row = next(r for r in self.tables.reviews.values() if r.gbp_review_id == "g-low")
        action = self.tables.create_action(ACCOUNT_ID, row, "Lo sentimos.", self.now)
        await self.queue.enqueue(REPLY_KIND, {"action_id": action.id})
        await self.drain()
        return action

    async def hours(self):
        change = checked(DAY, KIND_CLOSED)
        action = self.tables.create_special_hours_action(
            ACCOUNT_ID, SUB, LOCATION, DAY, change.to_text(), self.now
        )
        await self.queue.enqueue(HOURS_KIND, {"action_id": action.id}, max_attempts=MAX_ATTEMPTS)
        await self.drain()
        return action

    def told(self):
        return [j.payload for j in self.queue.jobs.values() if j.kind == CHANNEL_ACTION_RESULT]

    def job_states(self, kind: str):
        return [j.state for j in self.queue.jobs.values() if j.kind == kind]


@pytest.mark.parametrize("kind", ["reply", "hours"])
def test_an_action_of_a_profile_deactivated_before_it_runs_never_reaches_google(kind):
    w = World()
    w.store.inactive.add((ACCOUNT_ID, LOCATION))
    action = run(w.reply() if kind == "reply" else w.hours())
    assert (action.state, action.error_code) == (actions.FAILED, actions.ERROR_LOCATION_INACTIVE)
    assert w.google.write_attempts == 0 and not w.google.special_writes
    assert w.google.refreshed_with == [] or kind == "reply"  # the reply's sync refreshed
    assert [a.reason for a in w.tables.audit if a.action_id == action.id][-1] == (
        actions.ERROR_LOCATION_INACTIVE
    )
    assert w.told() == [{"action_id": action.id, "ok": False, "error": "location_inactive"}]
    # Not an error of the job: nothing to retry, the owner reactivates and confirms again.
    assert w.job_states(REPLY_KIND if kind == "reply" else HOURS_KIND) == [DONE]


def test_an_action_of_an_active_profile_is_still_sent():
    w = World()
    action = run(w.reply())
    assert action.state == actions.DONE and w.google.replies


def test_a_forged_action_is_still_internal_even_on_an_inactive_profile():
    # The coherence check (M1) comes first: a forged row says nothing about the profile.
    w = World()
    w.store.inactive.add((ACCOUNT_ID, LOCATION))
    w.store.not_owned.add((ACCOUNT_ID, LOCATION))
    action = run(w.hours())
    assert action.error_code == actions.ERROR_INTERNAL


def test_the_owner_reads_why_the_action_was_not_sent():
    for action_type in (actions.REPLY_REVIEW, actions.SET_SPECIAL_HOURS):
        message = actions.status_message(
            actions.FAILED, actions.ERROR_LOCATION_INACTIVE, action_type
        )
        assert "no está activa" in message
    assert result_error(actions.ERROR_LOCATION_INACTIVE) == "failed"


def test_the_term_of_a_deactivated_profile_is_30_days_and_never_longer(monkeypatch):
    assert RetentionPolicy().inactive_days == 30
    monkeypatch.setenv("FICHAITOR_RETENTION_INACTIVE_DAYS", "7")
    assert RetentionSettings(_env_file=None).policy().inactive_days == 7
    monkeypatch.setenv("FICHAITOR_RETENTION_INACTIVE_DAYS", "31")
    with pytest.raises(ValidationError):
        RetentionSettings(_env_file=None)
