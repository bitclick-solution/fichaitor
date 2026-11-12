"""Integration: leaving and the retention task in PostgreSQL, with the roles of the API
and of the worker.

Skipped unless the three URLs are set. They must point to the TEST database (its name
ends in `_test`; tests/conftest.py stops the run otherwise). Create it with
scripts/preparar_bd_pruebas.sh:

    FICHAITOR_TEST_OWNER_URL=postgresql://fichaitor:dev@127.0.0.1:5433/fichaitor_test \
    FICHAITOR_TEST_DATABASE_URL=postgresql://fichaitor_api_dev:dev@127.0.0.1:5433/fichaitor_test \
    FICHAITOR_TEST_WORKER_URL=postgresql://fichaitor_worker_dev:dev@127.0.0.1:5433/fichaitor_test \
    uv run pytest -q tests/test_offboarding_postgres.py

Google is the fake adapter and no email leaves: nothing is revoked or sent for real.
Every date comes from the clock of the test.
"""

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from app.adapters.crypto.sealed import generate_keypair, seal
from app.adapters.google_profile.fake import FakeGoogleProfile
from app.adapters.mailer.fake import FakeMailer
from app.adapters.store.postgres import PostgresStore
from app.adapters.worker_store.postgres import PostgresWorkerStore
from app.application.offboarding import CodeGone, Offboarding
from app.domain.jobs import Job
from app.domain.offboarding import RetentionPolicy
from app.domain.profile_summary import Review, ReviewSet
from app.ports.google_oauth import ProfileLocation
from app.ports.store import GoogleConnection
from app.worker.apply_offboarding import ApplyOffboarding
from app.worker.retention import Retention
from app.worker.send_mail import SendMail
from app.worker.sync_profile import SyncProfile

API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")
WORKER_URL = os.environ.get("FICHAITOR_TEST_WORKER_URL")

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL and WORKER_URL), reason="sin base de datos de pruebas configurada"
)

PUBLIC, PRIVATE = generate_keypair()
# Later than the real clock: rows the database dates itself (connections, jobs) are
# always "before" what the tests schedule.
NOW = datetime(2027, 3, 10, 12, 0, tzinfo=UTC)
GRACE = timedelta(hours=48)
DAY = timedelta(days=1)
ANA, LUIS = "pytest-ana", "pytest-luis"
SHARED = ProfileLocation(
    "accounts/pytest-1", "locations/pytest-compartida", "Bar compartido", "place-pytest-9", True
)
POLICY = RetentionPolicy()
DENIED = asyncpg.InsufficientPrivilegeError
SECRET = "secreto-de-pruebas"

REVIEWS = ReviewSet(
    (
        Review(
            rating=5,
            created_at=NOW - timedelta(days=3),
            replied=False,
            text="Todo perfecto.",
            review_id="pytest-five",
        ),
        Review(
            rating=1,
            created_at=NOW - timedelta(days=9),
            replied=False,
            text="La comida llegó fría.",
            review_id="pytest-low",
        ),
    )
)


def run(coro):
    return asyncio.run(coro)


async def _as(url: str, sql: str, *args):
    conn = await asyncpg.connect(url)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def owner(sql: str, *args):
    return await _as(OWNER_URL, sql, *args)


async def count(table: str, where: str = "true", *args) -> int:
    return (await owner(f"SELECT count(*) AS n FROM {table} WHERE {where}", *args))[0]["n"]  # noqa: S608


async def cleanup():
    mine = (
        "(SELECT id FROM account WHERE google_sub LIKE 'pytest-%' OR google_sub LIKE 'deleted:%')"
    )
    await owner(f"DELETE FROM audit_log WHERE account_id IN {mine}")  # noqa: S608
    await owner(f"DELETE FROM action WHERE account_id IN {mine}")  # noqa: S608
    await owner("DELETE FROM review WHERE google_sub LIKE 'pytest-%'")
    await owner("DELETE FROM job WHERE kind LIKE 'pytest-%'")
    await owner("DELETE FROM job WHERE payload->>'google_sub' LIKE 'pytest-%'")
    for table in ("sentiment_analysis", "sentiment_history", "sentiment_usage", "profile_summary"):
        await owner(f"DELETE FROM {table} WHERE google_sub LIKE 'pytest-%'")  # noqa: S608
    await owner("DELETE FROM google_connection WHERE google_sub LIKE 'pytest-%'")
    await owner(f"UPDATE account SET selected_location_id = NULL WHERE id IN {mine}")  # noqa: S608
    await owner(f"DELETE FROM offboarding_code WHERE account_id IN {mine}")  # noqa: S608
    await owner(f"DELETE FROM offboarding WHERE account_id IN {mine}")  # noqa: S608
    await owner(f"DELETE FROM session WHERE account_id IN {mine}")  # noqa: S608
    await owner(f"DELETE FROM location WHERE account_id IN {mine}")  # noqa: S608
    await owner(f"DELETE FROM account WHERE id IN {mine}")  # noqa: S608
    await owner("DELETE FROM waitlist_entry WHERE contact LIKE 'pytest-%'")
    await owner("DELETE FROM retention_run")


@pytest.fixture(autouse=True)
def clean():
    run(cleanup())
    yield
    run(cleanup())


