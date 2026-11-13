"""Integration: db/schema_admin.sql, the database side of the backoffice (paso 5 of
docs/admin-facturacion.md, section 6).

The role fichaitor_admin reads only what the backoffice needs: no Google tokens, no
email addresses, no waiting list (only anonymous totals with the cells under 5
suppressed) and no job payloads. Its audit log is insert-only: nobody reads it, changes
it or deletes it. The leave asked for by email is a SECURITY DEFINER function that does
the same transaction as scripts/baja_manual.py.

Skipped unless the test URLs are set. They must point to the TEST database (its name
ends in `_test`; tests/conftest.py stops the run otherwise). scripts/preparar_bd_pruebas.sh
applies db/schema_admin.sql and db/dev-init.sql creates the test user fichaitor_admin_dev
(password 'dev'); the fixture checks both and applies the schema once more (idempotent):

    FICHAITOR_TEST_DB=fichaitor_adm_test bash scripts/preparar_bd_pruebas.sh
    B=127.0.0.1:5433/fichaitor_adm_test
    FICHAITOR_TEST_OWNER_URL=postgresql://fichaitor:dev@$B \
    FICHAITOR_TEST_DATABASE_URL=postgresql://fichaitor_api_dev:dev@$B \
    FICHAITOR_TEST_WORKER_URL=postgresql://fichaitor_worker_dev:dev@$B \
    FICHAITOR_TEST_CONVERSATION_URL=postgresql://fichaitor_conversation_dev:dev@$B \
    uv run pytest -q tests/test_admin_schema_postgres.py

Every statement expected to fail runs inside a transaction that is rolled back, or
carries WHERE false.
"""

import asyncio
import hashlib
import importlib.util
import json
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "db" / "schema_admin.sql"

API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")
WORKER_URL = os.environ.get("FICHAITOR_TEST_WORKER_URL")
CONVERSATION_URL = os.environ.get("FICHAITOR_TEST_CONVERSATION_URL")

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL and WORKER_URL and CONVERSATION_URL),
    reason="sin base de datos de pruebas configurada",
)

ADMIN_USER, ADMIN_PASSWORD = "fichaitor_admin_dev", "dev"
MOTIVO = "Ticket 4321: pide la baja por correo"
SUB = "pytest-adm-"
HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2hoYXNo"


def _admin_url() -> str:
    """The URL of fichaitor_admin_dev: the host, port and database of the owner URL."""
    parts = urlsplit(OWNER_URL or "")
    netloc = f"{ADMIN_USER}:{ADMIN_PASSWORD}@{parts.hostname}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


ADMIN_URL = os.environ.get("FICHAITOR_TEST_ADMIN_URL") or _admin_url()


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


async def admin(sql: str, *args):
    return await _as(ADMIN_URL, sql, *args)


async def attempt(url: str, sql: str, *args) -> str:
    """'ok' or the name of the error. Always inside a transaction that is rolled back."""
    conn = await asyncpg.connect(url)
    try:
        tr = conn.transaction()
        await tr.start()
        try:
            await conn.fetch(sql, *args)
            return "ok"
        except asyncpg.PostgresError as exc:
            return type(exc).__name__
        finally:
            await tr.rollback()
    finally:
        await conn.close()


DENIED = "InsufficientPrivilegeError"


# --- the schema, applied by the fixture ---------------------------------------------


async def _check_schema() -> None:
    """scripts/preparar_bd_pruebas.sh applies db/schema_admin.sql (the last one, after
    db/schema_billing.sql) and db/dev-init.sql creates fichaitor_admin_dev. Applied once
    more here: it must be idempotent, with the test user already a member (the M6 revoke
    of TEMPORARY reaches the members of the role too)."""
    conn = await asyncpg.connect(OWNER_URL)
    try:
        present = await conn.fetchval(
            "SELECT to_regclass('public.admin_user') IS NOT NULL"
            " AND EXISTS (SELECT FROM pg_roles WHERE rolname = $1)",
            ADMIN_USER,
        )
        assert present, "falta db/schema_admin.sql: ejecuta scripts/preparar_bd_pruebas.sh"
        await conn.execute(SCHEMA.read_text(encoding="utf-8"))
    finally:
        await conn.close()


@pytest.fixture(scope="module", autouse=True)
def schema():
    run(_check_schema())


