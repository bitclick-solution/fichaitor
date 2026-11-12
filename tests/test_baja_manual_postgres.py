"""Integration: scripts/baja_manual.py, the tool support uses for a leave asked for by
email. It runs with the owner role of the database. By default the leave is scheduled
48 hours, with the email of the scheduled leave and its restore link, as in the panel
(B15 of docs/seguridad/revision-2026-11-12-c.md); with --inmediata it is effective at
once. The worker makes it effective, as with one asked for in the panel.

Skipped unless the test URLs are set. They must point to the TEST database (its name
ends in `_test`; tests/conftest.py stops the run otherwise). Google is the fake adapter.
"""

import importlib.util
import json
import uuid
from pathlib import Path

import asyncpg
import pytest

from app.domain.jobs import Job
from app.domain.offboarding import APPLY_KIND, SEND_MAIL_KIND, restore_token_hash
from app.ports.google_oauth import ProfileLocation
from tests.test_offboarding_postgres import (
    ANA,
    API_URL,
    DAY,
    EMPTY,
    FULL,
    LUIS,
    NOW,
    OWNER_URL,
    SHARED,
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

_spec = importlib.util.spec_from_file_location(
    "baja_manual", Path(__file__).resolve().parent.parent / "scripts" / "baja_manual.py"
)
baja_manual = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(baja_manual)

MOTIVO = "Ticket 1234: pide la baja por correo"
SECOND = ProfileLocation(
    "accounts/pytest-1", "locations/pytest-segunda", "Bar segundo", "place-pytest-8", True
)


async def clean_jobs() -> None:
    # The script enqueues the real kinds; these are the only ones of the test accounts.
    await owner(
        """
        DELETE FROM job WHERE kind = $1 AND payload->>'offboarding_id' IN (
            SELECT o.id::text FROM offboarding o JOIN account a ON a.id = o.account_id
            WHERE a.google_sub LIKE 'pytest-%' OR a.google_sub LIKE 'deleted:%')
        """,
        APPLY_KIND,
    )
    await owner(
        "DELETE FROM job WHERE kind = $1 AND payload->>'to' LIKE 'pytest-%'", SEND_MAIL_KIND
    )


@pytest.fixture(autouse=True)
def clean():
    run(clean_jobs())
    run(cleanup())
    yield
    run(clean_jobs())
    run(cleanup())


class Out:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, line: str = "") -> None:
        self.lines.append(line)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


async def cli(s: Scene, *args: str, url: str = OWNER_URL) -> tuple[int, str]:
    out = Out()
    code = await baja_manual.run(list(args), url=url, now=s.now, wait_seconds=0, out=out)
    return code, out.text


async def apply_job(s: Scene, run_at=None) -> None:
    """The worker takes the job the script enqueued."""
    rows = await owner(
        "SELECT payload::text AS payload, run_at, max_attempts FROM job WHERE kind = $1", APPLY_KIND
    )
    assert len(rows) == 1 and rows[0]["max_attempts"] == 5
    assert rows[0]["run_at"] == (run_at or s.now)
    await s.apply(Job("pytest-apply", APPLY_KIND, json.loads(rows[0]["payload"])))


async def mail_jobs() -> list[dict]:
    rows = await owner(
        "SELECT id::text AS id, payload::text AS payload, max_attempts FROM job"
        " WHERE kind = $1 AND payload->>'to' LIKE 'pytest-%'",
        SEND_MAIL_KIND,
    )
    return [
        {**json.loads(r["payload"]), "max_attempts": r["max_attempts"], "job_id": r["id"]}
        for r in rows
    ]


def args(email: str, scope: str = "ficha", *extra: str) -> list[str]:
    return ["--email", email, "--scope", scope, "--motivo", MOTIVO, *extra]


# --- without --confirmar nothing changes ------------------------------------------------