class Scene:
    """The API's store and the worker's store on the database, each with its own role,
    one clock for both, a fake Google and a fake mailer."""

    def __init__(self) -> None:
        kind = f"pytest-{uuid.uuid4().hex[:12]}"
        self.mail_kind, self.apply_kind = f"{kind}-mail", f"{kind}-apply"
        self.store = PostgresStore(
            API_URL,
            reply_job_kind=kind,
            mail_job_kind=self.mail_kind,
            apply_job_kind=self.apply_kind,
        )
        self.now = NOW
        self.google = FakeGoogleProfile(reviews=REVIEWS)
        self.mailer = FakeMailer()
        self.tokens = iter(f"tok-{i}" for i in range(100))

    def clock(self) -> datetime:
        return self.now

    async def __aenter__(self) -> "Scene":
        await self.store.connect()
        self.pool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=4)
        self.wstore = PostgresWorkerStore(self.pool)
        self.offboarding = Offboarding(
            self.store, secret=SECRET, mail_available=True, clock=lambda: self.now.timestamp()
        )
        self.apply = ApplyOffboarding(self.wstore, self.google, PRIVATE, self.clock)
        self.retention = Retention(self.wstore, self.apply, POLICY, self.clock)
        self.send_mail = SendMail(self.wstore, self.mailer, "https://fichaitor.test")
        self.sync_job = SyncProfile(self.wstore, self.google, PRIVATE, self.clock)
        return self

    async def __aexit__(self, *exc) -> None:
        await self.pool.close()
        await self.store.close()

    # --- setting the stage -----------------------------------------------------

    async def connect(self, sub: str, token: str | None = None, at: datetime | None = None) -> str:
        """Connect with Google, as the OAuth callback does. Returns the account id."""
        account_id = await self.store.upsert_account(sub, f"{sub}@example.com")
        await self.store.sync_locations(account_id, [SHARED], self.now)
        await self.store.add_google_connection(
            GoogleConnection(
                sub,
                f"{sub}@example.com",
                seal(PUBLIC, token or f"refresh-{sub}"),
                1,
                ("scope",),
                ({"account": SHARED.account, "location": SHARED.location},),
            )
        )
        if at is not None:
            await owner(
                "UPDATE google_connection SET connected_at = $2 WHERE google_sub = $1"
                " AND connected_at = (SELECT max(connected_at) FROM google_connection"
                "                     WHERE google_sub = $1)",
                sub,
                at,
            )
        return account_id

    async def select(self, account_id: str) -> None:
        rows = await owner(
            "SELECT id FROM location WHERE account_id = $1::uuid AND gbp_location = $2",
            account_id,
            SHARED.location,
        )
        assert await self.store.select_location(account_id, rows[0]["id"])

    async def sync(self, sub: str) -> None:
        job = Job("pytest-sync", "sync_profile", {"google_sub": sub, "location": SHARED.location})
        await self.sync_job(job)

    async def session(self, account_id: str):
        token_hash = uuid.uuid4().hex + uuid.uuid4().hex
        await self.store.create_session(
            account_id, token_hash, self.now, self.now + timedelta(days=30)
        )
        return token_hash

    async def me(self, token_hash: str):
        return await self.store.account_for_session(token_hash, self.now)

    async def furnished(self, sub: str) -> tuple[str, str]:
        """An account with everything the product stores about it: two connections,
        a synced profile with reviews, their classification, a draft, an analysis with
        history and usage, a published reply with its audit rows, and a finished job.
        Returns (account id, session token hash)."""
        account_id = await self.connect(sub, f"refresh-old-{sub}")
        await self.select(account_id)
        await self.sync(sub)
        # Connected again and not synced since: a good sync deletes the connections the
        # newest one replaced (test_worker_postgres.py), so both are still stored here.
        await self.connect(sub, f"refresh-new-{sub}")
        reviews = await owner("SELECT id, gbp_review_id FROM review WHERE google_sub = $1", sub)
        for review in reviews:
            await owner(
                "INSERT INTO review_sentiment (review_id, source_hash, sentiment, method)"
                " VALUES ($1, repeat('a', 64), 'positive', 'model')",
                review["id"],
            )
        first = reviews[0]
        await owner(
            "INSERT INTO review_draft (review_id, source_hash, draft)"
            " VALUES ($1, repeat('b', 64), 'Gracias por venir.')",
            first["id"],
        )
        await owner(
            "INSERT INTO sentiment_analysis (google_sub, location, status, generated_at, based_on,"
            " score, overall) VALUES ($1, $2, 'ok', $3, 2, 50, 'mixto')",
            sub,
            SHARED.location,
            self.now,
        )
        await owner(
            "INSERT INTO sentiment_history (google_sub, location, month, score, based_on)"
            " VALUES ($1, $2, '2027-03-01', 50, 2)",
            sub,
            SHARED.location,
        )
        await owner(
            "INSERT INTO sentiment_usage (google_sub, location, trigger) VALUES ($1, $2, 'first')",
            sub,
            SHARED.location,
        )
        action = await owner(
            "INSERT INTO action (account_id, type, google_sub, location, review_id, text, state)"
            " VALUES ($1::uuid, 'reply_review', $2, $3, $4, 'Gracias.', 'done') RETURNING id",
            account_id,
            sub,
            SHARED.location,
            first["id"],
        )
        for event in ("reply_confirmed", "reply_sent"):
            await owner(
                "INSERT INTO audit_log (event, account_id, action_id, action_type, location,"
                " review_id, gbp_review_id, text, confirmed_at)"
                " VALUES ($1, $2::uuid, $3, 'reply_review', $4, $5, $6, 'Gracias.', $7)",
                event,
                account_id,
                action[0]["id"],
                SHARED.location,
                first["id"],
                first["gbp_review_id"],
                self.now,
            )
        await owner(
            "INSERT INTO job (kind, payload, state, created_at)"
            " VALUES ('pytest-old-sync', $1::jsonb, 'done', $2)",
            json.dumps({"google_sub": sub, "location": SHARED.location}),
            self.now - DAY,
        )
        return account_id, await self.session(account_id)

    # --- the leave ---------------------------------------------------------------

    async def mail_payloads(self) -> list[dict]:
        rows = await owner(
            "SELECT payload::text AS payload FROM job WHERE kind = $1 ORDER BY created_at",
            self.mail_kind,
        )
        return [json.loads(r["payload"]) for r in rows]

    async def leave(self, token_hash: str, scope: str = "ficha") -> datetime:
        """Ask for a code and confirm with it, as the routes do. Returns delete_at."""
        await self.offboarding.request_code(await self.me(token_hash), scope)
        code = [p for p in await self.mail_payloads() if p.get("code")][-1]["code"]
        return await self.offboarding.confirm(await self.me(token_hash), scope, code)

    async def restore_token(self) -> str:
        return [p for p in await self.mail_payloads() if p.get("token")][-1]["token"]

    async def leave_id(self, account_id: str) -> str:
        rows = await owner(
            "SELECT id::text AS id FROM offboarding WHERE account_id = $1::uuid"
            " ORDER BY confirmed_at DESC LIMIT 1",
            account_id,
        )
        return rows[0]["id"]

    async def apply_leave(self, account_id: str, **job) -> None:
        payload = {"offboarding_id": await self.leave_id(account_id)}
        await self.apply(Job("pytest-apply", self.apply_kind, payload, **job))

    async def effective(self, sub: str, scope: str = "ficha") -> tuple[str, str, datetime]:
        """A furnished account whose leave is already effective.
        Returns (account id, session token hash, when it became effective)."""
        account_id, token_hash = await self.furnished(sub)
        delete_at = await self.leave(token_hash, scope)
        self.now = delete_at
        await self.apply_leave(account_id)
        return account_id, token_hash, delete_at

    async def retain(self, at: datetime) -> dict[str, int]:
        self.now = at
        return await self.wstore.run_retention(at, POLICY)

    async def reads(self, account_id: str):
        """Everything a route could show of the profile of that account."""
        profile = await self.store.selected_profile(account_id)
        reviews = await self.store.list_reviews(account_id, unanswered_only=False, limit=50)
        sentiment = await self.store.sentiment(account_id, self.now)
        return profile, reviews.total, sentiment

    async def stored(self, sub: str, account_id: str) -> dict[str, int]:
        """Rows of everything obtained from Google for that user."""
        return {
            "google_connection": await count("google_connection", "google_sub = $1", sub),
            "profile_summary": await count("profile_summary", "google_sub = $1", sub),
            "review": await count("review", "google_sub = $1", sub),
            "review_sentiment": await count(
                "review_sentiment s JOIN review r ON r.id = s.review_id", "r.google_sub = $1", sub
            ),
            "review_draft": await count(
                "review_draft d JOIN review r ON r.id = d.review_id", "r.google_sub = $1", sub
            ),
            "sentiment_analysis": await count("sentiment_analysis", "google_sub = $1", sub),
            "sentiment_history": await count("sentiment_history", "google_sub = $1", sub),
            "sentiment_usage": await count("sentiment_usage", "google_sub = $1", sub),
            "location": await count("location", "account_id = $1::uuid", account_id),
            "job": await count("job", "payload->>'google_sub' = $1", sub),
        }