async def cleanup() -> None:
    mine = f"(SELECT id FROM account WHERE google_sub LIKE '{SUB}%' OR email LIKE '{SUB}%')"
    leaves = f"(SELECT id::text FROM offboarding WHERE account_id IN {mine})"
    await owner(
        "DELETE FROM job WHERE kind = 'apply_offboarding'"
        f" AND payload->>'offboarding_id' IN {leaves}"  # noqa: S608
    )
    await owner(f"DELETE FROM job WHERE kind = 'send_mail' AND payload->>'to' LIKE '{SUB}%'")  # noqa: S608
    for table in ("audit_log", "conversation_lane", "proposal", "conversation", "link_code"):
        await owner(f"DELETE FROM {table} WHERE account_id IN {mine}")  # noqa: S608
    await owner(f"DELETE FROM channel_identity WHERE account_id IN {mine}")  # noqa: S608
    await owner(f"DELETE FROM offboarding WHERE account_id IN {mine}")  # noqa: S608
    await owner(f"DELETE FROM session WHERE account_id IN {mine}")  # noqa: S608
    await owner(f"DELETE FROM location_activation_log WHERE account_id IN {mine}")  # noqa: S608
    await owner(f"UPDATE account SET selected_location_id = NULL WHERE id IN {mine}")  # noqa: S608
    await owner(f"DELETE FROM location WHERE account_id IN {mine}")  # noqa: S608
    await owner(f"DELETE FROM account WHERE id IN {mine}")  # noqa: S608
    await owner(f"DELETE FROM waitlist_entry WHERE contact LIKE '{SUB}%'")  # noqa: S608
    # The audit log of the backoffice cannot be deleted (that is the point): the rows of
    # the test users stay. The users go (the log has no foreign key to them).
    await owner("DELETE FROM admin_user WHERE username LIKE 'pytest%'")


@pytest.fixture(autouse=True)
def clean():
    run(cleanup())
    yield
    run(cleanup())


# --- helpers: a backoffice user with a session, an account with everything --------------


def _hex() -> str:
    return secrets.token_hex(32)


async def backoffice(role: str, *, fresh: bool = True) -> tuple[str, str]:
    """A user of the backoffice with a session whose TOTP was confirmed now (or 10
    minutes ago). Written by the owner, as scripts/admin_usuario.py would leave it after
    the enrollment. Returns (user id, session token hash)."""
    name = f"pytest{uuid.uuid4().hex[:10]}"
    user = await owner(
        "INSERT INTO admin_user (username, role, password_hash, totp_secret_enc, must_enroll)"
        " VALUES ($1, $2, $3, 'cifrado', false) RETURNING id::text",
        name,
        role,
        HASH,
    )
    token = _hex()
    verified = "now()" if fresh else "now() - interval '10 minutes'"
    await owner(
        "INSERT INTO admin_session (token_hash, admin_user_id, created_at, last_seen_at,"
        f" expires_at, totp_verified_at) VALUES ($1, $2::uuid, now(), now(),"
        f" now() + interval '8 hours', {verified})",
        token,
        user[0]["id"],
    )
    return user[0]["id"], token


async def furnish(sub: str, *, locations: int = 1) -> dict:
    """An account with what a leave touches: profiles, an open and a finished proposal,
    the lane holding the open one, a session, a linked chat and an unused link code."""
    account = (
        await owner(
            "INSERT INTO account (google_sub, email) VALUES ($1, $2) RETURNING id::text",
            sub,
            f"{sub}@example.com",
        )
    )[0]["id"]
    locs = []
    for i in range(locations):
        row = await owner(
            "INSERT INTO location (account_id, gbp_account, gbp_location, title)"
            " VALUES ($1::uuid, 'accounts/pytest', $2, $3) RETURNING id",
            account,
            f"locations/{sub}-{i}",
            f"Bar {sub} {i}",
        )
        locs.append(row[0]["id"])
    await owner("INSERT INTO conversation (account_id) VALUES ($1::uuid)", account)
    open_id, done_id = f"prop_{secrets.token_hex(8)}", f"prop_{secrets.token_hex(8)}"
    await owner(
        "INSERT INTO proposal (id, account_id, type, source_ref, state, location_id,"
        " step_token, message_refs) VALUES"
        " ($1, $3::uuid, 'review_reply', 'r-open', 'enviada', $4, 'abc123', '{\"x\": 1}'),"
        " ($2, $3::uuid, 'review_reply', 'r-done', 'caducada', $4, '', '{}')",
        open_id,
        done_id,
        account,
        locs[0],
    )
    await owner(
        "INSERT INTO conversation_lane (account_id, location_id, active_proposal_id)"
        " VALUES ($1::uuid, $2, $3)",
        account,
        locs[0],
        open_id,
    )
    await owner(
        "INSERT INTO session (account_id, token_hash, expires_at)"
        " VALUES ($1::uuid, $2, now() + interval '30 days')",
        account,
        _hex(),
    )
    await owner(
        "INSERT INTO channel_identity (account_id, channel, address)"
        " VALUES ($1::uuid, 'telegram', $2)",
        account,
        str(secrets.randbelow(10**12)),
    )
    await owner(
        "INSERT INTO link_code (code_hash, account_id, expires_at)"
        " VALUES ($1, $2::uuid, now() + interval '10 minutes')",
        _hex(),
        account,
    )
    return {"id": account, "sub": sub, "email": f"{sub}@example.com", "locations": locs}