def test_by_default_it_only_shows_what_it_would_do():
    async def scenario(s: Scene):
        ana, _ = await s.furnished(ANA)
        before = await s.stored(ANA, ana)
        code, text = await cli(s, *args(f"{ANA}@example.com"))
        return (
            code,
            text,
            before,
            await s.stored(ANA, ana),
            await count("offboarding"),
            await count("job", "kind = $1", APPLY_KIND),
            await count("audit_log", "account_id = $1::uuid", ana),
            ana,
        )

    code, text, before, after, leaves, jobs, audit, ana = scene(scenario)
    assert code == 0 and before == after and leaves == 0 and jobs == 0 and audit == 2
    assert "Simulación" in text and "--confirmar" in text
    assert ana in text and "p***a@example.com" in text  # the account, the email masked
    assert f"{ANA}@example.com" not in text
    assert "refresh-" not in text  # never a token


# --- by default: scheduled 48 hours, with the email and its restore link (B15) --------


def test_by_default_it_is_scheduled_48_hours_with_the_email_and_its_restore_link():
    async def scenario(s: Scene):
        ana, session = await s.furnished(ANA)
        code, text = await cli(s, *args(f"{ANA}@example.com"), "--confirmar")
        leave = (await owner("SELECT * FROM offboarding WHERE account_id = $1::uuid", ana))[0]
        closed = await s.reads(ana)  # already closed while scheduled, as in the panel
        mails = await mail_jobs()
        apply_rows = await owner("SELECT run_at FROM job WHERE kind = $1", APPLY_KIND)
        # The worker sends the email of the panel, with the link to restore it.
        payload = {k: v for k, v in mails[0].items() if k not in ("max_attempts", "job_id")}
        await s.send_mail(Job(mails[0]["job_id"], SEND_MAIL_KIND, payload))
        # Before the date the worker does nothing with it.
        await s.apply(Job("pytest-apply", APPLY_KIND, {"offboarding_id": str(leave["id"])}))
        early = (await owner("SELECT * FROM offboarding WHERE id = $1", leave["id"]))[0]
        return code, text, leave, closed, mails, apply_rows, s.mailer.sent, early, session

    code, text, leave, closed, mails, apply_rows, sent, early, _ = scene(scenario)
    assert code == 0
    assert leave["reason"] == "email_request" and leave["scope"] == "ficha"
    assert leave["confirmed_at"] == NOW and leave["delete_at"] == NOW + 2 * DAY
    assert leave["google_sub"] == ANA and leave["restore_token_hash"] is not None
    assert closed == (None, 0, None)
    # One email, to the address of the account, with the token of the restore link.
    (mail,) = mails
    assert mail["template"] == "offboarding_scheduled" and mail["to"] == f"{ANA}@example.com"
    assert mail["scope"] == "ficha" and mail["max_attempts"] == 3
    assert restore_token_hash(mail["token"]) == leave["restore_token_hash"]
    assert [r["run_at"] for r in apply_rows] == [NOW + 2 * DAY]
    (email,) = sent
    assert "#restaurar=" in email.text and "Restaurar" in email.html
    assert early["effective_at"] is None and early["revoke_state"] is None
    assert early["restored_at"] is None
    # What it prints: scheduled, the date, the address masked. Never the token.
    assert "Programada" in text and "p***a@example.com" in text
    assert mail["token"] not in text and f"{ANA}@example.com" not in text


def test_the_owner_restores_it_from_the_link_and_nothing_is_revoked():
    async def scenario(s: Scene):
        ana, _ = await s.furnished(ANA)
        await cli(s, *args(f"{ANA}@example.com", "cuenta"), "--confirmar")
        (mail,) = await mail_jobs()
        s.now = NOW + DAY
        restored = await s.offboarding.restore_by_token(mail["token"])
        leave = (await owner("SELECT * FROM offboarding WHERE account_id = $1::uuid", ana))[0]
        # The job of the date finds it restored and leaves it alone.
        s.now = NOW + 2 * DAY
        await apply_job(s, run_at=NOW + 2 * DAY)
        after = (await owner("SELECT * FROM offboarding WHERE id = $1", leave["id"]))[0]
        account = (await owner("SELECT * FROM account WHERE id = $1::uuid", ana))[0]
        return ana, restored, after, account, list(s.google.revoked), await s.stored(ANA, ana)

    ana, restored, after, account, revoked, stored = scene(scenario)
    assert restored == ana
    assert after["restored_at"] == NOW + DAY and after["effective_at"] is None
    assert after["restore_token_hash"] is None and after["google_sub"] is None
    assert account["deleted_at"] is None and account["email"] == f"{ANA}@example.com"
    assert revoked == [] and stored == FULL