FULL = {
    "google_connection": 2,
    "profile_summary": 1,
    "review": 2,
    "review_sentiment": 2,
    "review_draft": 1,
    "sentiment_analysis": 1,
    "sentiment_history": 1,
    "sentiment_usage": 1,
    "location": 1,
    "job": 1,
}
EMPTY = dict.fromkeys(FULL, 0)


def scene(scenario):
    async def go():
        async with Scene() as s:
            return await scenario(s)

    return run(go())


# --- the code and the token are never stored in clear -------------------------------


def test_the_code_and_the_restore_token_exist_only_in_the_mail_job_until_it_is_sent():
    async def scenario(s: Scene):
        account_id, token_hash = await s.furnished(ANA)
        await s.offboarding.request_code(await s.me(token_hash), "ficha")
        code = (await s.mail_payloads())[-1]["code"]
        await s.offboarding.confirm(await s.me(token_hash), "ficha", code)
        token = await s.restore_token()

        async def everywhere() -> str:
            dump = ""
            for table in ("offboarding_code", "offboarding", "job", "account", "session"):
                rows = await owner(f"SELECT t::text AS row FROM {table} t")  # noqa: S608
                dump += "".join(r["row"] for r in rows)
            return dump

        before = await everywhere()
        assert code in before and token in before  # only inside the two mail jobs
        codes = await owner("SELECT * FROM offboarding_code")
        leaves = await owner("SELECT * FROM offboarding")
        assert code not in str(codes) and token not in str(leaves)
        assert len(codes[0]["code_hash"]) == 64 and len(leaves[0]["restore_token_hash"]) == 64

        # The worker sends both emails: the payloads are emptied.
        jobs = await owner(
            "SELECT id::text AS id, payload::text AS payload FROM job WHERE kind = $1"
            " ORDER BY created_at",
            s.mail_kind,
        )
        for job in jobs:
            await s.send_mail(Job(job["id"], s.mail_kind, json.loads(job["payload"])))
        after = await everywhere()
        assert code not in after and token not in after and f"{ANA}@example.com" in after
        left = await owner(
            "SELECT payload::text AS p, last_error FROM job WHERE kind = $1", s.mail_kind
        )
        assert [r["p"] for r in left] == ["{}", "{}"] and all(r["last_error"] is None for r in left)
        assert (await owner("SELECT mail_state FROM offboarding_code"))[0]["mail_state"] == "sent"
        assert await s.store.offboarding_code_mail(account_id) is None  # spent
        return s.mailer.sent, code, token

    sent, code, token = scene(scenario)
    assert code in sent[0].text and f"#restaurar={token}" in sent[1].text


def test_what_a_dead_worker_left_in_a_finished_mail_job_is_emptied():
    async def scenario(s: Scene):
        await owner(
            "INSERT INTO job (kind, payload, state) VALUES"
            " ($1, '{\"code\": \"123456\"}', 'failed'), ($1, '{\"code\": \"654321\"}', 'pending')",
            s.mail_kind,
        )
        swept = await s.send_mail.on_lost(s.mail_kind)
        rows = await owner("SELECT state, payload::text AS p FROM job WHERE kind = $1", s.mail_kind)
        return swept, {r["state"]: r["p"] for r in rows}

    swept, rows = scene(scenario)
    assert swept == 1 and rows == {"failed": "{}", "pending": '{"code": "654321"}'}


# --- while the leave is scheduled ------------------------------------------------------