def _load_baja_manual():
    spec = importlib.util.spec_from_file_location(
        "baja_manual", ROOT / "scripts" / "baja_manual.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def snapshot(account: dict) -> dict:
    """What a leave by email left in the database, without what changes on every run
    (ids, the clock, the random token): the shape both ways must share."""
    (leave,) = await owner(
        "SELECT * FROM offboarding WHERE account_id = $1::uuid AND restored_at IS NULL",
        account["id"],
    )
    leave_id = str(leave["id"])
    proposals = await owner(
        "SELECT source_ref, state, outcome_reason, step_token, message_refs::text AS refs"
        " FROM proposal WHERE account_id = $1::uuid ORDER BY source_ref",
        account["id"],
    )
    lanes = await owner(
        "SELECT active_proposal_id FROM conversation_lane WHERE account_id = $1::uuid",
        account["id"],
    )
    sessions = await owner(
        "SELECT revoked_at IS NOT NULL AS closed FROM session WHERE account_id = $1::uuid",
        account["id"],
    )
    chats = await owner(
        "SELECT unlinked_at IS NOT NULL AS unlinked FROM channel_identity"
        " WHERE account_id = $1::uuid",
        account["id"],
    )
    codes = await owner(
        "SELECT used_at IS NOT NULL AS used FROM link_code WHERE account_id = $1::uuid",
        account["id"],
    )
    audit = await owner(
        "SELECT event, action_id, action_type, location, review_id, gbp_review_id, text,"
        " confirmed_at, reason FROM audit_log WHERE account_id = $1::uuid",
        account["id"],
    )
    mails = await owner(
        "SELECT payload::text AS payload, max_attempts, run_at, idempotency_key FROM job"
        " WHERE kind = 'send_mail' AND payload->>'to' = $1",
        account["email"],
    )
    applies = await owner(
        "SELECT payload::text AS payload, max_attempts, run_at, idempotency_key FROM job"
        " WHERE kind = 'apply_offboarding' AND payload->>'offboarding_id' = $1",
        leave_id,
    )
    location = {loc: f"loc{i}" for i, loc in enumerate(account["locations"])}
    mail_shape = []
    for mail in mails:
        payload = json.loads(mail["payload"])
        mail_shape.append(
            {
                "keys": sorted(payload),
                "template": payload["template"],
                "to": payload["to"] == account["email"],
                "scope": payload["scope"],
                "delete_at": datetime.fromisoformat(payload["delete_at"]) == leave["delete_at"],
                "token": hashlib.sha256(payload["token"].encode()).hexdigest()
                == leave["restore_token_hash"],
                "max_attempts": mail["max_attempts"],
                "key": mail["idempotency_key"],
            }
        )
    return {
        "leave": {
            "scope": leave["scope"],
            "reason": leave["reason"],
            "grace": leave["delete_at"] - leave["confirmed_at"],
            "google_sub": leave["google_sub"] == account["sub"],
            "restore_token": leave["restore_token_hash"] is not None,
            "effective_at": leave["effective_at"],
            "revoke_state": leave["revoke_state"],
        },
        "proposals": [
            {
                "source_ref": p["source_ref"],
                "state": p["state"],
                "outcome_reason": p["outcome_reason"],
                "token_changed": p["step_token"] != "abc123" and p["state"] == "cancelada",
                "refs": p["refs"],
            }
            for p in proposals
        ],
        "lanes": [lane["active_proposal_id"] for lane in lanes],
        "sessions_closed": [s["closed"] for s in sessions],
        "chats_unlinked": [c["unlinked"] for c in chats],
        "codes_used": [c["used"] for c in codes],
        "audit": [
            {
                **{k: a[k] for k in ("event", "action_type", "review_id", "gbp_review_id")},
                "text": a["text"],
                "reason": a["reason"],
                "action_id": a["action_id"] == leave_id,
                "location": location.get(a["location"], a["location"]),
                "confirmed_at": a["confirmed_at"] == leave["confirmed_at"],
            }
            for a in audit
        ],
        "mails": mail_shape,
        "applies": [
            {
                "payload": json.loads(j["payload"]) == {"offboarding_id": leave_id},
                "max_attempts": j["max_attempts"],
                "run_at": j["run_at"] == leave["delete_at"],
                "key": j["idempotency_key"] == f"apply_offboarding:{leave_id}",
            }
            for j in applies
        ],
    }


async def checked(token: str, account: dict) -> bool:
    """The backoffice compares the From: of the pasted message with the account."""
    rows = await admin(
        "SELECT admin_email_matches($1, $2::uuid, $3, $4) AS ok",
        token,
        account["id"],
        account["email"].upper(),
        _hex(),
    )
    return rows[0]["ok"]


# --- the role never reads emails, connections, the waiting list or payloads -----------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT email FROM account WHERE false",
        "SELECT google_sub FROM account WHERE false",
        "SELECT * FROM account WHERE false",
        "SELECT title FROM location WHERE false",
        "SELECT * FROM google_connection WHERE false",
        "SELECT refresh_token_enc FROM google_connection WHERE false",
        "SELECT * FROM waitlist_entry WHERE false",
        "SELECT count(*) FROM waitlist_entry",
        "SELECT payload FROM job WHERE false",
        "SELECT id, kind FROM job WHERE false",
        "SELECT * FROM session WHERE false",
        "SELECT address FROM channel_identity WHERE false",
        "SELECT * FROM channel_message WHERE false",
        "SELECT * FROM review WHERE false",
        "SELECT * FROM review_draft WHERE false",
        "SELECT facts FROM profile_summary WHERE false",
        "SELECT * FROM sentiment_analysis WHERE false",
        "SELECT * FROM proposal WHERE false",
        "SELECT text FROM action WHERE false",
        "SELECT * FROM audit_log WHERE false",
        "SELECT google_sub FROM offboarding WHERE false",
        "SELECT restore_token_hash FROM offboarding WHERE false",
        "SELECT * FROM offboarding_code WHERE false",
        "SELECT * FROM link_code WHERE false",
        "SELECT * FROM funnel_event WHERE false",
        "SELECT * FROM usage_counter WHERE false",
    ],
)
def test_the_role_reads_no_personal_data_tokens_waitlist_or_payloads(sql):
    assert run(attempt(ADMIN_URL, sql)) == DENIED


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id, created_at, deleted_at, disconnected_at FROM account WHERE false",
        "SELECT id, account_id, active_since, plan, deactivated_at, removed_at, verified"
        " FROM location WHERE false",
        "SELECT * FROM plan_entitlement WHERE false",
        "SELECT * FROM location_activation_log WHERE false",
        "SELECT id, account_id, scope, confirmed_at, delete_at, restored_at, effective_at,"
        " revoke_state, purged_at, reason FROM offboarding WHERE false",
    ],
)
def test_the_role_reads_the_columns_section_6_6_grants(sql):
    assert run(attempt(ADMIN_URL, sql)) == "ok"


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE account SET email = '' WHERE false",
        "UPDATE location SET active_since = NULL WHERE false",
        "INSERT INTO plan_entitlement (account_id, plan, quantity, starts_at, source)"
        " VALUES (gen_random_uuid(), 'pro', 1, now(), 'manual')",
        "DELETE FROM offboarding WHERE false",
        "INSERT INTO offboarding (account_id, scope, confirmed_at, delete_at)"
        " VALUES (gen_random_uuid(), 'ficha', now(), now())",
        "INSERT INTO job (kind) VALUES ('send_mail')",
        "INSERT INTO audit_log (event, account_id, action_id, action_type, location, review_id,"
        " gbp_review_id, text, confirmed_at) VALUES ('reply_confirmed', gen_random_uuid(),"
        " '', '', '', '', '', '', now())",
    ],
)
def test_the_role_writes_nothing_of_the_product_directly(sql):
    assert run(attempt(ADMIN_URL, sql)) == DENIED


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT role FROM admin_user WHERE false",
        "SELECT active FROM admin_user WHERE false",
        "SELECT password_hash FROM admin_user WHERE false",
        "UPDATE admin_user SET role = 'propietario' WHERE false",
        "UPDATE admin_user SET active = true WHERE false",
        "INSERT INTO admin_user (username, role) VALUES ('pytest-intruso', 'propietario')",
        "SELECT * FROM admin_session WHERE false",
        "UPDATE admin_session SET totp_verified_at = now() WHERE false",
        "SELECT * FROM admin_recovery_code WHERE false",
        "SELECT * FROM admin_enrollment WHERE false",
    ],
)
def test_users_roles_and_sessions_of_the_backoffice_only_by_function(sql):
    assert run(attempt(ADMIN_URL, sql)) == DENIED