# --- --inmediata: the exceptional way, effective at once ---------------------------------


def test_inmediata_is_effective_at_once_without_code_48_hours_or_email():
    async def scenario(s: Scene):
        ana, session = await s.furnished(ANA)
        luis, _ = await s.furnished(LUIS)
        code, text = await cli(s, *args(f"{ANA.upper()}@Example.com"), "--inmediata", "--confirmar")
        leave = (await owner("SELECT * FROM offboarding WHERE account_id = $1::uuid", ana))[0]
        closed = await s.reads(ana)  # before the worker takes the job: already closed
        await apply_job(s)
        leave_after = (await owner("SELECT * FROM offboarding WHERE id = $1", leave["id"]))[0]
        audit = await owner(
            "SELECT * FROM audit_log WHERE account_id = $1::uuid AND event = $2",
            ana,
            "offboarding_email_request",
        )
        me = await s.me(session)
        purged = await s.retain(s.now + 30 * DAY)
        return (
            code,
            text,
            leave,
            closed,
            leave_after,
            sorted(s.google.revoked),
            audit,
            me,
            await s.stored(ANA, ana),
            await s.stored(LUIS, luis),
            purged,
        )

    code, text, leave, closed, after, revoked, audit, me, ana_rows, luis_rows, purged = scene(
        scenario
    )
    assert code == 0 and "INMEDIATA" in text
    assert leave["reason"] == "email_request" and leave["scope"] == "ficha"
    assert leave["confirmed_at"] == NOW and leave["delete_at"] == NOW
    assert leave["restore_token_hash"] is None and leave["google_sub"] == ANA
    assert closed == (None, 0, None)
    # The worker did what it does with a leave of the panel: revoke and delete the tokens.
    assert after["effective_at"] == NOW and after["revoke_state"] == "revoked"
    assert revoked == [f"refresh-new-{ANA}", f"refresh-old-{ANA}"]
    # The session of a leave of the profile stays; the account has no profiles.
    assert me is not None and me.connected is False and me.pending_deletion is None
    # The terms of deletion run as usual: 30 days later, nothing obtained from Google.
    assert ana_rows == EMPTY and purged["review"] == 2
    assert luis_rows == FULL
    # One audit line, by the owner, with the reason and no personal data.
    (line,) = audit
    assert line["text"] == MOTIVO and line["reason"] == "email_request"
    assert line["action_id"] == str(leave["id"]) and line["action_type"] == "offboarding_ficha"
    assert line["written_by"] == "fichaitor"
    assert str(leave["id"]) in text and "refresh-" not in text
    assert '"inmediata": true' in text


def test_inmediata_sends_no_email():
    async def scenario(s: Scene):
        await s.furnished(ANA)
        await cli(s, *args(f"{ANA}@example.com"), "--inmediata", "--confirmar")
        return await mail_jobs()

    assert scene(scenario) == []