def test_a_scheduled_leave_closes_the_data_at_once_touches_nothing_and_can_be_restored():
    async def scenario(s: Scene):
        ana, ana_session = await s.furnished(ANA)
        luis, _ = await s.furnished(LUIS)  # manages the very same Google profile
        open_before = await s.reads(ana)
        delete_at = await s.leave(ana_session)
        closed = await s.reads(ana)
        luis_reads = await s.reads(luis)
        suspended = (await s.wstore.is_suspended(ANA), await s.wstore.is_suspended(LUIS))
        me = await s.me(ana_session)
        # The jobs of that account do nothing meanwhile.
        s.google.reads.clear()
        await s.sync(ANA)
        no_reads = list(s.google.reads)
        untouched = await s.stored(ANA, ana)
        # One minute before the date the leave is not applied.
        s.now = delete_at - timedelta(minutes=1)
        await s.apply_leave(ana)
        still = (await s.stored(ANA, ana), list(s.google.revoked))
        restored = await s.store.restore_offboarding(ana, s.now)
        back = await s.reads(ana)
        # A restored leave is never applied, not even past its date.
        s.now = delete_at + DAY
        await s.apply_leave(ana)
        await s.retention(Job("pytest-ret", "retention", {}))
        after = (await s.stored(ANA, ana), list(s.google.revoked), await s.reads(ana))
        return (
            open_before,
            delete_at,
            closed,
            luis_reads,
            suspended,
            me,
            no_reads,
            untouched,
            still,
            restored,
            back,
            after,
        )

    (
        open_before,
        delete_at,
        closed,
        luis_reads,
        suspended,
        me,
        no_reads,
        untouched,
        still,
        restored,
        back,
        after,
    ) = scene(scenario)
    assert open_before[0] is not None and open_before[1] == 2 and open_before[2] is not None
    assert delete_at == NOW + GRACE
    assert closed == (None, 0, None)
    assert luis_reads[0] is not None and luis_reads[1] == 2 and luis_reads[2] is not None
    assert suspended == (True, False)
    assert me.pending_deletion.scope == "ficha" and me.pending_deletion.delete_at == delete_at
    assert no_reads == []
    assert untouched == FULL and still == (FULL, [])
    assert restored is True and back[1] == 2 and back[0] is not None
    assert after[0] == FULL and after[1] == [] and after[2][1] == 2


def test_the_restore_link_works_once_until_the_date_and_says_nothing_otherwise():
    async def scenario(s: Scene):
        ana, session = await s.furnished(ANA)
        delete_at = await s.leave(session)
        token = await s.restore_token()
        wrong = await s.offboarding.restore_by_token("no-existe")
        s.now = delete_at  # at the date it is too late
        late = await s.offboarding.restore_by_token(token)
        s.now = delete_at - timedelta(seconds=1)
        good = await s.offboarding.restore_by_token(token)
        again = await s.offboarding.restore_by_token(token)
        return ana, wrong, late, good, again, (await s.me(session)).pending_deletion

    ana, wrong, late, good, again, pending = scene(scenario)
    assert wrong is None and late is None and again is None
    assert good == ana and pending is None


def test_a_code_is_single_use_and_bound_to_its_session_in_the_database():
    async def scenario(s: Scene):
        ana, session = await s.furnished(ANA)
        other = await s.session(ana)
        await s.offboarding.request_code(await s.me(session), "ficha")
        code = (await s.mail_payloads())[-1]["code"]
        outcomes = []
        for who, scope in ((other, "ficha"), (session, "cuenta")):
            try:
                await s.offboarding.confirm(await s.me(who), scope, code)
                outcomes.append("ok")
            except Exception as exc:
                outcomes.append(type(exc).__name__)
        await s.offboarding.confirm(await s.me(session), "ficha", code)
        await s.store.restore_offboarding(ana, s.now)
        with pytest.raises(CodeGone):
            await s.offboarding.confirm(await s.me(session), "ficha", code)
        return outcomes, (await owner("SELECT attempts FROM offboarding_code"))[0]["attempts"]

    outcomes, attempts = scene(scenario)
    assert outcomes == ["CodeWrong", "CodeWrong"] and attempts == 3


# --- when the date arrives ---------------------------------------------------------------


def test_at_48_hours_every_token_is_revoked_and_deleted_and_the_account_is_left_without_profiles():
    async def scenario(s: Scene):
        ana, session = await s.furnished(ANA)
        luis, _ = await s.furnished(LUIS)
        delete_at = await s.leave(session)
        s.now = delete_at  # exactly 48 hours later
        await s.apply_leave(ana)
        account = (await owner("SELECT * FROM account WHERE id = $1::uuid", ana))[0]
        leave = (await owner("SELECT * FROM offboarding WHERE account_id = $1::uuid", ana))[0]
        me = await s.me(session)
        return (
            list(s.google.revoked),
            await s.stored(ANA, ana),
            await s.stored(LUIS, luis),
            await s.reads(ana),
            await s.reads(luis),
            account,
            leave,
            me,
            await s.store.list_locations(ana),
            await s.store.restore_offboarding(ana, s.now),
            await s.offboarding.restore_by_token(await s.restore_token()),
            delete_at,
        )

    (
        revoked,
        ana_rows,
        luis_rows,
        ana_reads,
        luis_reads,
        account,
        leave,
        me,
        locations,
        r1,
        r2,
        at,
    ) = scene(scenario)
    # Every stored connection of that account, not only the latest; nothing of Luis.
    assert sorted(revoked) == [f"refresh-new-{ANA}", f"refresh-old-{ANA}"]
    # The tokens are gone at once; the rest waits for its term but nobody can read it.
    assert ana_rows == {**FULL, "google_connection": 0}
    assert luis_rows == FULL
    assert ana_reads == (None, 0, None)
    assert luis_reads[1] == 2 and luis_reads[0] is not None
    assert account["disconnected_at"] == at and account["deleted_at"] is None
    assert account["selected_location_id"] is None and account["google_sub"] == ANA
    assert leave["effective_at"] == at and leave["revoke_state"] == "revoked"
    assert leave["restore_token_hash"] is None
    # The account lives on, without profiles, and says it is not connected.
    assert me is not None and me.connected is False and me.pending_deletion is None
    assert locations == []
    assert r1 is False and r2 is None  # too late to restore


def test_if_google_never_answers_the_tokens_are_deleted_all_the_same_and_it_is_recorded():
    async def scenario(s: Scene):
        from app.ports.google_profile import GoogleProfileError

        ana, session = await s.furnished(ANA)
        s.now = await s.leave(session)
        s.google.revoke_error = GoogleProfileError("sin respuesta", 503)
        errors = []
        for attempt in (1, 2):
            try:
                await s.apply_leave(ana, attempts=attempt, max_attempts=3)
            except Exception as exc:
                errors.append(str(exc))
        kept = await count("google_connection", "google_sub = $1", ANA)
        closed = await s.reads(ana)  # effective from the first attempt
        await s.apply_leave(ana, attempts=3, max_attempts=3)
        leave = (await owner("SELECT revoke_state FROM offboarding"))[0]["revoke_state"]
        return errors, kept, closed, await count("google_connection", "google_sub = $1", ANA), leave

    errors, kept, closed, left, state = scene(scenario)
    assert len(errors) == 2 and all("refresh" not in e for e in errors)
    assert kept == 2 and closed == (None, 0, None)
    assert left == 0 and state == "failed"


