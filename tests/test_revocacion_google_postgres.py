"""Integration: the owner removes Fichaitor's access in their Google account. The worker
marks the connection revoked and deletes its token; 30 days later, if the owner has not
connected again, the retention pass deletes what was obtained from Google of every
profile of that Google user, as with an effective leave of the profile.

Skipped unless the test URLs are set. They must point to the TEST database (its name
ends in `_test`; tests/conftest.py stops the run otherwise). Google is the fake adapter:
nothing is called for real.
"""

import json
import uuid
from datetime import timedelta

import asyncpg
import pytest

from app.domain.jobs import PermanentJobError
from app.ports.google_profile import GrantRevoked, ReconnectRequired
from tests.test_offboarding_postgres import (
    ANA,
    API_URL,
    DAY,
    EMPTY,
    FULL,
    LUIS,
    NOW,
    OWNER_URL,
    WORKER_URL,
    Scene,
    cleanup,
    count,
    owner,
    run,
    scene,
)

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL and WORKER_URL), reason="sin base de datos de pruebas configurada"
)


@pytest.fixture(autouse=True)
def clean():
    run(cleanup())
    yield
    run(cleanup())


async def revoked_sync(s: Scene, sub: str, error: Exception | None = None) -> None:
    """A sync in which Google answers invalid_grant to the refresh."""
    s.google.refresh_error = error or GrantRevoked("permiso de Google revocado o caducado (400)")
    with pytest.raises(PermanentJobError):
        await s.sync(sub)
    s.google.refresh_error = None


async def connections(sub: str) -> list[asyncpg.Record]:
    return await owner(
        "SELECT revoked_at, refresh_token_enc IS NULL AS no_token FROM google_connection"
        " WHERE google_sub = $1 ORDER BY connected_at",
        sub,
    )


async def summary_status(sub: str) -> str:
    return (await owner("SELECT status FROM profile_summary WHERE google_sub = $1", sub))[0][
        "status"
    ]


def test_invalid_grant_marks_the_connection_revoked_and_deletes_only_the_tokens():
    async def scenario(s: Scene):
        ana, session = await s.furnished(ANA)  # two connections: the second never synced
        luis, _ = await s.furnished(LUIS)
        await revoked_sync(s, ANA)
        return (
            await connections(ANA),
            await s.stored(ANA, ana),
            await s.stored(LUIS, luis),
            await summary_status(ANA),
            await s.me(session),
            list(s.google.revoked),
        )

    rows, ana_rows, luis_rows, status, me, revoked = scene(scenario)
    # The connection that failed stays, revoked and without token; the one it replaced goes.
    assert [(r["revoked_at"], r["no_token"]) for r in rows] == [(NOW, True)]
    # Everything else is kept for now, and still shown: the panel asks to reconnect.
    assert ana_rows == {**FULL, "google_connection": 1}
    assert luis_rows == FULL
    assert status == "needs_reconnect"
    # The session lives on, and the account still counts as connected until the term.
    assert me is not None and me.connected is True and me.pending_deletion is None
    assert revoked == []  # nothing to revoke at Google: it already is


def test_a_401_or_a_passing_failure_revokes_nothing():
    async def scenario(s: Scene):
        await s.furnished(ANA)
        await revoked_sync(s, ANA, ReconnectRequired("Google no acepta el acceso (401)"))
        return await connections(ANA)

    rows = scene(scenario)
    assert [(r["revoked_at"], r["no_token"]) for r in rows] == [(None, False), (None, False)]


def test_what_came_from_google_is_deleted_30_days_after_the_revocation_and_not_before():
    async def scenario(s: Scene):
        ana, _ = await s.furnished(ANA)
        luis, _ = await s.furnished(LUIS)
        proposal = f"prop_{uuid.uuid4().hex[:16]}"
        await owner(
            "INSERT INTO proposal (id, account_id, type, source_ref, state)"
            " VALUES ($1, $2::uuid, 'review_reply', 'pytest:review', 'enviada')",
            proposal,
            ana,
        )
        await revoked_sync(s, ANA)
        at = s.now
        early = await s.retain(at + 30 * DAY - timedelta(minutes=1))
        before = await s.stored(ANA, ana)
        counts = await s.retain(at + 30 * DAY)
        after = await s.stored(ANA, ana)
        again = await s.retain(at + 31 * DAY)
        account = (await owner("SELECT * FROM account WHERE id = $1::uuid", ana))[0]
        runs = await owner("SELECT deleted::text AS deleted FROM retention_run ORDER BY id")
        return (
            early,
            before,
            counts,
            after,
            again,
            account,
            # The sessions are not closed: one opened before the term still works (the
            # first one has expired by now, 30 days later).
            await count("session", "account_id = $1::uuid AND revoked_at IS NULL", ana),
            await s.me(await s.session(ana)),
            await count("proposal", "id = $1", proposal),
            await s.stored(LUIS, luis),
            await count("audit_log", "account_id = $1::uuid", ana),
            await count("action", "account_id = $1::uuid", ana),
            [json.loads(r["deleted"]) for r in runs],
            at,
        )

    (
        early,
        before,
        counts,
        after,
        again,
        account,
        open_sessions,
        me,
        proposals,
        luis_rows,
        audit,
        actions_,
        runs,
        at,
    ) = scene(scenario)
    early.pop("job_finished", None)
    assert "revoked_identity" not in early and before == {**FULL, "google_connection": 1}
    # The same tables as an effective leave of the profile, for every profile of that
    # Google user, the revoked connection included.
    assert after == EMPTY
    assert counts["revoked_identity"] == 1
    for table in ("review", "review_sentiment"):
        assert counts[table] == 2, table
    for table in (
        "google_connection",
        "profile_summary",
        "sentiment_analysis",
        "sentiment_history",
        "sentiment_usage",
        "location",
        "job",
    ):
        assert counts[table] == 1, table
    assert "revoked_identity" not in again
    assert proposals == 0  # the conversation quotes reviews: it goes too
    # Without profiles, not connected; the session stays and the account lives on.
    assert account["disconnected_at"] == at + 30 * DAY and account["deleted_at"] is None
    assert account["selected_location_id"] is None and account["google_sub"] == ANA
    assert open_sessions == 1 and me is not None and me.connected is False
    # Nothing of the other account; the record of published replies is kept 12 months.
    assert luis_rows == FULL
    assert audit == 2 and actions_ == 1
    assert ANA not in json.dumps(runs)