def test_the_account_search_masks_the_email_and_the_names_need_a_ticket():
    async def scenario():
        account = await furnish(f"{SUB}busca")
        _, token = await backoffice("soporte")
        found = await admin(
            "SELECT * FROM admin_find_accounts($1, $2)", token, account["email"].upper()
        )
        by_location = await admin(
            "SELECT account_id::text AS id FROM admin_find_accounts($1, $2)",
            token,
            account["locations"][0],
        )
        no_ticket = await attempt(
            ADMIN_URL, "SELECT * FROM admin_account_detail($1, $2::uuid, '')", token, account["id"]
        )
        detail = await admin(
            "SELECT * FROM admin_account_detail($1, $2::uuid, 'Ticket 77')", token, account["id"]
        )
        return account, found, by_location, no_ticket, detail

    account, found, by_location, no_ticket, detail = run(scenario())
    (row,) = found
    assert str(row["account_id"]) == account["id"]
    assert row["email_masked"] == "p***a@example.com"  # pytest-adm-busca
    assert account["email"] not in json.dumps([dict(r) for r in found], default=str)
    assert [r["id"] for r in by_location] == [account["id"]]
    assert no_ticket == "CheckViolationError"
    assert [r["title"] for r in detail] == [f"Bar {account['sub']} 0"]