def test_the_daily_pass_applies_a_leave_whose_job_was_lost():
    async def scenario(s: Scene):
        ana, session = await s.furnished(ANA)
        delete_at = await s.leave(session)
        s.now = delete_at - timedelta(minutes=1)
        await s.retention(Job("pytest-ret", "retention", {}))
        before = list(s.google.revoked)
        s.now = delete_at
        await s.retention(Job("pytest-ret", "retention", {}))
        return (
            before,
            sorted(s.google.revoked),
            await count("google_connection", "google_sub = $1", ANA),
        )

    before, revoked, left = scene(scenario)
    assert before == [] and len(revoked) == 2 and left == 0


# --- deleting the account ------------------------------------------------------------------


def test_deleting_the_account_closes_sessions_frees_the_identity_and_inherits_nothing():
    async def scenario(s: Scene):
        ana, session = await s.furnished(ANA)
        delete_at = await s.leave(session, "cuenta")
        closed_now = await s.me(session)  # every session, at once
        # Signing in during the 48 hours: the same account, with its leave on show.
        same = await s.store.upsert_account(ANA, f"{ANA}@example.com")
        visitor = await s.session(same)
        seen = await s.me(visitor)
        s.now = delete_at
        await s.apply_leave(ana)
        account = (await owner("SELECT * FROM account WHERE id = $1::uuid", ana))[0]
        gone = await s.me(visitor)
        # A later sign-in with the same Google identity: a new, empty account.
        new = await s.connect(ANA, "refresh-de-la-cuenta-nueva", at=delete_at + timedelta(hours=1))
        await s.select(new)
        new_session = await s.session(new)
        inherited = await s.reads(new)  # the old rows are still there, and unreachable
        old_reviews = {
            r["id"] for r in await owner("SELECT id FROM review WHERE google_sub = $1", ANA)
        }
        s.now = delete_at + timedelta(hours=2)
        await s.sync(ANA)  # the first sync of the new account purges what was left
        fresh = await s.reads(new)
        new_reviews = {
            r["id"] for r in await owner("SELECT id FROM review WHERE google_sub = $1", ANA)
        }
        leave = (await owner("SELECT * FROM offboarding WHERE account_id = $1::uuid", ana))[0]
        return (
            ana,
            same,
            new,
            closed_now,
            seen,
            account,
            gone,
            inherited,
            fresh,
            old_reviews,
            new_reviews,
            leave,
            await s.me(new_session),
            list(s.google.revoked),
            await count("sentiment_analysis", "google_sub = $1", ANA),
            await count("google_connection", "google_sub = $1", ANA),
            await count("audit_log", "account_id = $1::uuid", ana),
        )

    (
        ana,
        same,
        new,
        closed_now,
        seen,
        account,
        gone,
        inherited,
        fresh,
        old_reviews,
        new_reviews,
        leave,
        new_me,
        revoked,
        analyses,
        connections,
        audit,
    ) = scene(scenario)
    assert closed_now is None
    assert same == ana and seen.pending_deletion.scope == "cuenta"
    assert account["deleted_at"] is not None and account["email"] == ""
    assert account["google_sub"] == f"deleted:{ana}" and account["selected_location_id"] is None
    assert gone is None  # the session opened during the 48 hours is closed too
    assert new != ana and new_me.email == f"{ANA}@example.com" and new_me.pending_deletion is None
    assert inherited[1] == 0 and inherited[2] is None and inherited[0].summary is None
    assert fresh[1] == 2 and fresh[0].summary is not None and fresh[2] is None
    assert old_reviews.isdisjoint(new_reviews) and len(new_reviews) == 2
    assert leave["purged_at"] is not None and leave["google_sub"] is None
    assert sorted(revoked) == [f"refresh-new-{ANA}", f"refresh-old-{ANA}"]
    assert analyses == 0 and connections == 1  # only the new connection is left
    assert audit == 2  # the record of replies waits for its 12 months


def test_a_deleted_account_never_changes_again_and_only_the_worker_can_date_a_leave():
    async def scenario(s: Scene):
        ana, _, _ = await s.effective(ANA, "cuenta")
        luis = await s.connect(LUIS)
        await owner(
            "UPDATE account SET email = 'otra@example.com', google_sub = 'pytest-robada',"
            " deleted_at = NULL, disconnected_at = NULL WHERE id = $1::uuid",
            ana,
        )
        account = (await owner("SELECT * FROM account WHERE id = $1::uuid", ana))[0]
        attempts = {}
        for name, sql in {
            "api dates a disconnection": "UPDATE account SET disconnected_at = now()"
            " WHERE id = $1::uuid",
            "api deletes": "UPDATE account SET deleted_at = now() WHERE id = $1::uuid",
        }.items():
            try:
                await _as(API_URL, sql, luis)
                attempts[name] = "ok"
            except DENIED:
                attempts[name] = "denied"
        await _as(API_URL, "UPDATE account SET disconnected_at = NULL WHERE id = $1::uuid", luis)
        return account, ana, attempts

    account, ana, attempts = scene(scenario)
    assert account["email"] == "" and account["google_sub"] == f"deleted:{ana}"
    assert account["deleted_at"] is not None and account["disconnected_at"] is not None
    assert attempts == {"api dates a disconnection": "denied", "api deletes": "denied"}


# --- connecting again -------------------------------------------------------------------------