def test_connecting_again_before_30_days_cancels_the_countdown():
    async def scenario(s: Scene):
        ana, _ = await s.furnished(ANA)
        await revoked_sync(s, ANA)
        at = s.now
        s.now = at + 10 * DAY
        await s.connect(ANA, "refresh-again")  # the OAuth callback stores a new one
        late = await s.retain(at + 40 * DAY)
        kept = await s.stored(ANA, ana)
        s.now = at + 40 * DAY
        await s.sync(ANA)  # works with the new connection; the revoked one goes
        return late, kept, await connections(ANA), await summary_status(ANA)

    late, kept, rows, status = scene(scenario)
    assert "revoked_identity" not in late
    assert kept["review"] == 2 and kept["profile_summary"] == 1
    assert [(r["revoked_at"], r["no_token"]) for r in rows] == [(None, False)]
    assert status == "ok"


def test_revoking_again_after_reconnecting_starts_the_countdown_again():
    async def scenario(s: Scene):
        ana, _ = await s.furnished(ANA)
        await revoked_sync(s, ANA)
        first = s.now
        s.now = first + 10 * DAY
        await s.connect(ANA, "refresh-again")
        s.now = first + 20 * DAY
        await revoked_sync(s, ANA)
        not_yet = await s.retain(first + 35 * DAY)
        kept = await s.stored(ANA, ana)
        done = await s.retain(first + 50 * DAY)
        return not_yet, kept, done, await s.stored(ANA, ana)

    not_yet, kept, done, after = scene(scenario)
    assert "revoked_identity" not in not_yet and kept["review"] == 2
    assert done["revoked_identity"] == 1 and after == EMPTY


def test_an_effective_leave_with_a_revoked_connection_has_nothing_to_revoke():
    async def scenario(s: Scene):
        ana, session = await s.furnished(ANA)
        await revoked_sync(s, ANA)
        s.now = await s.leave(session)
        await s.apply_leave(ana)
        leave = (await owner("SELECT revoke_state FROM offboarding"))[0]["revoke_state"]
        purged = await s.retain(s.now + 30 * DAY)
        return list(s.google.revoked), leave, purged, await s.stored(ANA, ana)

    revoked, state, purged, after = scene(scenario)
    assert revoked == [] and state == "none"
    assert after == EMPTY and purged["google_connection"] == 1


# --- the database rules -----------------------------------------------------------------


async def _try(url: str, sql: str, *args) -> str:
    conn = await asyncpg.connect(url)
    try:
        await conn.execute(sql, *args)
        return "ok"
    except asyncpg.PostgresError as exc:
        return type(exc).__name__
    finally:
        await conn.close()


def test_a_revoked_connection_never_gets_a_token_back_and_the_api_cannot_revoke():
    async def scenario(s: Scene):
        await s.furnished(ANA)
        await revoked_sync(s, ANA)
        await s.connect(LUIS)
        results = {
            # The worker: only "revoked, without token", and never undone.
            "worker_restores_token": await _try(
                WORKER_URL,
                "UPDATE google_connection SET refresh_token_enc = 'x', revoked_at = NULL"
                " WHERE google_sub = $1",
                ANA,
            ),
            "worker_revokes_keeping_token": await _try(
                WORKER_URL,
                "UPDATE google_connection SET revoked_at = now() WHERE google_sub = $1",
                LUIS,
            ),
            "worker_changes_locations": await _try(
                WORKER_URL,
                "UPDATE google_connection SET locations = '[]' WHERE google_sub = $1",
                LUIS,
            ),
            # The API: cannot read, change or create a revoked connection.
            "api_inserts_revoked": await _try(
                API_URL,
                "INSERT INTO google_connection"
                " (google_sub, email, refresh_token_enc, key_version, scopes, revoked_at)"
                " VALUES ($1, 'x@example.com', NULL, 1, '{}', now())",
                LUIS,
            ),
            "api_revokes": await _try(
                API_URL,
                "UPDATE google_connection SET revoked_at = now(), refresh_token_enc = NULL"
                " WHERE false",
            ),
            # A connection without token and not revoked does not exist.
            "owner_token_less": await _try(
                OWNER_URL,
                "INSERT INTO google_connection"
                " (google_sub, email, refresh_token_enc, key_version, scopes)"
                " VALUES ($1, 'x@example.com', NULL, 1, '{}')",
                LUIS,
            ),
        }
        return results, await connections(ANA), await connections(LUIS)

    results, ana, luis = scene(scenario)
    assert results == {
        "worker_restores_token": "CheckViolationError",
        "worker_revokes_keeping_token": "CheckViolationError",
        "worker_changes_locations": "InsufficientPrivilegeError",
        "api_inserts_revoked": "CheckViolationError",
        "api_revokes": "InsufficientPrivilegeError",
        "owner_token_less": "CheckViolationError",
    }
    assert [(r["revoked_at"], r["no_token"]) for r in ana] == [(NOW, True)]
    assert [(r["revoked_at"], r["no_token"]) for r in luis] == [(None, False)]