# --- the waiting list: anonymous totals, cells under 5 suppressed -------------------------


def test_the_waitlist_totals_suppress_the_cells_under_5():
    async def scenario():
        week = datetime(2027, 3, 3, 10, 0, tzinfo=UTC)  # a Wednesday
        for i in range(5):
            await owner(
                "INSERT INTO waitlist_entry (channel, contact, score, source, consent_version,"
                " created_at) VALUES ('email', $1, 72, 'pytest-cinco', 'v1', $2)",
                f"{SUB}cinco-{i}@example.com",
                week + timedelta(hours=i),
            )
        for i in range(4):
            await owner(
                "INSERT INTO waitlist_entry (channel, contact, score, source, consent_version,"
                " created_at) VALUES ('email', $1, 72, 'pytest-cuatro', 'v1', $2)",
                f"{SUB}cuatro-{i}@example.com",
                week + timedelta(hours=i),
            )
        _, owner_token = await backoffice("propietario")
        _, support = await backoffice("soporte")
        totals = await admin("SELECT * FROM admin_waitlist_totals($1)", owner_token)
        denied = await attempt(ADMIN_URL, "SELECT * FROM admin_waitlist_totals($1)", support)
        return totals, denied

    totals, denied = run(scenario())
    mine = [dict(t) for t in totals if t["source"].startswith("pytest-")]
    assert mine == [
        {
            "week": datetime(2027, 3, 1).date(),
            "channel": "email",
            "score_band": "70-89",
            "source": "pytest-cinco",
            "people": 5,
        }
    ]
    assert all(t["people"] >= 5 for t in totals)
    assert set(totals[0].keys()) == {"week", "channel", "score_band", "source", "people"}
    assert denied == DENIED


# --- the leave by email: the same as scripts/baja_manual.py ------------------------------


@pytest.mark.parametrize(
    ("scope", "named", "locations"),
    [("ficha", False, 1), ("ficha", True, 2), ("cuenta", False, 1)],
)
def test_the_leave_by_function_is_the_same_as_the_one_of_baja_manual(scope, named, locations):
    baja_manual = _load_baja_manual()

    async def scenario():
        by_script = await furnish(f"{SUB}script-{scope}-{locations}", locations=locations)
        by_function = await furnish(f"{SUB}funcion-{scope}-{locations}", locations=locations)
        argv = ["--email", by_script["email"], "--scope", scope, "--motivo", MOTIVO]
        if named:
            argv += ["--location", by_script["locations"][1]]
        out: list[str] = []
        code = await baja_manual.run(
            [*argv, "--confirmar"],
            url=OWNER_URL,
            now=datetime.now(UTC),
            wait_seconds=0,
            out=out.append,
        )
        assert code == 0, out
        _, token = await backoffice("soporte")
        assert await checked(token, by_function)
        result = await admin(
            "SELECT offboarding_id::text AS id, delete_at FROM"
            " fichaitor_email_offboarding($1, $2::uuid, $3, $4, $5)",
            token,
            by_function["id"],
            scope,
            by_function["locations"][1] if named else None,
            MOTIVO,
        )
        return await snapshot(by_script), await snapshot(by_function), result

    script, function, result = run(scenario())
    assert function == script
    # And what both left is a leave by email scheduled 48 hours with its restore link.
    assert script["leave"]["reason"] == "email_request"
    assert script["leave"]["grace"] == timedelta(hours=48)
    assert script["leave"]["restore_token"] and script["leave"]["google_sub"]
    assert [m["template"] for m in script["mails"]] == ["offboarding_scheduled"]
    assert script["mails"][0]["token"] and script["mails"][0]["delete_at"]
    assert script["applies"] == [{"payload": True, "max_attempts": 5, "run_at": True, "key": True}]
    assert [(p["state"], p["outcome_reason"]) for p in script["proposals"]] == [
        ("caducada", None),
        ("cancelada", "offboarding"),
    ]
    assert script["lanes"] == [None]
    assert script["audit"][0]["event"] == "offboarding_email_request"
    assert script["audit"][0]["text"] == MOTIVO
    closed = scope == "cuenta"
    assert script["sessions_closed"] == [closed]
    assert script["chats_unlinked"] == [closed] and script["codes_used"] == [closed]
    (row,) = result
    assert row["id"] and row["delete_at"]