def test_connecting_again_starts_from_nothing_and_deletes_what_was_left_at_once():
    async def scenario(s: Scene):
        ana, session, at = await s.effective(ANA)
        old_reviews = {
            r["id"] for r in await owner("SELECT id FROM review WHERE google_sub = $1", ANA)
        }
        # Two days later the owner connects again.
        s.now = at + 2 * DAY
        again = await s.connect(ANA, "refresh-reconectado", at=s.now)
        me = await s.me(session)
        await s.select(ana)
        gated = await s.reads(ana)  # nothing of before comes back
        await s.sync(ANA)
        fresh = await s.reads(ana)
        new_reviews = {
            r["id"] for r in await owner("SELECT id FROM review WHERE google_sub = $1", ANA)
        }
        leave = (await owner("SELECT * FROM offboarding WHERE account_id = $1::uuid", ana))[0]
        # The term passes: there is nothing left to delete, and the new data stays.
        counts = await s.retain(at + 31 * DAY)
        return (
            ana,
            again,
            me,
            gated,
            fresh,
            old_reviews,
            new_reviews,
            leave,
            counts,
            await s.reads(ana),
        )

    ana, again, me, gated, fresh, old, new, leave, counts, later = scene(scenario)
    assert again == ana and me.connected is True
    assert gated[1] == 0 and gated[2] is None and gated[0].summary is None
    assert fresh[1] == 2 and fresh[0].summary is not None and fresh[2] is None
    assert old.isdisjoint(new)
    assert leave["purged_at"] is not None
    assert counts.get("review", 0) == 0 and later[1] == 2


def test_a_revocation_finished_after_the_owner_reconnected_does_not_cut_the_new_connection():
    async def scenario(s: Scene):
        from app.ports.google_profile import GoogleProfileError

        ana, session = await s.furnished(ANA)
        delete_at = await s.leave(session)
        # At the date the leave becomes effective, but Google does not answer.
        s.now = delete_at
        s.google.revoke_error = GoogleProfileError("sin respuesta", 503)
        with pytest.raises(Exception, match="fallo temporal"):
            await s.apply_leave(ana)
        # Before the retry, the owner connects again.
        await s.connect(ANA, "refresh-posterior", at=delete_at + timedelta(hours=1))
        s.google.revoke_error = None
        s.now = delete_at + timedelta(hours=2)
        await s.apply_leave(ana, attempts=2)
        left = await count("google_connection", "google_sub = $1", ANA)
        state = (await owner("SELECT revoke_state FROM offboarding"))[0]["revoke_state"]
        return list(s.google.revoked), left, state

    revoked, left, state = scene(scenario)
    # Revoking any token of the grant would cut the new connection: the old ones are
    # only deleted.
    assert revoked == [] and left == 1 and state == "superseded"


# --- retention: exactly what is past its term ----------------------------------------------------


def test_what_came_from_google_is_deleted_30_days_after_the_leave_and_not_a_day_before():
    async def scenario(s: Scene):
        ana, _, at = await s.effective(ANA)
        luis, _ = await s.furnished(LUIS)
        early = await s.retain(at + 30 * DAY - timedelta(minutes=1))
        before = await s.stored(ANA, ana)
        counts = await s.retain(at + 30 * DAY)
        after = await s.stored(ANA, ana)
        leave = (await owner("SELECT * FROM offboarding WHERE account_id = $1::uuid", ana))[0]
        again = await s.retain(at + 30 * DAY)  # a second pass deletes nothing more
        runs = await owner("SELECT deleted::text AS deleted FROM retention_run ORDER BY id")
        return (
            early,
            before,
            counts,
            after,
            leave,
            again,
            [json.loads(r["deleted"]) for r in runs],
            await s.stored(LUIS, luis),
            await s.reads(luis),
            await count("audit_log", "account_id = $1::uuid", ana),
            await count("action", "account_id = $1::uuid", ana),
        )

    early, before, counts, after, leave, again, runs, luis_rows, luis_reads, audit, actions_ = (
        scene(scenario)
    )
    assert early.pop("offboarding_code") == 1  # the code of the leave, past its own term
    assert sum(early.values()) == 0 and before == {**FULL, "google_connection": 0}
    assert after == EMPTY
    for table in ("review", "review_sentiment"):
        assert counts[table] == 2
    for table in (
        "profile_summary",
        "sentiment_analysis",
        "sentiment_history",
        "sentiment_usage",
        "location",
        "job",
    ):
        assert counts[table] == 1, table
    assert leave["purged_at"] is not None and leave["google_sub"] is None
    assert sum(again.values()) == 0
    # What each pass deleted is recorded: table names and counts, nothing else.
    assert runs[1] == counts and all(isinstance(v, int) for run in runs for v in run.values())
    assert ANA not in json.dumps(runs)
    # The other account, which manages the same Google profile, lost nothing.
    assert luis_rows == FULL and luis_reads[1] == 2
    # The record of published replies outlives the reviews.
    assert audit == 2 and actions_ == 1


def test_two_workers_running_the_pass_at_once_delete_everything_once():
    async def scenario(s: Scene):
        ana, _, at = await s.effective(ANA)
        s.now = at + 30 * DAY
        first, second = await asyncio.gather(
            s.wstore.run_retention(s.now, POLICY), s.wstore.run_retention(s.now, POLICY)
        )
        return first, second, await s.stored(ANA, ana)

    first, second, after = scene(scenario)
    assert after == EMPTY
    assert first.get("review", 0) + second.get("review", 0) == 2


def test_the_record_of_replies_is_deleted_12_months_after_the_leave_and_not_before():
    async def scenario(s: Scene):
        ana, _, _ = await s.effective(ANA)
        luis, _ = await s.furnished(LUIS)  # an active account
        # The floor of the database counts from the real clock: the leave is moved to
        # the real past, 12 months and a few days ago.
        left = (await owner("SELECT now() - interval '12 months' - interval '3 days' AS t"))[0]["t"]
        await owner(
            "UPDATE offboarding SET confirmed_at = $2, delete_at = $2, effective_at = $2"
            " WHERE account_id = $1::uuid",
            ana,
            left,
        )
        await owner("UPDATE account SET disconnected_at = $2 WHERE id = $1::uuid", ana, left)
        twelve = (await owner("SELECT $1::timestamptz + interval '12 months' AS t", left))[0]["t"]
        early = await s.retain(twelve - DAY)
        kept = (
            await count("audit_log", "account_id = $1::uuid", ana),
            await count("action", "account_id = $1::uuid", ana),
        )
        counts = await s.retain(twelve)
        gone = (
            await count("audit_log", "account_id = $1::uuid", ana),
            await count("action", "account_id = $1::uuid", ana),
        )
        again = await s.retain(twelve)
        active = (
            await count("audit_log", "account_id = $1::uuid", luis),
            await count("action", "account_id = $1::uuid", luis),
        )
        return early, kept, counts, gone, again, active

    early, kept, counts, gone, again, active = scene(scenario)
    assert early["audit_log"] == 0 and early["action"] == 0 and kept == (2, 1)
    assert counts["audit_log"] == 2 and counts["action"] == 1 and gone == (0, 0)
    assert again["audit_log"] == 0 and again["action"] == 0
    assert active == (2, 1)  # the audit trail of an active account is never deleted