def test_with_several_profiles_the_profile_must_be_named_and_belong_to_the_account():
    async def scenario(s: Scene):
        ana, _ = await s.furnished(ANA)
        await s.store.sync_locations(ana, [SHARED, SECOND], s.now)
        luis, _ = await s.furnished(LUIS)
        ana_locs = await owner(
            "SELECT id FROM location WHERE account_id = $1::uuid ORDER BY id", ana
        )
        luis_loc = (await owner("SELECT id FROM location WHERE account_id = $1::uuid", luis))[0]
        missing = await cli(s, *args(f"{ANA}@example.com"))
        foreign = await cli(s, *args(f"{ANA}@example.com", "ficha", "--location", luis_loc["id"]))
        wrong = await cli(s, *args(f"{ANA}@example.com", "ficha", "--location", "nope"))
        named = await cli(
            s,
            *args(f"{ANA}@example.com", "ficha", "--location", ana_locs[1]["id"]),
            "--confirmar",
        )
        return (
            missing,
            foreign,
            wrong,
            named,
            [r["id"] for r in ana_locs],
            await count("offboarding", "account_id = $1::uuid", ana),
        )

    missing, foreign, wrong, named, locs, leaves = scene(scenario)
    assert missing[0] == 1 and all(loc in missing[1] for loc in locs)
    assert foreign[0] == 1 and wrong[0] == 1
    assert named[0] == 0 and leaves == 1
    # Google grants access per user: all the profiles of the account are disconnected.
    assert "2 fichas" in named[1]


def test_a_leave_scheduled_in_the_panel_is_replaced_by_the_one_by_email():
    async def scenario(s: Scene):
        ana, session = await s.furnished(ANA)
        await s.leave(session)
        code, text = await cli(
            s, *args(f"{ANA}@example.com", "cuenta"), "--inmediata", "--confirmar"
        )
        leaves = await owner(
            "SELECT reason, restored_at, effective_at, scope, google_sub FROM offboarding"
            " WHERE account_id = $1::uuid ORDER BY confirmed_at, reason NULLS FIRST",
            ana,
        )
        sessions = await count("session", "account_id = $1::uuid AND revoked_at IS NULL", ana)
        await apply_job(s)
        account = (await owner("SELECT * FROM account WHERE id = $1::uuid", ana))[0]
        return code, text, leaves, sessions, account

    code, text, leaves, sessions, account = scene(scenario)
    assert code == 0 and "programada" in text
    panel, email = leaves
    assert panel["reason"] is None and panel["restored_at"] == NOW and panel["google_sub"] is None
    assert email["reason"] == "email_request" and email["scope"] == "cuenta"
    # Deleting the account closes every session at once, as in the panel.
    assert sessions == 0
    assert account["deleted_at"] == NOW and account["email"] == ""


@pytest.mark.parametrize(
    ("argv", "fragment"),
    [
        (["--email", "nadie@example.com", "--scope", "ficha", "--motivo", MOTIVO], "ninguna"),
        (args(f"{ANA}@example.com", "cuenta", "--location", "loc_abcd"), "--location"),
        (["--email", f"{ANA}@example.com", "--scope", "ficha", "--motivo", "ok"], "motivo"),
        (
            ["--email", f"{ANA}@example.com", "--scope", "ficha", "--motivo", "de x@y.com"],
            "correo",
        ),
    ],
)
def test_what_does_not_add_up_is_refused_without_changing_anything(argv, fragment):
    async def scenario(s: Scene):
        await s.furnished(ANA)
        code, text = await cli(s, *argv, "--confirmar")
        return code, text, await count("offboarding"), await count("job", "kind = $1", APPLY_KIND)

    code, text, leaves, jobs = scene(scenario)
    assert code != 0 and fragment in text and leaves == 0 and jobs == 0


def test_a_profile_already_disconnected_is_refused_and_a_deleted_account_is_not_found():
    async def scenario(s: Scene):
        await s.effective(ANA)  # its leave of the profile is effective
        again = await cli(s, *args(f"{ANA}@example.com"), "--confirmar")
        await s.furnished(LUIS)
        await cli(s, *args(f"{LUIS}@example.com", "cuenta"), "--inmediata", "--confirmar")
        await apply_job(s)
        gone = await cli(s, *args(f"{LUIS}@example.com", "cuenta"), "--confirmar")
        return again, gone

    again, gone = scene(scenario)
    assert again[0] == 1 and "desconectada" in again[1]
    assert gone[0] == 1 and "ninguna" in gone[1]