def test_the_leave_by_function_writes_both_audits_without_personal_data():
    async def scenario():
        account = await furnish(f"{SUB}auditada")
        user, token = await backoffice("propietario")
        await checked(token, account)
        await admin(
            "SELECT * FROM fichaitor_email_offboarding($1, $2::uuid, 'ficha', NULL, $3)",
            token,
            account["id"],
            MOTIVO,
        )
        product = await owner(
            "SELECT written_by FROM audit_log WHERE account_id = $1::uuid", account["id"]
        )
        backoffice_rows = await owner(
            "SELECT action, role, written_by, ticket_ref, target, detail::text AS detail"
            " FROM admin_audit_log WHERE admin_user = $1::uuid ORDER BY id",
            user,
        )
        return account, product, backoffice_rows

    account, product, rows = run(scenario())
    assert [r["written_by"] for r in product] == [ADMIN_USER]
    assert [r["action"] for r in rows] == ["offboarding_email_checked", "offboarding_email"]
    assert all(r["role"] == "propietario" and r["written_by"] == ADMIN_USER for r in rows)
    assert rows[1]["ticket_ref"] == MOTIVO
    checked_detail = json.loads(rows[0]["detail"])
    assert checked_detail["match"] is True and checked_detail["domain"] == "example.com"
    everything = json.dumps([dict(r) for r in rows], default=str)
    assert account["email"] not in everything and account["email"].upper() not in everything


@pytest.mark.parametrize(
    ("role", "immediate", "check", "motivo", "expected"),
    [
        ("finanzas", False, True, MOTIVO, DENIED),  # not their row of the matrix
        ("soporte", True, True, MOTIVO, DENIED),  # immediate: only the owner of the base
        ("soporte", False, False, MOTIVO, "CheckViolationError"),  # without the DMARC check
        ("soporte", False, True, "de pepe@example.com", "CheckViolationError"),
        ("soporte", False, True, "ok", "CheckViolationError"),
    ],
)
def test_the_leave_by_function_refuses_what_does_not_add_up(
    role, immediate, check, motivo, expected
):
    async def scenario():
        account = await furnish(f"{SUB}rechazo")
        _, token = await backoffice(role)
        if check:
            await owner(
                "UPDATE admin_user SET role = 'soporte' WHERE id = (SELECT admin_user_id"
                " FROM admin_session WHERE token_hash = $1)",
                token,
            )
            await checked(token, account)
            await owner(
                "UPDATE admin_user SET role = $2 WHERE id = (SELECT admin_user_id"
                " FROM admin_session WHERE token_hash = $1)",
                token,
                role,
            )
        outcome = await attempt(
            ADMIN_URL,
            "SELECT * FROM fichaitor_email_offboarding($1, $2::uuid, 'ficha', NULL, $3, $4)",
            token,
            account["id"],
            motivo,
            immediate,
        )
        leaves = await owner(
            "SELECT count(*) AS n FROM offboarding WHERE account_id = $1::uuid", account["id"]
        )
        return outcome, leaves[0]["n"]

    outcome, leaves = run(scenario())
    assert outcome == expected and leaves == 0


def test_the_leave_needs_a_fresh_totp_and_a_matching_from():
    async def scenario():
        account = await furnish(f"{SUB}totp")
        _, stale = await backoffice("soporte", fresh=False)
        stale_try = await attempt(
            ADMIN_URL,
            "SELECT * FROM fichaitor_email_offboarding($1, $2::uuid, 'ficha', NULL, $3)",
            stale,
            account["id"],
            MOTIVO,
        )
        _, token = await backoffice("soporte")
        wrong = await admin(
            "SELECT admin_email_matches($1, $2::uuid, 'otro@example.com', $3) AS ok",
            token,
            account["id"],
            _hex(),
        )
        after_wrong = await attempt(
            ADMIN_URL,
            "SELECT * FROM fichaitor_email_offboarding($1, $2::uuid, 'ficha', NULL, $3)",
            token,
            account["id"],
            MOTIVO,
        )
        return stale_try, wrong[0]["ok"], after_wrong

    stale_try, wrong, after_wrong = run(scenario())
    assert stale_try == DENIED
    assert wrong is False and after_wrong == "CheckViolationError"


def test_without_a_session_only_the_owner_of_the_base_calls_the_leave():
    async def scenario():
        account = await furnish(f"{SUB}sinsesion")
        results = {}
        for name, url in (("admin", ADMIN_URL), ("api", API_URL), ("worker", WORKER_URL)):
            results[name] = await attempt(
                url,
                "SELECT * FROM fichaitor_email_offboarding(NULL, $1::uuid, 'ficha', NULL, $2)",
                account["id"],
                MOTIVO,
            )
        results["owner_immediate"] = await attempt(
            OWNER_URL,
            "SELECT * FROM fichaitor_email_offboarding(NULL, $1::uuid, 'ficha', NULL, $2, true)",
            account["id"],
            MOTIVO,
        )
        return results

    assert run(scenario()) == {
        "admin": DENIED,
        "api": DENIED,
        "worker": DENIED,
        "owner_immediate": "ok",
    }