def test_the_database_itself_refuses_to_delete_the_audit_trail_before_its_12_months():
    async def scenario(s: Scene):
        ana, _ = await s.furnished(ANA)  # active
        luis, _, _ = await s.effective(LUIS)  # on leave for 11 real months
        await owner(
            "UPDATE account SET disconnected_at = now() - interval '11 months' WHERE id = $1::uuid",
            luis,
        )
        # The worker asks for everything, with a clock far in the future.
        far = datetime(2040, 1, 1, tzinfo=UTC)
        counts = await s.wstore.run_retention(far, POLICY)
        direct = await _as(WORKER_URL, "DELETE FROM audit_log")
        conn = await asyncpg.connect(WORKER_URL)
        try:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('fichaitor.audit_cutoff', '2040-01-01', true)"
                )
                forced = await conn.execute("DELETE FROM audit_log")
        finally:
            await conn.close()
        return (
            counts["audit_log"],
            direct,
            forced,
            await count("audit_log", "account_id = $1::uuid", ana),
            await count("audit_log", "account_id = $1::uuid", luis),
        )

    deleted, direct, forced, ana_rows, luis_rows = scene(scenario)
    assert deleted == 0 and direct == [] and forced == "DELETE 0"
    assert ana_rows == 2 and luis_rows == 2


def test_sessions_removed_reviews_codes_and_the_waiting_list_go_at_their_term():
    async def scenario(s: Scene):
        ana, _ = await s.furnished(ANA)
        t = s.now
        for name, expires, revoked in (
            ("a" * 64, t - 30 * DAY, None),  # expired 30 days ago: goes
            ("b" * 64, t - 30 * DAY + timedelta(minutes=1), None),  # a minute short: stays
            ("c" * 64, t + DAY, t - 30 * DAY),  # closed 30 days ago: goes
            ("d" * 64, t + DAY, t - 29 * DAY),  # closed 29 days ago: stays
            ("e" * 64, t + DAY, None),  # live: stays
        ):
            await owner(
                "INSERT INTO session (account_id, token_hash, created_at, expires_at, revoked_at)"
                " VALUES ($1::uuid, $2, $3, $4, $5)",
                ana,
                name,
                t - 60 * DAY,
                expires,
                revoked,
            )
        reviews = await owner("SELECT id FROM review WHERE google_sub = $1 ORDER BY id", ANA)
        await owner(
            "UPDATE review SET removed_at = $2 WHERE id = $1", reviews[0]["id"], t - 30 * DAY
        )
        await owner(
            "UPDATE review SET removed_at = $2 WHERE id = $1",
            reviews[1]["id"],
            t - 30 * DAY + timedelta(minutes=1),
        )
        for contact, created in (
            ("pytest-viejo@example.com", "$1::timestamptz - interval '12 months'"),
            (
                "pytest-casi@example.com",
                "$1::timestamptz - interval '12 months' + interval '1 minute'",
            ),
            ("pytest-nuevo@example.com", "$1::timestamptz"),
        ):
            await owner(
                "INSERT INTO waitlist_entry (channel, contact, consent_version, created_at)"
                f" VALUES ('email', '{contact}', 'x', {created})",  # noqa: S608
                t,
            )
        session = await s.session(ana)
        for created in (t - 31 * DAY, t - DAY):
            await owner(
                "INSERT INTO offboarding_code (account_id, session_id, scope, code_hash,"
                " created_at, expires_at) SELECT $1::uuid, id, 'ficha', repeat('c', 64), $2,"
                " $2::timestamptz + interval '10 minutes' FROM session WHERE token_hash = $3",
                ana,
                created,
                session,
            )
        counts = await s.retain(t)
        sessions = {
            r["token_hash"][0]
            for r in await owner(
                "SELECT token_hash FROM session WHERE account_id = $1::uuid"
                " AND token_hash ~ '^(.)\\1+$'",
                ana,
            )
        }
        waitlist = {
            r["contact"]
            for r in await owner("SELECT contact FROM waitlist_entry WHERE contact LIKE 'pytest-%'")
        }
        left = (
            await count("review", "google_sub = $1", ANA),
            await count(
                "review_sentiment s JOIN review r ON r.id = s.review_id", "r.google_sub = $1", ANA
            ),
            await count("offboarding_code", "account_id = $1::uuid", ana),
            await count("profile_summary", "google_sub = $1", ANA),
        )
        # The launch: everything signed up before the notice goes in the next pass.
        launched = RetentionPolicy(waitlist_launched_at=t + timedelta(minutes=1))
        await s.wstore.run_retention(t + timedelta(minutes=2), launched)
        after_launch = await count("waitlist_entry", "contact LIKE 'pytest-%'")
        return (counts, sessions, waitlist, after_launch, *left)

    counts, sessions, waitlist, after_launch, reviews, classified, codes, summary = scene(scenario)
    assert sessions == {"b", "d", "e"} and counts["session"] == 2
    assert waitlist == {"pytest-casi@example.com", "pytest-nuevo@example.com"}
    assert counts["waitlist_entry"] == 1 and after_launch == 0
    assert counts["review"] == 1 and reviews == 1 and classified == 1
    assert counts["offboarding_code"] == 1 and codes == 1
    assert summary == 1 and counts["profile_summary"] == 0  # nothing else was touched


def test_a_profile_google_stopped_listing_is_deleted_with_its_data_after_30_days():
    async def scenario(s: Scene):
        ana, _ = await s.furnished(ANA)
        luis, _ = await s.furnished(LUIS)
        await s.store.sync_locations(ana, [], s.now)  # Google no longer lists it for Ana
        early = await s.retain(s.now + 30 * DAY - timedelta(minutes=1))
        before = await s.stored(ANA, ana)
        await s.retain(s.now + timedelta(minutes=1))
        after = await s.stored(ANA, ana)
        return early, before, after, await s.stored(LUIS, luis)

    early, before, after, luis_rows = scene(scenario)
    assert sum(early.values()) == 0 and before == FULL
    # The connection (the account is still connected) and the jobs stay.
    assert after == {**EMPTY, "google_connection": 2, "job": 1}
    assert luis_rows == FULL