def test_only_the_owner_role_can_run_it():
    async def scenario(s: Scene):
        await s.furnished(ANA)
        results = []
        for url in (API_URL, WORKER_URL):
            results.append(await cli(s, *args(f"{ANA}@example.com"), "--confirmar", url=url))
        return results, await count("offboarding")

    results, leaves = scene(scenario)
    assert all(code == 2 and "propietario" in text for code, text in results)
    assert leaves == 0


# --- the database keeps the shortcut for the owner -------------------------------------


async def _try(url: str, sql: str, *args) -> str:
    conn = await asyncpg.connect(url)
    try:
        await conn.execute(sql, *args)
        return "ok"
    except asyncpg.PostgresError as exc:
        return type(exc).__name__
    finally:
        await conn.close()


def test_only_the_owner_writes_an_email_request_and_nobody_changes_it():
    async def scenario(s: Scene):
        ana, session = await s.furnished(ANA)
        await s.leave(session)
        leave_id = await s.leave_id(ana)
        return {
            # The API cannot write the reason: column privileges, and the trigger.
            "api_insert": await _try(
                API_URL,
                "INSERT INTO offboarding (account_id, scope, google_sub, confirmed_at, delete_at,"
                " reason) VALUES ($1::uuid, 'ficha', $2, now(), now(), 'email_request')",
                ana,
                ANA,
            ),
            "api_update": await _try(
                API_URL, "UPDATE offboarding SET reason = 'email_request' WHERE false"
            ),
            "worker_update": await _try(
                WORKER_URL, "UPDATE offboarding SET reason = 'email_request' WHERE false"
            ),
            # Without the reason, the floor of 47 hours still holds for everybody else.
            "api_short": await _try(
                API_URL,
                "INSERT INTO offboarding (account_id, scope, google_sub, confirmed_at, delete_at)"
                " VALUES ($1::uuid, 'ficha', $2, now(), now())",
                ana,
                ANA,
            ),
            "unknown_reason": await _try(
                OWNER_URL, "UPDATE offboarding SET reason = 'otra' WHERE id = $1", leave_id
            ),
        }, leave_id

    results, _ = scene(scenario)
    assert results == {
        "api_insert": "InsufficientPrivilegeError",
        "api_update": "InsufficientPrivilegeError",
        "worker_update": "InsufficientPrivilegeError",
        "api_short": "CheckViolationError",
        "unknown_reason": "CheckViolationError",
    }


def test_the_worker_never_makes_effective_a_short_leave_without_the_reason():
    # Written by the owner without the reason (a mistake, or an old row): it stays put.
    async def scenario(s: Scene):
        ana, _ = await s.furnished(ANA)
        leave_id = uuid.uuid4()
        await owner(
            "INSERT INTO offboarding (id, account_id, scope, google_sub, confirmed_at, delete_at)"
            " VALUES ($1, $2::uuid, 'ficha', $3, $4, $4)",
            leave_id,
            ana,
            ANA,
            s.now,
        )
        await s.apply(Job("pytest-apply", APPLY_KIND, {"offboarding_id": str(leave_id)}))
        return (await owner("SELECT effective_at FROM offboarding WHERE id = $1", leave_id))[0]

    assert scene(scenario)["effective_at"] is None


# --- I20: the URL in the one-off container -----------------------------------------------


def test_the_url_of_the_one_off_container_is_made_from_db_env():
    owner_url = baja_manual.owner_url
    assert owner_url({}) is None
    assert owner_url({"POSTGRES_PASSWORD": "x"}) is None  # without the host of the Compose
    assert owner_url({"FICHAITOR_OWNER_DATABASE_URL": "postgresql://a"}) == "postgresql://a"
    assert (
        owner_url(
            {
                "FICHAITOR_OWNER_DB_HOST": "fichaitor-db",
                "POSTGRES_PASSWORD": "p@ss/wo:rd",
                "POSTGRES_USER": "fichaitor",
                "POSTGRES_DB": "fichaitor",
            }
        )
        == "postgresql://fichaitor:p%40ss%2Fwo%3Ard@fichaitor-db:5432/fichaitor"
    )