# --- the audit log: insert only, nobody reads it or deletes it -----------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM admin_audit_log WHERE false",
        "SELECT count(*) FROM admin_audit_log",
        "UPDATE admin_audit_log SET detail = '{}' WHERE false",
        "DELETE FROM admin_audit_log WHERE false",
        "DELETE FROM admin_audit_log",
        "TRUNCATE admin_audit_log",
    ],
)
def test_the_backoffice_cannot_read_change_or_delete_its_audit_log(sql):
    assert run(attempt(ADMIN_URL, sql)) == DENIED


@pytest.mark.parametrize("url_name", ["api", "worker", "conversation"])
def test_no_other_role_touches_the_audit_log(url_name):
    url = {"api": API_URL, "worker": WORKER_URL, "conversation": CONVERSATION_URL}[url_name]
    for sql in (
        "SELECT * FROM admin_audit_log WHERE false",
        "INSERT INTO admin_audit_log (action) VALUES ('app_intruso')",
        "DELETE FROM admin_audit_log WHERE false",
    ):
        assert run(attempt(url, sql)) == DENIED, sql


def test_the_backoffice_inserts_only_its_own_app_events_and_the_base_signs_them():
    async def scenario():
        user, _ = await backoffice("soporte")
        ok = await attempt(
            ADMIN_URL,
            "INSERT INTO admin_audit_log (admin_user, action, account_id, target, detail)"
            " VALUES ($1::uuid, 'app_page_view', NULL, 'salud', '{}')",
            user,
        )
        forged_event = await attempt(
            ADMIN_URL,
            "INSERT INTO admin_audit_log (admin_user, action) VALUES ($1::uuid, 'login_ok')",
            user,
        )
        forged_author = await attempt(
            ADMIN_URL,
            "INSERT INTO admin_audit_log (admin_user, action, written_by)"
            " VALUES ($1::uuid, 'app_x', 'fichaitor')",
            user,
        )
        forged_role = await attempt(
            ADMIN_URL,
            "INSERT INTO admin_audit_log (admin_user, action, role)"
            " VALUES ($1::uuid, 'app_x', 'propietario')",
            user,
        )
        # Committed, to see what the base wrote: role and author come from the base.
        await admin(
            "INSERT INTO admin_audit_log (admin_user, action) VALUES ($1::uuid, 'app_marca')",
            user,
        )
        row = await owner(
            "SELECT role, written_by FROM admin_audit_log WHERE admin_user = $1::uuid"
            " AND action = 'app_marca'",
            user,
        )
        return ok, forged_event, forged_author, forged_role, row

    ok, forged_event, forged_author, forged_role, row = run(scenario())
    assert ok == "ok"
    assert forged_event == DENIED and forged_author == DENIED and forged_role == DENIED
    assert [(r["role"], r["written_by"]) for r in row] == [("soporte", ADMIN_USER)]


def test_not_even_the_owner_changes_or_deletes_a_recent_row():
    async def scenario():
        user, _ = await backoffice("soporte")
        await admin(
            "INSERT INTO admin_audit_log (admin_user, action) VALUES ($1::uuid, 'app_fija')", user
        )
        return {
            "update": await attempt(
                OWNER_URL,
                "UPDATE admin_audit_log SET detail = '{}' WHERE admin_user = $1::uuid",
                user,
            ),
            "delete": await attempt(
                OWNER_URL, "DELETE FROM admin_audit_log WHERE admin_user = $1::uuid", user
            ),
            "truncate": await attempt(OWNER_URL, "TRUNCATE admin_audit_log"),
        }

    assert run(scenario()) == {
        "update": "RaiseError",
        "delete": "RaiseError",
        "truncate": "RaiseError",
    }


def test_the_owner_reads_the_log_through_the_function_and_nobody_else():
    async def scenario():
        _, owner_token = await backoffice("propietario")
        _, support = await backoffice("soporte")
        rows = await admin("SELECT * FROM admin_audit_read($1, 10, NULL)", owner_token)
        denied = await attempt(ADMIN_URL, "SELECT * FROM admin_audit_read($1, 10, NULL)", support)
        return rows, denied

    rows, denied = run(scenario())
    assert denied == DENIED
    assert rows and rows[0]["action"] == "audit_read"


# --- nobody has TEMPORARY; every function keeps its search_path (M6) ----------------------