# --- privileges --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        # The API still deletes nothing, old or new.
        "DELETE FROM offboarding WHERE false",
        "DELETE FROM offboarding_code WHERE false",
        "DELETE FROM account WHERE false",
        "DELETE FROM location WHERE false",
        "DELETE FROM session WHERE false",
        "DELETE FROM review WHERE false",
        "DELETE FROM action WHERE false",
        "DELETE FROM audit_log WHERE false",
        "DELETE FROM google_connection WHERE false",
        "DELETE FROM waitlist_entry WHERE false",
        "DELETE FROM job WHERE false",
        # It schedules and restores a leave; it cannot make one effective or done.
        "UPDATE offboarding SET effective_at = now() WHERE false",
        "UPDATE offboarding SET purged_at = now() WHERE false",
        "UPDATE offboarding SET revoke_state = 'revoked' WHERE false",
        "UPDATE offboarding SET delete_at = now() WHERE false",
        "UPDATE offboarding SET google_sub = 'x' WHERE false",
        "INSERT INTO offboarding (account_id, scope, confirmed_at, delete_at, effective_at)"
        " SELECT gen_random_uuid(), 'ficha', now(), now(), now() WHERE false",
        # A code: no new fingerprint, session or expiry once created; no mail state.
        "UPDATE offboarding_code SET code_hash = repeat('a', 64) WHERE false",
        "UPDATE offboarding_code SET expires_at = now() WHERE false",
        "UPDATE offboarding_code SET session_id = gen_random_uuid() WHERE false",
        "UPDATE offboarding_code SET mail_state = 'sent' WHERE false",
        "UPDATE account SET deleted_at = now() WHERE false",
        "SELECT * FROM retention_run LIMIT 1",
        "INSERT INTO retention_run (deleted) SELECT '{}'::jsonb WHERE false",
    ],
)
def test_the_api_role_cannot(sql):
    with pytest.raises(DENIED):
        run(_as(API_URL, sql))


@pytest.mark.parametrize(
    "sql",
    [
        # The worker makes a leave effective; it cannot create or restore one.
        "INSERT INTO offboarding (account_id, scope, confirmed_at, delete_at)"
        " SELECT gen_random_uuid(), 'ficha', now(), now() WHERE false",
        "UPDATE offboarding SET restored_at = now() WHERE false",
        "UPDATE offboarding SET delete_at = now() WHERE false",
        "UPDATE offboarding SET scope = 'cuenta' WHERE false",
        "DELETE FROM offboarding WHERE false",
        # Codes: it records whether the email went out, and nothing else.
        "SELECT code_hash FROM offboarding_code LIMIT 1",
        "UPDATE offboarding_code SET used_at = now() WHERE false",
        "INSERT INTO offboarding_code (account_id, session_id, scope, code_hash, created_at,"
        " expires_at) SELECT gen_random_uuid(), gen_random_uuid(), 'ficha', repeat('a', 64),"
        " now(), now() WHERE false",
        # Accounts, sessions and the waiting list: dates and deletions, never the data.
        "SELECT email FROM account LIMIT 1",
        "UPDATE account SET email = 'x' WHERE false",
        "UPDATE account SET google_sub = 'x' WHERE false",
        "DELETE FROM account WHERE false",
        "INSERT INTO account (google_sub, email) SELECT 'pytest-x', 'x' WHERE false",
        "SELECT token_hash FROM session LIMIT 1",
        "UPDATE session SET expires_at = now() WHERE false",
        "INSERT INTO session (account_id, token_hash, expires_at)"
        " SELECT gen_random_uuid(), repeat('a', 64), now() WHERE false",
        "SELECT contact FROM waitlist_entry LIMIT 1",
        "UPDATE waitlist_entry SET contact = 'x' WHERE false",
        "UPDATE location SET title = 'x' WHERE false",
        "UPDATE location SET account_id = gen_random_uuid() WHERE false",
        # The record of the passes: it adds a row and that is all.
        "SELECT * FROM retention_run LIMIT 1",
        "UPDATE retention_run SET deleted = '{}'::jsonb WHERE false",
        "DELETE FROM retention_run WHERE false",
        "INSERT INTO retention_run (deleted, at) SELECT '{}'::jsonb, now() WHERE false",
        # Drafts go with their review; the worker never touches the table.
        "DELETE FROM review_draft WHERE false",
        "DELETE FROM review_sentiment WHERE false",
        "DELETE FROM funnel_event WHERE false",
    ],
)
def test_the_worker_role_cannot(sql):
    with pytest.raises(DENIED):
        run(_as(WORKER_URL, sql))


def test_nobody_can_truncate_and_only_the_worker_deletes():
    async def held(role: str, table: str) -> set[str]:
        rows = await owner(
            "SELECT p FROM unnest(ARRAY['DELETE', 'TRUNCATE']) AS p"
            " WHERE has_table_privilege($1, $2, p)",
            role,
            table,
        )
        return {r["p"] for r in rows}

    async def scenario():
        tables = [
            r["tablename"]
            for r in await owner(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"
            )
        ]
        return {
            (role, table): await held(role, table)
            for role in ("fichaitor_api", "fichaitor_worker")
            for table in tables
        }

    held_by = run(scenario())
    deletes = {
        "google_connection",
        "profile_summary",
        "review",
        "sentiment_analysis",
        "sentiment_history",
        "sentiment_usage",
        "action",
        "job",
        "location",
        "session",
        "waitlist_entry",
        "offboarding_code",
        "audit_log",
    }
    for (role, table), privileges in held_by.items():
        assert "TRUNCATE" not in privileges, (role, table)
        if role == "fichaitor_api":
            assert privileges == set(), table
        elif table in deletes:
            assert privileges == {"DELETE"}, table
        elif table in (
            "account",
            "offboarding",
            "retention_run",
            "review_draft",
            "review_sentiment",
            "funnel_event",
        ):
            assert privileges == set(), table