def test_no_role_of_the_application_has_temporary():
    async def scenario():
        rows = await owner(
            """
            SELECT r.rolname, has_database_privilege(r.rolname, current_database(), 'TEMPORARY')
                AS temp
            FROM pg_roles r
            WHERE r.rolname IN ('fichaitor_api', 'fichaitor_worker', 'fichaitor_conversation',
                                'fichaitor_admin', 'fichaitor_billing', 'fichaitor_api_dev',
                                'fichaitor_worker_dev', 'fichaitor_conversation_dev', $1)
            """,
            ADMIN_USER,
        )
        create = await attempt(ADMIN_URL, "CREATE TEMP TABLE pg_roles (rolname name)")
        return {r["rolname"]: r["temp"] for r in rows}, create

    temp, create = run(scenario())
    assert {"fichaitor_admin", ADMIN_USER} <= set(temp)
    assert not any(temp.values()), temp
    assert create == DENIED


def test_every_function_of_the_backoffice_fixes_the_search_path_and_is_not_public():
    async def scenario():
        return await owner(
            """
            SELECT p.proname, p.prosecdef, p.proconfig,
                   EXISTS (SELECT 1
                           FROM aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
                           WHERE a.grantee = 0) AS public_execute,
                   has_function_privilege('fichaitor_admin', p.oid, 'EXECUTE') AS admin_execute
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public'
              AND (p.proname LIKE 'admin\\_%' OR p.proname = 'fichaitor_email_offboarding')
            ORDER BY p.proname
            """
        )

    rows = run(scenario())
    names = {r["proname"] for r in rows}
    assert {
        "admin_find_accounts",
        "admin_account_detail",
        "admin_email_matches",
        "admin_waitlist_totals",
        "admin_audit_read",
        "admin_session_check",
        "fichaitor_email_offboarding",
    } <= names
    for r in rows:
        assert r["proconfig"] == ["search_path=pg_catalog, public, pg_temp"], r["proname"]
        assert r["prosecdef"], r["proname"]
        assert not r["public_execute"], r["proname"]
    internal = {
        "admin_actor",
        "admin_audit_write",
        "admin_fail",
        "admin_caller_is_owner",
        "admin_mask_email",
        # Paso 12: the reference of the case and the mode of Stripe, used by the others.
        "admin_ticket",
        "admin_stripe_livemode",
        # One page of a named listing, for admin_list and the screens that embed it.
        "admin_list_page",
    }
    owner_only = {n for n in names if n.startswith("admin_owner_")}
    for r in rows:
        if r["proname"] in internal | owner_only or r["proname"].endswith("_guard"):
            assert not r["admin_execute"], r["proname"]
        else:
            assert r["admin_execute"], r["proname"]


def test_a_temporary_table_does_not_cover_the_tables_of_the_functions():
    # M6: with TEMPORARY revoked this cannot even start; the qualified names and
    # pg_temp last are the second barrier. Shown with the owner, who can create one.
    async def scenario():
        _, token = await backoffice("propietario")
        conn = await asyncpg.connect(OWNER_URL)
        try:
            tr = conn.transaction()
            await tr.start()
            try:
                await conn.execute(
                    "CREATE TEMP TABLE admin_session (token_hash text, admin_user_id uuid)"
                )
                rows = await conn.fetch("SELECT * FROM admin_session_check($1)", token)
                return [r["role"] for r in rows]
            finally:
                await tr.rollback()
        finally:
            await conn.close()

    assert run(scenario()) == ["propietario"]


# --- sign-in pieces the database enforces --------------------------------------------------


def test_the_same_totp_step_is_not_accepted_twice():
    async def scenario():
        user, token = await backoffice("soporte")
        step = (await owner("SELECT floor(extract(epoch FROM now()) / 30)::bigint AS s"))[0]["s"]
        first = await admin("SELECT admin_session_totp($1, $2) AS ok", token, step)
        again = await admin("SELECT admin_session_totp($1, $2) AS ok", token, step)
        far = await admin("SELECT admin_session_totp($1, $2) AS ok", token, step + 10)
        return first[0]["ok"], again[0]["ok"], far[0]["ok"]

    assert run(scenario()) == (True, False, False)


def test_five_failures_lock_the_user_and_the_time_doubles():
    async def scenario():
        user, _ = await backoffice("soporte")
        name = (await owner("SELECT username FROM admin_user WHERE id = $1::uuid", user))[0][
            "username"
        ]
        locks = []
        for _ in range(2):
            for _ in range(5):
                await admin("SELECT admin_login_failed($1, 'password')", name)
            row = await owner(
                "SELECT round(extract(epoch FROM locked_until - now()) / 60) AS minutes"
                " FROM admin_user WHERE id = $1::uuid",
                user,
            )
            locks.append(row[0]["minutes"])
        lookup = await admin("SELECT * FROM admin_login_lookup($1)", name)
        unknown = await admin("SELECT admin_login_failed('pytest-nadie', 'password')")
        return locks, [r["locked_until"] is not None for r in lookup], unknown

    locks, locked, _ = run(scenario())
    assert locks == [15, 30]
    assert locked == [True]
