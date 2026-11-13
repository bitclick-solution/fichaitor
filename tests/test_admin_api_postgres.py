"""Integration: the backoffice (app/admin, paso 12 of docs/admin-facturacion.md) over HTTP,
against the database with the role fichaitor_admin.

Covers: password and TOTP, the same code twice, the lock at 5 failures and the global cap,
single-use recovery codes, the enrollment link, the role matrix of section 6.3 (one test
per cell), the audit of each action and each read, the fresh TOTP of the sensitive
actions, the CSRF and that no full email, token nor individual waiting-list entry comes
out. Also scripts/admin_usuario.py.

Skipped unless the test URLs are set. They must point to the TEST database (its name
ends in `_test`; tests/conftest.py stops the run otherwise):

    FICHAITOR_TEST_DB=fichaitor_bo_test bash scripts/preparar_bd_pruebas.sh
    B=127.0.0.1:5433/fichaitor_bo_test
    FICHAITOR_TEST_OWNER_URL=postgresql://fichaitor:dev@$B \
    FICHAITOR_TEST_ADMIN_URL=postgresql://fichaitor_admin_dev:dev@$B \
    uv run pytest -q tests/test_admin_api_postgres.py

The audit log of the backoffice cannot be deleted (that is the point). So that the global
cap of 20 failures per hour does not stop later runs, a fixture removes, in the test
database only and as its owner, the `login_failed` rows of the last hours (it lifts the
guard trigger inside one transaction).
"""

import asyncio
import io
import os
import re
import secrets
import time
import uuid
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pyotp
import pytest
from fastapi.testclient import TestClient

from app.admin.app import create_app
from app.admin.auth import Passwords, SecretBox, csrf_token, normalize_recovery_code, token_hash
from app.admin.config import AdminSettings
from app.admin.session import COOKIE

ROOT = Path(__file__).resolve().parent.parent
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")
ADMIN_URL = os.environ.get("FICHAITOR_TEST_ADMIN_URL")

pytestmark = pytest.mark.skipif(
    not (OWNER_URL and ADMIN_URL), reason="sin base de datos de pruebas configurada"
)

KEY = "ab" * 32
PEPPER = "pimienta-de-pruebas-con-mas-de-32-caracteres"
AUTHSERV = "mx.fichaitor.test"
SUB = "pytest-bo-"
USER_PREFIX = "pytestbo"
PASSWORD = "caballo correcto grapa pila"
TICKET = "Ticket 9876"
FAST = {"time_cost": 1, "memory_kib": 8, "parallelism": 1}
PASSWORDS = Passwords(PEPPER, **FAST)
BOX = SecretBox(KEY)
JSON = {"X-Requested-With": "fichaitor-admin"}


def run(coro):
    return asyncio.run(coro)


async def owner(sql: str, *args):
    conn = await asyncpg.connect(OWNER_URL)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


def settings() -> AdminSettings:
    return AdminSettings(
        database_url=ADMIN_URL or "",
        totp_key=KEY,
        password_pepper=PEPPER,
        authserv_id=AUTHSERV,
        argon2_time_cost=1,
        argon2_memory_kib=8,
        argon2_parallelism=1,
    )


@pytest.fixture(scope="module")
def client():
    app = create_app(settings(), environ={})
    with TestClient(app, base_url="https://localhost:8090") as test_client:
        yield test_client


# --- cleaning -----------------------------------------------------------------------------


async def cleanup() -> None:
    mine = f"(SELECT id FROM account WHERE google_sub LIKE '{SUB}%')"
    leaves = f"(SELECT id::text FROM offboarding WHERE account_id IN {mine})"
    refunds = f"(SELECT id::text FROM billing_refund_request WHERE account_id IN {mine})"
    await owner(
        f"DELETE FROM job WHERE kind = 'apply_offboarding' AND payload->>'offboarding_id' IN {leaves}"  # noqa: E501, S608
    )
    await owner(
        f"DELETE FROM job WHERE kind = 'billing_refund' AND payload->>'refund_request_id' IN {refunds}"  # noqa: E501, S608
    )
    await owner(f"DELETE FROM job WHERE kind = 'send_mail' AND payload->>'to' LIKE '{SUB}%'")  # noqa: S608
    for table in (
        "billing_refund_request",
        "account_billing_block",
        "billing_document",
        "billing_charge",
        "plan_entitlement",
        "billing_seat",
        "billing_customer_history",
        "billing_customer",
        "audit_log",
        "conversation_lane",
        "proposal",
        "conversation",
        "link_code",
        "channel_identity",
        "offboarding",
        "session",
        "location_activation_log",
    ):
        await owner(f"DELETE FROM {table} WHERE account_id IN {mine}")  # noqa: S608
    await owner(f"UPDATE account SET selected_location_id = NULL WHERE id IN {mine}")  # noqa: S608
    await owner(f"DELETE FROM location WHERE account_id IN {mine}")  # noqa: S608
    await owner(f"DELETE FROM account WHERE id IN {mine}")  # noqa: S608
    await owner(f"DELETE FROM waitlist_entry WHERE contact LIKE '{SUB}%'")  # noqa: S608
    await owner(f"DELETE FROM admin_user WHERE username LIKE '{USER_PREFIX}%'")  # noqa: S608
    await forget_recent_failures()


async def forget_recent_failures() -> None:
    """Test database only: the global cap counts every login_failed of the last hour."""
    conn = await asyncpg.connect(OWNER_URL)
    try:
        assert (await conn.fetchval("SELECT current_database()")).endswith("_test")
        async with conn.transaction():
            await conn.execute("ALTER TABLE admin_audit_log DISABLE TRIGGER admin_audit_log_guard")
            await conn.execute(
                "DELETE FROM admin_audit_log WHERE action = 'login_failed'"
                " AND at > now() - interval '3 hours'"
            )
            await conn.execute("ALTER TABLE admin_audit_log ENABLE TRIGGER admin_audit_log_guard")
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
def clean():
    run(cleanup())
    yield
    run(cleanup())


# --- helpers ------------------------------------------------------------------------------


async def make_user(role: str, *, secret: str | None = None) -> tuple[str, str]:
    """A person with the enrollment done: password PASSWORD and, if given, a TOTP secret
    sealed with the key of the test settings. Returns (id, username)."""
    name = f"{USER_PREFIX}{uuid.uuid4().hex[:10]}"
    sealed = BOX.seal_totp(secret, name) if secret else "v1.sin-secreto"
    row = await owner(
        "INSERT INTO admin_user (username, role, password_hash, totp_secret_enc, must_enroll)"
        " VALUES ($1, $2, $3, $4, false) RETURNING id::text",
        name,
        role,
        PASSWORDS.hash(PASSWORD),
        sealed,
    )
    return row[0]["id"], name


async def make_session(user_id: str, *, fresh: bool = True) -> str:
    token = secrets.token_urlsafe(32)
    verified = "now()" if fresh else "now() - interval '10 minutes'"
    await owner(
        "INSERT INTO admin_session (token_hash, admin_user_id, expires_at, totp_verified_at)"
        f" VALUES ($1, $2::uuid, now() + interval '8 hours', {verified})",
        token_hash(token),
        user_id,
    )
    return token


def logged(role: str, *, fresh: bool = True, secret: str | None = None) -> dict:
    """A person of `role` with a valid session: what a request needs."""

    async def scenario():
        user_id, name = await make_user(role, secret=secret)
        token = await make_session(user_id, fresh=fresh)
        return user_id, name, token

    user_id, name, token = run(scenario())
    return {
        "id": user_id,
        "name": name,
        "token": token,
        "headers": headers_for(token),
    }


def headers_for(token: str) -> dict:
    return {
        "Cookie": f"{COOKIE}={token}",
        "X-Requested-With": "fichaitor-admin",
        "X-CSRF-Token": csrf_token(PEPPER, token),
    }


def furnish() -> dict:
    """An account with a profile, a beta entitlement, a paid seat and charge, an open
    block, a linked chat and fiscal data."""

    async def scenario():
        sub = f"{SUB}{uuid.uuid4().hex[:8]}"
        account = (
            await owner(
                "INSERT INTO account (google_sub, email) VALUES ($1, $2) RETURNING id::text",
                sub,
                f"{sub}@example.com",
            )
        )[0]["id"]
        location = (
            await owner(
                "INSERT INTO location (account_id, gbp_account, gbp_location, title)"
                " VALUES ($1::uuid, 'accounts/pytest', $2, 'Bar La Parra') RETURNING id",
                account,
                f"locations/{sub}",
            )
        )[0]["id"]
        entitlement = (
            await owner(
                "INSERT INTO plan_entitlement (account_id, plan, quantity, starts_at, ends_at,"
                " source, granted_by) VALUES ($1::uuid, 'pro', 1, now() - interval '1 day',"
                " now() + interval '30 days', 'beta', 'pytest') RETURNING id::text",
                account,
            )
        )[0]["id"]
        seat = f"seat_{secrets.token_hex(8)}"
        invoice = f"in_{secrets.token_hex(8)}"
        await owner(
            "INSERT INTO billing_seat (id, account_id, plan, stripe_subscription_id, location_id,"
            " status, current_period_end, next_price_key) VALUES ($1, $2::uuid, 'pro', $3, $4,"
            " 'active', now() + interval '300 days', 'pro_first_yearly')",
            seat,
            account,
            f"sub_{secrets.token_hex(8)}",
            location,
        )
        await owner(
            "INSERT INTO billing_charge (stripe_invoice_id, account_id, seat_id, status, subtotal,"
            " tax, total, amount_paid, paid_at) VALUES ($1, $2::uuid, $3, 'paid', 3500, 735,"
            " 4235, 4235, now())",
            invoice,
            account,
            seat,
        )
        block = (
            await owner(
                "INSERT INTO account_billing_block (account_id, reason, stripe_invoice_id, seat_id)"
                " VALUES ($1::uuid, 'payment_failed', $2, $3) RETURNING id",
                account,
                f"in_{secrets.token_hex(8)}",
                seat,
            )
        )[0]["id"]
        await owner(
            "INSERT INTO billing_customer (account_id, legal_name, tax_id_type, tax_id_kind,"
            " tax_id, address_line1, postal_code, city, country, territory, tax_treatment,"
            " invoice_email) VALUES ($1::uuid, 'Bar La Parra SL', 'es_cif', 'cif', 'B12345674',"
            " 'Calle Real 1', '18001', 'Granada', 'ES', 'peninsula_baleares', 'iva_21', $2)",
            account,
            f"{sub}-facturas@example.com",
        )
        await owner(
            "INSERT INTO channel_identity (account_id, channel, address)"
            " VALUES ($1::uuid, 'telegram', $2)",
            account,
            str(secrets.randbelow(10**12)),
        )
        return {
            "id": account,
            "sub": sub,
            "email": f"{sub}@example.com",
            "location": location,
            "entitlement": entitlement,
            "invoice": invoice,
            "block": block,
            "seat": seat,
        }

    return run(scenario())


def mail(account: dict, *, dmarc: str = "pass") -> str:
    domain = account["email"].split("@")[1]
    return (
        f"Authentication-Results: {AUTHSERV}; dkim=pass header.d={domain};"
        f" spf=pass smtp.mailfrom={domain}; dmarc={dmarc} (p=reject) header.from={domain}\r\n"
        f"From: El dueño <{account['email']}>\r\n"
        f"To: soporte@fichaitor.es\r\n"
        f"Message-ID: <{uuid.uuid4().hex}@{domain}>\r\n"
        "Subject: Baja\r\n\r\nQuiero darme de baja.\r\n"
    )


def audit_since(user_id: str, after: int) -> list[str]:
    rows = run(
        owner(
            "SELECT action FROM admin_audit_log WHERE admin_user = $1::uuid AND id > $2"
            " ORDER BY id",
            user_id,
            after,
        )
    )
    return [r["action"] for r in rows]


def last_audit_id() -> int:
    return run(owner("SELECT coalesce(max(id), 0) AS id FROM admin_audit_log"))[0]["id"]


# --- sign-in: password, TOTP and the same code twice ------------------------------------


def enroll_by_script(role: str) -> tuple[str, str]:
    """scripts/admin_usuario.py alta, as the owner: (username, token of the link)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "admin_usuario", ROOT / "scripts" / "admin_usuario.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    name = f"{USER_PREFIX}{uuid.uuid4().hex[:8]}"
    args = module.parse_args(["alta", "--usuario", name, "--rol", role])

    async def scenario():
        conn = await asyncpg.connect(OWNER_URL)
        try:
            lines: list[str] = []
            code = await module.run(args, conn, lines.append)
            return code, lines
        finally:
            await conn.close()

    code, lines = run(scenario())
    assert code == 0
    link = next(line for line in lines if line.startswith("http://localhost:8090/#alta="))
    return name, link.split("#alta=", 1)[1]


def finish_enrollment(client, token: str) -> tuple[str, list[str], int]:
    start = client.post("/admin/api/enrollment/start", json={"token": token}, headers=JSON)
    assert start.status_code == 200, start.text
    body = start.json()
    assert body["otpauth_uri"].startswith("otpauth://totp/")
    now = time.time()
    finish = client.post(
        "/admin/api/enrollment/finish",
        json={
            "token": token,
            "password": PASSWORD,
            "state": body["state"],
            "code": pyotp.TOTP(body["secret"]).at(now),
        },
        headers=JSON,
    )
    assert finish.status_code == 200, finish.text
    return body["secret"], finish.json()["recovery_codes"], int(now // 30)


def login(client, name: str, password: str = PASSWORD):
    client.cookies.clear()
    return client.post(
        "/admin/api/login", json={"username": name, "password": password}, headers=JSON
    )


def with_cookie(response) -> dict:
    token = response.cookies.get(COOKIE)
    assert token
    return headers_for(token)


def test_enrollment_password_totp_and_the_same_code_twice(client):
    name, link_token = enroll_by_script("soporte")
    secret, codes, step = finish_enrollment(client, link_token)
    assert len(codes) == 10
    # The link is single use.
    again = client.post("/admin/api/enrollment/start", json={"token": link_token}, headers=JSON)
    assert again.status_code == 404

    first = login(client, name)
    assert first.status_code == 200 and first.json()["stage"] == "totp"
    cookie = first.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=")
    for attribute in ("Secure", "HttpOnly", "SameSite=strict", "Path=/"):
        assert attribute.lower() in cookie.lower()
    assert "domain=" not in cookie.lower()
    headers = with_cookie(first)

    # Before the TOTP the session opens nothing.
    assert client.get("/admin/api/accounts?q=abcdef", headers=headers).status_code == 401
    # The code of the enrollment (same step): already used.
    otp = pyotp.TOTP(secret)
    replay = client.post(
        "/admin/api/session/totp", json={"code": otp.at(step * 30)}, headers=headers
    )
    assert replay.status_code == 401 and replay.json()["code"] == "totp_invalid"
    # The next step works once.
    code = otp.at((step + 1) * 30)
    ok = client.post("/admin/api/session/totp", json={"code": code}, headers=headers)
    assert ok.status_code == 200, ok.text
    assert ok.json()["role"] == "soporte" and ok.json()["username"] == name
    again = client.post("/admin/api/session/totp", json={"code": code}, headers=headers)
    assert again.status_code == 401
    assert client.get("/admin/api/session", headers=headers).json()["stage"] == "ok"

    out = client.post("/admin/api/logout", headers=headers)
    assert out.status_code == 200
    assert client.get("/admin/api/session", headers=headers).status_code == 401


def test_a_wrong_password_and_an_unknown_user_get_the_same_answer(client):
    _, name = run(make_user("soporte"))
    wrong = login(client, name, "otra contraseña larga")
    unknown = login(client, f"{USER_PREFIX}nadie", PASSWORD)
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert COOKIE not in wrong.headers.get("set-cookie", "")


def test_a_password_with_the_pepper_of_other_parameters_is_rehashed(client):
    user_id, name = run(make_user("soporte"))
    strong = Passwords(PEPPER, time_cost=2, memory_kib=16, parallelism=1)
    run(
        owner(
            "UPDATE admin_user SET password_hash = $1 WHERE id = $2::uuid",
            strong.hash(PASSWORD),
            user_id,
        )
    )
    assert login(client, name).status_code == 200
    stored = run(owner("SELECT password_hash FROM admin_user WHERE id = $1::uuid", user_id))
    assert "m=8,t=1,p=1" in stored[0]["password_hash"]


def test_five_failures_lock_the_user_even_with_the_right_password(client):
    user_id, name = run(make_user("finanzas"))
    for _ in range(5):
        assert login(client, name, "contraseña equivocada").status_code == 401
    locked = login(client, name)
    assert locked.status_code == 401 and locked.json()["code"] == "invalid_credentials"
    row = run(
        owner("SELECT locked_until > now() AS locked FROM admin_user WHERE id = $1::uuid", user_id)
    )
    assert row[0]["locked"] is True
    actions = run(owner("SELECT action FROM admin_audit_log WHERE admin_user = $1::uuid", user_id))
    assert [a["action"] for a in actions].count("locked") == 1


def test_five_wrong_totp_codes_lock_the_user_too(client):
    secret = pyotp.random_base32()
    user_id, name = run(make_user("soporte", secret=secret))
    headers = with_cookie(login(client, name))
    for _ in range(5):
        bad = client.post("/admin/api/session/totp", json={"code": "000000"}, headers=headers)
        assert bad.status_code == 401
    good = client.post(
        "/admin/api/session/totp", json={"code": pyotp.TOTP(secret).now()}, headers=headers
    )
    assert good.status_code == 401
    row = run(
        owner("SELECT locked_until > now() AS locked FROM admin_user WHERE id = $1::uuid", user_id)
    )
    assert row[0]["locked"] is True


def test_twenty_failures_in_an_hour_stop_the_whole_backoffice(client):
    _, name = run(make_user("propietario"))
    run(
        owner(
            "SELECT admin_login_failed($1, 'password') FROM generate_series(1, 20)",
            f"{USER_PREFIX}desconocido",
        )
    )
    stopped = login(client, name)
    assert stopped.status_code == 429 and stopped.json()["code"] == "too_many_attempts"


def test_recovery_codes_are_single_use_and_force_a_new_totp(client):
    name, link_token = enroll_by_script("finanzas")
    _, codes, _ = finish_enrollment(client, link_token)

    headers = with_cookie(login(client, name))
    used = client.post("/admin/api/login/recovery", json={"code": codes[0]}, headers=headers)
    assert used.status_code == 200 and used.json()["stage"] == "reenroll"
    # Until the new TOTP, the session opens no screen.
    assert client.get("/admin/api/session", headers=headers).json()["stage"] == "reenroll"
    assert client.get("/admin/api/metrics", headers=headers).status_code == 401
    start = client.post("/admin/api/totp/start", headers=headers)
    assert start.status_code == 200
    new_secret = start.json()["secret"]
    finish = client.post(
        "/admin/api/totp/finish",
        json={"state": start.json()["state"], "code": pyotp.TOTP(new_secret).now()},
        headers=headers,
    )
    assert finish.status_code == 200, finish.text
    new_codes = finish.json()["recovery_codes"]
    assert set(new_codes).isdisjoint(codes)
    assert client.get("/admin/api/session", headers=headers).json()["stage"] == "ok"

    # The used one, and the old ones (replaced), no longer work.
    for old in (codes[0], codes[1]):
        headers = with_cookie(login(client, name))
        refused = client.post("/admin/api/login/recovery", json={"code": old}, headers=headers)
        assert refused.status_code == 401 and refused.json()["code"] == "recovery_invalid"
    headers = with_cookie(login(client, name))
    fresh = client.post(
        "/admin/api/login/recovery",
        json={"code": normalize_recovery_code(new_codes[3]).upper()},
        headers=headers,
    )
    assert fresh.status_code == 200


def test_a_weak_password_is_refused_at_the_enrollment(client):
    name, link_token = enroll_by_script("soporte")
    start = client.post("/admin/api/enrollment/start", json={"token": link_token}, headers=JSON)
    body = start.json()
    for weak, code in (("corta", "password_too_short"), ("password1234", "password_common")):
        refused = client.post(
            "/admin/api/enrollment/finish",
            json={
                "token": link_token,
                "password": weak,
                "state": body["state"],
                "code": pyotp.TOTP(body["secret"]).now(),
            },
            headers=JSON,
        )
        assert refused.status_code == 422 and refused.json()["code"] == code
    # Another link's state does not work with this one.
    wrong = client.post(
        "/admin/api/enrollment/finish",
        json={
            "token": link_token,
            "password": PASSWORD,
            "state": BOX.seal_state(body["secret"], "f" * 64, time.time()),
            "code": pyotp.TOTP(body["secret"]).now(),
        },
        headers=JSON,
    )
    assert wrong.status_code == 409


def test_the_script_resets_and_deactivates_without_printing_passwords(client):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "admin_usuario", ROOT / "scripts" / "admin_usuario.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    name, link_token = enroll_by_script("soporte")
    finish_enrollment(client, link_token)
    headers = with_cookie(login(client, name))

    async def call(argv):
        conn = await asyncpg.connect(OWNER_URL)
        try:
            lines: list[str] = []
            code = await module.run(module.parse_args(argv), conn, lines.append)
            return code, lines
        finally:
            await conn.close()

    code, lines = run(call(["restablecer", "--usuario", name]))
    assert code == 0 and any("#alta=" in line for line in lines)
    assert PASSWORD not in "\n".join(lines)
    # Its sessions are closed and it cannot sign in until the new enrollment.
    assert client.get("/admin/api/session", headers=headers).status_code == 401
    assert login(client, name).status_code == 401
    new_token = next(line for line in lines if "#alta=" in line).split("#alta=", 1)[1]
    finish_enrollment(client, new_token)
    assert login(client, name).status_code == 200

    code, lines = run(call(["desactivar", "--usuario", name]))
    assert code == 0
    assert login(client, name).status_code == 401
    with pytest.raises(module.ScriptError):
        run(call(["alta", "--usuario", name, "--rol", "soporte"]))


def test_the_script_refuses_a_role_that_is_not_the_owner():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "admin_usuario", ROOT / "scripts" / "admin_usuario.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def scenario():
        conn = await asyncpg.connect(ADMIN_URL)
        try:
            return await module.run(
                module.parse_args(["alta", "--usuario", f"{USER_PREFIX}x", "--rol", "soporte"]),
                conn,
                print,
            )
        finally:
            await conn.close()

    with pytest.raises(module.ScriptError) as exc:
        run(scenario())
    assert exc.value.code == 2


def test_the_script_main_needs_the_url():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "admin_usuario", ROOT / "scripts" / "admin_usuario.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.owner_url({}) is None
    url = module.owner_url(
        {"FICHAITOR_OWNER_DB_HOST": "fichaitor-db", "POSTGRES_PASSWORD": "a/b@c"}
    )
    assert url == "postgresql://fichaitor:a%2Fb%40c@fichaitor-db:5432/fichaitor"
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        with pytest.raises(SystemExit):
            module.parse_args(["alta", "--usuario", "Ana López", "--rol", "soporte"])


# --- the role matrix of section 6.3: one test per cell ------------------------------------

ROLES = ("soporte", "finanzas", "propietario")
EVERYONE = set(ROLES)
SUPPORT = {"soporte", "propietario"}
FINANCE = {"finanzas", "propietario"}
OWNER = {"propietario"}


def _requests(row: str, account: dict) -> list[tuple[str, str, dict | None]]:
    acc = account["id"]
    future = (datetime.now(UTC) + timedelta(days=90)).isoformat()
    return {
        "buscar": [("GET", f"/admin/api/accounts?q={acc}", None)],
        "detalle": [
            ("GET", f"/admin/api/accounts/{acc}", None),
            ("POST", f"/admin/api/accounts/{acc}/locations", {"ticket_ref": TICKET}),
            ("GET", f"/admin/api/accounts/{acc}/channels", None),
        ],
        "baja": [
            (
                "POST",
                f"/admin/api/accounts/{acc}/offboarding/check",
                {"raw_message": mail(account)},
            ),
        ],
        "salud": [
            ("GET", "/admin/api/health/overview", None),
            ("GET", f"/admin/api/accounts/{acc}/sync", None),
        ],
        "pagos": [
            ("GET", f"/admin/api/accounts/{acc}/billing", None),
            ("GET", "/admin/api/billing/review", None),
            ("POST", f"/admin/api/accounts/{acc}/billing/contact", {"ticket_ref": TICKET}),
        ],
        "reembolso": [
            (
                "POST",
                "/admin/api/refunds",
                {
                    "stripe_invoice_id": account["invoice"],
                    "reason": "error_cobro",
                    "ticket_ref": TICKET,
                },
            ),
        ],
        "metricas": [("GET", "/admin/api/metrics", None)],
        "derechos": [
            (
                "POST",
                f"/admin/api/accounts/{acc}/entitlements",
                {
                    "plan": "pro",
                    "quantity": 1,
                    "source": "cortesia",
                    "ends_at": future,
                    "note_code": "cliente_fundador",
                    "ticket_ref": TICKET,
                },
            ),
            (
                "POST",
                f"/admin/api/entitlements/{account['entitlement']}/trim",
                {"ticket_ref": TICKET},
            ),
        ],
        "bloqueo": [
            ("POST", f"/admin/api/blocks/{account['block']}/close", {"ticket_ref": TICKET}),
        ],
        "espera": [("GET", "/admin/api/waitlist", None)],
        "usuarios": [
            ("GET", "/admin/api/users", None),
            ("GET", "/admin/api/audit?limit=5", None),
        ],
    }[row]


MATRIX = {
    "buscar": EVERYONE,
    "detalle": EVERYONE,
    "baja": SUPPORT,
    "salud": SUPPORT,
    "pagos": FINANCE,
    "reembolso": FINANCE,
    "metricas": FINANCE,
    "derechos": OWNER,
    "bloqueo": OWNER,
    "espera": OWNER,
    "usuarios": OWNER,
}


def call(client, person: dict, method: str, path: str, body: dict | None):
    if method == "GET":
        return client.get(path, headers=person["headers"])
    return client.post(path, json=body, headers=person["headers"])


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("row", list(MATRIX))
def test_role_matrix(client, row, role):
    account = furnish()
    person = logged(role)
    for method, path, body in _requests(row, account):
        response = call(client, person, method, path, body)
        if role in MATRIX[row]:
            assert response.status_code < 400, (path, response.status_code, response.text)
        else:
            assert response.status_code == 403, (path, response.status_code, response.text)
            assert response.json()["code"] == "role_not_allowed"
    if role not in MATRIX[row]:
        assert "app_denied" in audit_since(person["id"], 0)


# --- audit of every read and every action -------------------------------------------------

AUDITED = [
    ("GET", "/admin/api/accounts?q={acc}", None, ["find_accounts"]),
    ("GET", "/admin/api/accounts?q={email}", None, ["find_accounts"]),
    ("GET", "/admin/api/accounts/{acc}", None, ["account_overview_read"]),
    (
        "POST",
        "/admin/api/accounts/{acc}/locations",
        {"ticket_ref": TICKET},
        ["account_detail_read"],
    ),
    ("GET", "/admin/api/accounts/{acc}/sync", None, ["app_sync_read"]),
    ("GET", "/admin/api/accounts/{acc}/channels", None, ["app_channels_read"]),
    ("GET", "/admin/api/accounts/{acc}/billing", None, ["account_billing_read"]),
    (
        "POST",
        "/admin/api/accounts/{acc}/billing/contact",
        {"ticket_ref": TICKET},
        ["billing_contact_read"],
    ),
    ("GET", "/admin/api/billing/review", None, ["billing_review_read"]),
    ("GET", "/admin/api/health/overview", None, ["health_read", "app_jobs_read"]),
    ("GET", "/admin/api/metrics?from=2026-01-01&to=2026-12-31", None, ["metrics_read"]),
    ("GET", "/admin/api/waitlist", None, ["waitlist_totals_read"]),
    ("GET", "/admin/api/users", None, ["app_users_read"]),
    ("GET", "/admin/api/audit", None, ["audit_read"]),
    ("POST", "/admin/api/accounts/{acc}/entitlements", "grant", ["entitlement_granted"]),
    ("POST", "/admin/api/entitlements/{ent}/trim", {"ticket_ref": TICKET}, ["entitlement_trimmed"]),
    ("POST", "/admin/api/blocks/{block}/close", {"ticket_ref": TICKET}, ["block_closed"]),
    ("POST", "/admin/api/refunds", "refund", ["refund_requested"]),
    ("POST", "/admin/api/accounts/{acc}/offboarding/check", "mail", ["offboarding_email_checked"]),
]


@pytest.mark.parametrize(("method", "path", "body", "actions"), AUDITED)
def test_every_read_and_action_is_audited(client, method, path, body, actions):
    account = furnish()
    person = logged("propietario")
    if body == "grant":
        body = {
            "plan": "pro_whatsapp",
            "quantity": 2,
            "source": "recomendacion",
            "ends_at": (datetime.now(UTC) + timedelta(days=365)).isoformat(),
            "ticket_ref": TICKET,
        }
    elif body == "refund":
        body = {
            "stripe_invoice_id": account["invoice"],
            "amount": 1000,
            "reason": "decision_comercial",
            "ticket_ref": TICKET,
        }
    elif body == "mail":
        body = {"raw_message": mail(account)}
    url = path.format(
        acc=account["id"],
        email=account["email"],
        ent=account["entitlement"],
        block=account["block"],
    )
    before = last_audit_id()
    response = call(client, person, method, url, body)
    assert response.status_code < 400, response.text
    assert audit_since(person["id"], before) == actions


def test_the_leave_by_email_end_to_end_is_audited_and_scheduled_48_hours(client):
    account = furnish()
    person = logged("soporte")
    before = last_audit_id()
    checked = client.post(
        f"/admin/api/accounts/{account['id']}/offboarding/check",
        json={"raw_message": mail(account)},
        headers=person["headers"],
    )
    assert checked.status_code == 200
    assert checked.json() == {
        "dmarc": "pass",
        "from_domain": "example.com",
        "matches_account": True,
    }
    done = client.post(
        f"/admin/api/accounts/{account['id']}/offboarding",
        json={"scope": "cuenta", "ticket_ref": TICKET},
        headers=person["headers"],
    )
    assert done.status_code == 200, done.text
    delete_at = datetime.fromisoformat(done.json()["delete_at"])
    assert timedelta(hours=47) < delete_at - datetime.now(UTC) <= timedelta(hours=48)
    assert audit_since(person["id"], before) == ["offboarding_email_checked", "offboarding_email"]
    row = run(
        owner("SELECT reason FROM offboarding WHERE id = $1::uuid", done.json()["offboarding_id"])
    )
    assert row[0]["reason"] == "email_request"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"dmarc": "fail"}, "dmarc_not_pass"),
        ({"dmarc": "bestguesspass"}, "dmarc_not_pass"),
        ({"forged": True}, "wrong_authserv_id"),
    ],
)
def test_the_leave_needs_dmarc_pass_from_our_server(client, change, reason):
    account = furnish()
    person = logged("soporte")
    message = mail(account, dmarc=change.get("dmarc", "pass"))
    if change.get("forged"):
        # A result written by the sender, above ours: only the first one counts.
        message = (
            "Authentication-Results: mx.atacante.example; dmarc=pass header.from=example.com\r\n"
            + message
        )
    before = last_audit_id()
    refused = client.post(
        f"/admin/api/accounts/{account['id']}/offboarding/check",
        json={"raw_message": message},
        headers=person["headers"],
    )
    assert refused.status_code == 422 and refused.json()["code"] == "dmarc_failed"
    assert reason in refused.json()["message"]
    assert audit_since(person["id"], before) == ["app_offboarding_dmarc_failed"]
    # Without a positive check, the leave is refused.
    leave = client.post(
        f"/admin/api/accounts/{account['id']}/offboarding",
        json={"scope": "cuenta", "ticket_ref": TICKET},
        headers=person["headers"],
    )
    assert leave.status_code == 422


def test_a_message_from_another_address_does_not_match(client):
    account = furnish()
    person = logged("propietario")
    other = dict(account, email=f"{SUB}otro@example.com")
    checked = client.post(
        f"/admin/api/accounts/{account['id']}/offboarding/check",
        json={"raw_message": mail(other)},
        headers=person["headers"],
    )
    assert checked.status_code == 200 and checked.json()["matches_account"] is False
    assert other["email"] not in checked.text


# --- a fresh TOTP for the sensitive actions -------------------------------------------------

SENSITIVE = [
    ("propietario", "grant"),
    ("propietario", "trim"),
    ("propietario", "block"),
    ("finanzas", "refund"),
    ("soporte", "leave"),
    ("propietario", "password"),
]


@pytest.mark.parametrize(("role", "action"), SENSITIVE)
def test_a_sensitive_action_asks_again_for_the_totp(client, role, action):
    account = furnish()
    secret = pyotp.random_base32()
    person = logged(role, fresh=False, secret=secret)
    if action == "leave":
        # The check of the sender does not need a fresh code; the leave does.
        assert (
            client.post(
                f"/admin/api/accounts/{account['id']}/offboarding/check",
                json={"raw_message": mail(account)},
                headers=person["headers"],
            ).status_code
            == 200
        )
    path, body = {
        "grant": (
            f"/admin/api/accounts/{account['id']}/entitlements",
            {
                "plan": "pro",
                "quantity": 1,
                "source": "manual",
                "ends_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
                "ticket_ref": TICKET,
            },
        ),
        "trim": (f"/admin/api/entitlements/{account['entitlement']}/trim", {"ticket_ref": TICKET}),
        "block": (f"/admin/api/blocks/{account['block']}/close", {"ticket_ref": TICKET}),
        "refund": (
            "/admin/api/refunds",
            {
                "stripe_invoice_id": account["invoice"],
                "reason": "error_cobro",
                "ticket_ref": TICKET,
            },
        ),
        "leave": (
            f"/admin/api/accounts/{account['id']}/offboarding",
            {"scope": "cuenta", "ticket_ref": TICKET},
        ),
        "password": (
            "/admin/api/password",
            {"current_password": PASSWORD, "new_password": "otra frase larga y nueva"},
        ),
    }[action]
    before = last_audit_id()
    first = client.post(path, json=body, headers=person["headers"])
    assert first.status_code == 403 and first.json()["code"] == "totp_required"
    step_up = client.post(
        "/admin/api/session/totp",
        json={"code": pyotp.TOTP(secret).now()},
        headers=person["headers"],
    )
    assert step_up.status_code == 200, step_up.text
    second = client.post(path, json=body, headers=person["headers"])
    assert second.status_code < 400, second.text
    actions = audit_since(person["id"], before)
    # The refusal and the new code are in the log, before the action itself.
    assert actions[:2] == ["app_denied", "totp_stepup"], actions


def test_a_refund_is_queued_for_the_billing_process(client):
    account = furnish()
    person = logged("finanzas")
    done = client.post(
        "/admin/api/refunds",
        json={
            "stripe_invoice_id": account["invoice"],
            "amount": 500,
            "reason": "error_cobro",
            "ticket_ref": TICKET,
        },
        headers=person["headers"],
    )
    assert done.status_code == 202
    request_id = done.json()["refund_request_id"]
    rows = run(
        owner(
            "SELECT r.state, r.amount, r.requested_by, j.kind, j.idempotency_key"
            " FROM billing_refund_request r"
            " JOIN job j ON j.payload->>'refund_request_id' = r.id::text"
            " WHERE r.id = $1::uuid",
            request_id,
        )
    )
    assert dict(rows[0]) == {
        "state": "pendiente",
        "amount": 500,
        "requested_by": person["name"],
        "kind": "billing_refund",
        "idempotency_key": f"billing_refund:{request_id}",
    }
    twice = client.post(
        "/admin/api/refunds",
        json={
            "stripe_invoice_id": account["invoice"],
            "reason": "error_cobro",
            "ticket_ref": TICKET,
        },
        headers=person["headers"],
    )
    assert twice.status_code == 422
    too_much = client.post(
        "/admin/api/refunds",
        json={
            "stripe_invoice_id": f"in_{secrets.token_hex(6)}",
            "reason": "error_cobro",
            "ticket_ref": TICKET,
        },
        headers=person["headers"],
    )
    assert too_much.status_code == 404


def test_an_entitlement_is_granted_within_the_limits_and_only_trimmed(client):
    account = furnish()
    person = logged("propietario")
    path = f"/admin/api/accounts/{account['id']}/entitlements"
    late = (datetime.now(UTC) + timedelta(days=800)).isoformat()
    refused = client.post(
        path,
        json={
            "plan": "pro",
            "quantity": 1,
            "source": "cortesia",
            "ends_at": late,
            "ticket_ref": TICKET,
        },
        headers=person["headers"],
    )
    assert refused.status_code == 422
    paid = client.post(
        path,
        json={
            "plan": "pro",
            "quantity": 1,
            "source": "pago",
            "ends_at": late,
            "ticket_ref": TICKET,
        },
        headers=person["headers"],
    )
    assert paid.status_code == 422
    granted = client.post(
        path,
        json={
            "plan": "pro",
            "quantity": 3,
            "source": "cortesia",
            "ends_at": (datetime.now(UTC) + timedelta(days=60)).isoformat(),
            "ticket_ref": TICKET,
        },
        headers=person["headers"],
    )
    assert granted.status_code == 200
    ent = granted.json()["entitlement_id"]
    row = run(owner("SELECT granted_by, source FROM plan_entitlement WHERE id = $1::uuid", ent))
    assert dict(row[0]) == {"granted_by": person["name"], "source": "cortesia"}
    longer = client.post(
        f"/admin/api/entitlements/{ent}/trim",
        json={
            "ends_at": (datetime.now(UTC) + timedelta(days=90)).isoformat(),
            "ticket_ref": TICKET,
        },
        headers=person["headers"],
    )
    assert longer.status_code == 422
    shorter = client.post(
        f"/admin/api/entitlements/{ent}/trim",
        json={"ticket_ref": TICKET},
        headers=person["headers"],
    )
    assert shorter.status_code == 200
    overview = client.get(f"/admin/api/accounts/{account['id']}", headers=person["headers"])
    mine = next(e for e in overview.json()["entitlements"] if e["id"] == ent)
    assert mine["current"] is False


def test_closing_a_block_twice_is_refused(client):
    account = furnish()
    person = logged("propietario")
    path = f"/admin/api/blocks/{account['block']}/close"
    assert client.post(path, json={"ticket_ref": TICKET}, headers=person["headers"]).json() == {
        "account_id": account["id"]
    }
    again = client.post(path, json={"ticket_ref": TICKET}, headers=person["headers"])
    assert again.status_code == 404
    row = run(
        owner("SELECT unblocked_by FROM account_billing_block WHERE id = $1", account["block"])
    )
    assert row[0]["unblocked_by"] == "admin"


def test_a_ticket_with_an_email_address_is_refused(client):
    account = furnish()
    person = logged("soporte")
    refused = client.post(
        f"/admin/api/accounts/{account['id']}/locations",
        json={"ticket_ref": "lo pide dueno@example.com"},
        headers=person["headers"],
    )
    assert refused.status_code == 422


# --- CSRF -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spoil",
    ["no_csrf_token", "wrong_csrf_token", "no_requested_with", "foreign_origin", "cross_site"],
)
def test_every_change_needs_the_csrf_defences(client, spoil):
    account = furnish()
    person = logged("propietario")
    headers = dict(person["headers"])
    if spoil == "no_csrf_token":
        del headers["X-CSRF-Token"]
    elif spoil == "wrong_csrf_token":
        headers["X-CSRF-Token"] = csrf_token(PEPPER, "otra-sesion")
    elif spoil == "no_requested_with":
        del headers["X-Requested-With"]
    elif spoil == "foreign_origin":
        headers["Origin"] = "http://evil.example.com"
    else:
        headers["Sec-Fetch-Site"] = "cross-site"
    refused = client.post(
        f"/admin/api/blocks/{account['block']}/close",
        json={"ticket_ref": TICKET},
        headers=headers,
    )
    assert refused.status_code == 403 and refused.json()["code"] == "csrf_failed"
    open_block = run(
        owner(
            "SELECT unblocked_at IS NULL AS open FROM account_billing_block WHERE id = $1",
            account["block"],
        )
    )
    assert open_block[0]["open"] is True
    headers = dict(
        person["headers"], Origin="http://localhost:8090", **{"Sec-Fetch-Site": "same-origin"}
    )
    accepted = client.post(
        f"/admin/api/blocks/{account['block']}/close",
        json={"ticket_ref": TICKET},
        headers=headers,
    )
    assert accepted.status_code == 200


def test_a_get_never_changes_anything_and_needs_no_csrf(client):
    person = logged("soporte")
    headers = {"Cookie": f"{COOKIE}={person['token']}"}
    assert client.get("/admin/api/session", headers=headers).status_code == 200


# --- what never comes out -------------------------------------------------------------------


def test_no_full_email_token_hash_nor_waitlist_entry_comes_out(client):
    account = furnish()
    for i in range(6):
        run(
            owner(
                "INSERT INTO waitlist_entry (channel, contact, business_name, score, source,"
                " consent_version) VALUES ('email', $1, 'Bar Secreto', 55, 'pytest-bo', 'v1')",
                f"{SUB}espera-{i}@example.com",
            )
        )
    texts = []
    cookies = []
    for role in ROLES:
        person = logged(role)
        for path in (
            f"/admin/api/accounts?q={account['email']}",
            f"/admin/api/accounts/{account['id']}",
            f"/admin/api/accounts/{account['id']}/channels",
            f"/admin/api/accounts/{account['id']}/billing",
            "/admin/api/billing/review",
            "/admin/api/health/overview",
            "/admin/api/waitlist",
            "/admin/api/users",
            "/admin/api/audit?limit=500",
            "/admin/api/metrics",
            "/admin/api/session",
        ):
            response = client.get(path, headers=person["headers"])
            texts.append(response.text)
        texts.append(
            client.post(
                f"/admin/api/accounts/{account['id']}/offboarding/check",
                json={"raw_message": mail(account)},
                headers=person["headers"],
            ).text
        )
        cookies.append(person["token"])
    found = client.get(
        f"/admin/api/accounts?q={account['email']}", headers=logged("soporte")["headers"]
    ).json()["accounts"]
    assert [a["account_id"] for a in found] == [account["id"]]
    assert found[0]["email_masked"] != account["email"] and "***" in found[0]["email_masked"]

    tokens = run(
        owner(
            "SELECT token_hash FROM admin_session s JOIN admin_user u ON u.id = s.admin_user_id"
            " WHERE u.username LIKE $1",
            f"{USER_PREFIX}%",
        )
    )
    secrets_in_db = run(
        owner(
            "SELECT password_hash, totp_secret_enc FROM admin_user WHERE username LIKE $1",
            f"{USER_PREFIX}%",
        )
    )
    joined = "\n".join(texts)
    for cookie in cookies:
        assert cookie not in joined
    assert account["email"] not in joined
    assert f"{account['sub']}-facturas@example.com" not in joined
    assert "Bar Secreto" not in joined and "espera-" not in joined
    for row in tokens:
        assert row["token_hash"] not in joined
    for row in secrets_in_db:
        assert row["password_hash"] not in joined and row["totp_secret_enc"] not in joined
    # The names of the profiles are not in the overview: only with a ticket.
    overview = client.get(
        f"/admin/api/accounts/{account['id']}", headers=logged("soporte")["headers"]
    ).text
    assert "Bar La Parra" not in overview
    # The waiting list, only in cells of 5 or more.
    cells = client.get("/admin/api/waitlist", headers=logged("propietario")["headers"]).json()
    mine = [c for c in cells["cells"] if c["source"] == "pytest-bo"]
    assert len(mine) == 1 and mine[0]["people"] == 6
    assert set(mine[0]) == {"week", "channel", "score_band", "source", "people"}


def test_the_names_of_the_profiles_and_the_invoice_email_need_a_ticket(client):
    account = furnish()
    person = logged("finanzas")
    names = client.post(
        f"/admin/api/accounts/{account['id']}/locations",
        json={"ticket_ref": TICKET},
        headers=person["headers"],
    )
    assert names.json()["locations"][0]["title"] == "Bar La Parra"
    contact = client.post(
        f"/admin/api/accounts/{account['id']}/billing/contact",
        json={"ticket_ref": TICKET},
        headers=person["headers"],
    )
    assert contact.json()["invoice_email"] == f"{account['sub']}-facturas@example.com"
    without = client.post(
        f"/admin/api/accounts/{account['id']}/billing/contact", json={}, headers=person["headers"]
    )
    assert without.status_code == 422
    billing = client.get(
        f"/admin/api/accounts/{account['id']}/billing", headers=person["headers"]
    ).json()
    assert "invoice_email" not in billing["customer"] and "address_line1" not in str(billing)
    assert billing["charges"][0]["stripe_url"].startswith("https://dashboard.stripe.com/")


def test_the_metrics_are_totals(client):
    furnish()
    person = logged("finanzas")
    response = client.get("/admin/api/metrics", headers=person["headers"])
    assert response.status_code == 200
    body = response.json()
    assert body["revenue"]["arr_cents"] >= 3500
    assert body["revenue"]["mrr_cents"] == round(body["revenue"]["arr_cents"] / 12)
    assert set(body["funnel"]) == FUNNEL_STEPS
    assert set(body["churn"]) == {"seats_canceled", "seats_to_cancel", "account_leaves"}
    assert body["signups"]["accounts"] >= 1
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", body["period"]["from"])
    assert (
        client.get(
            "/admin/api/metrics?from=2026-01-01&to=2025-01-01", headers=person["headers"]
        ).status_code
        == 422
    )


# Every step has something that records it (docs/contrato-backoffice.md, «Métricas»).
FUNNEL_STEPS = {
    "visit",
    "diagnosis",
    "screenshot_diagnosis",
    "signup",
    "connected",
    "checkout_started",
    "paid",
}
METRICS_PERIOD = "from=2019-03-01&to=2019-04-01"
METRICS_SOURCE = "pytest-metricas"


def _metrics(client, person: dict) -> dict:
    response = client.get(f"/admin/api/metrics?{METRICS_PERIOD}", headers=person["headers"])
    assert response.status_code == 200
    return response.json()


async def _account(created_at: str | None, *, auto_beta: bool) -> str:
    """An account of the test. The trigger gives every account the automatic beta right
    (no launch date in the test database); `auto_beta=False` takes it away."""
    sub = f"{SUB}{uuid.uuid4().hex[:8]}"
    row = await owner(
        "INSERT INTO account (google_sub, email, created_at)"
        " VALUES ($1, $2, coalesce($3::text::timestamptz, now())) RETURNING id::text",
        sub,
        f"{sub}@example.com",
        created_at,
    )
    account = row[0]["id"]
    if not auto_beta:
        await owner(
            "DELETE FROM plan_entitlement WHERE account_id = $1::uuid AND source = 'beta'",
            account,
        )
    return account


async def _seat(
    account: str, created_at: str, *, status: str, contracted: bool, paid_cents: int = 0
) -> str:
    """A seat. `contracted`: its first invoice was paid, so it has a 'pago' right (also the
    0 € invoice of the beta window). `paid_cents`: the money of that invoice."""
    seat = f"seat_{secrets.token_hex(8)}"
    await owner(
        "INSERT INTO billing_seat (id, account_id, plan, status, current_period_end,"
        " next_price_key, created_at, updated_at) VALUES ($1, $2::uuid, 'pro', $3,"
        " $4::text::timestamptz + interval '365 days', 'pro_first_yearly', $4::text::timestamptz,"
        " $4::text::timestamptz + interval '1 day')",
        seat,
        account,
        status,
        created_at,
    )
    if contracted:
        invoice = f"in_{secrets.token_hex(8)}"
        await owner(
            "INSERT INTO billing_charge (stripe_invoice_id, account_id, seat_id, status,"
            " billing_reason, total, amount_paid, paid_at) VALUES ($1, $2::uuid, $3, 'paid',"
            " 'subscription_create', $4, $4, $5::text::timestamptz)",
            invoice,
            account,
            seat,
            paid_cents,
            created_at,
        )
        await owner(
            "INSERT INTO plan_entitlement (account_id, plan, quantity, starts_at, ends_at,"
            " source, external_ref, seat_id) VALUES ($1::uuid, 'pro', 1, $2::text::timestamptz,"
            " $2::text::timestamptz + interval '400 days', 'pago', $3, $4)",
            account,
            created_at,
            invoice,
            seat,
        )
    return seat


def test_the_metrics_count_only_what_is_recorded(client):
    """Each figure against what records it: the seats that were never paid are neither new
    nor lost, no seat is «lost for not paying» (the block has no time limit), a 0 € invoice
    of the beta window is not a payment and a beta extended from the backoffice does not make
    an account of the beta."""
    person = logged("finanzas")
    before = _metrics(client, person)

    async def scenario():
        # Accounts of the period, without the automatic beta: the Gratis cohort.
        paying = await _account("2019-03-05", auto_beta=False)
        trial = await _account("2019-03-06", auto_beta=False)
        extended = await _account("2019-03-07", auto_beta=False)
        await owner(
            "INSERT INTO plan_entitlement (account_id, plan, quantity, starts_at, ends_at,"
            " source, granted_by) VALUES ($1::uuid, 'pro', 1, now(), now() + interval '30 days',"
            " 'beta', 'pytest')",
            extended,
        )
        await _seat(paying, "2019-03-07", status="active", contracted=True, paid_cents=4235)
        await _seat(trial, "2019-03-08", status="active", contracted=True, paid_cents=0)
        # Its first payment never completed: canceled without a right.
        await _seat(extended, "2019-03-09", status="canceled", contracted=False)
        # Paid, blocked once and paid again, then canceled: a cancellation, not a loss.
        lost = await _seat(
            paying, "2019-03-10", status="canceled", contracted=True, paid_cents=4235
        )
        await owner(
            "INSERT INTO account_billing_block (account_id, reason, seat_id, blocked_at,"
            " unblocked_at, unblocked_by) VALUES ($1::uuid, 'payment_failed', $2,"
            " '2019-03-11', '2019-03-12', 'payment')",
            paying,
            lost,
        )
        # Accounts of the beta (now, outside the period): one pays, one only has the 0 € of
        # its window.
        beta_paying = await _account(None, auto_beta=True)
        beta_trial = await _account(None, auto_beta=True)
        await _seat(beta_paying, "2019-02-01", status="active", contracted=True, paid_cents=4235)
        await _seat(beta_trial, "2019-02-01", status="active", contracted=True, paid_cents=0)
        for kind, times in (
            ("visit", 2),
            ("diagnosis", 1),
            ("screenshot_diagnosis", 2),
            ("signup", 1),
            ("connected", 3),
            ("checkout_started", 2),
            ("paid", 1),
        ):
            for _ in range(times):
                await owner(
                    "INSERT INTO funnel_event (at, kind, source) VALUES ('2019-03-15', $1, $2)",
                    kind,
                    METRICS_SOURCE,
                )

    try:
        run(scenario())
        after = _metrics(client, person)
    finally:
        run(owner("DELETE FROM funnel_event WHERE source = $1", METRICS_SOURCE))

    assert set(after["funnel"]) == FUNNEL_STEPS
    assert after["funnel"] == {
        "visit": 2,
        "diagnosis": 1,
        "screenshot_diagnosis": 2,
        "signup": 1,
        "connected": 3,
        "checkout_started": 2,
        "paid": 1,
    }
    assert after["signups"] == {"accounts": 3, "first_seats": 2, "new_seats": 3}
    assert after["churn"]["seats_canceled"] == 1
    assert "seats_lost_unpaid" not in after["churn"]
    assert after["blocks"]["opened"] == 1 and after["blocks"]["closed"] == 1
    assert after["conversion"]["free_accounts"] == 3
    assert after["conversion"]["free_paid"] == 1
    assert after["conversion"]["beta_accounts"] - before["conversion"]["beta_accounts"] == 2
    assert after["conversion"]["beta_paid"] - before["conversion"]["beta_paid"] == 1


# --- pages (keyset): no duplicates and no gaps with rows inserted between pages ----------


def walk(client, path: str, key: str, headers: dict, limit: int, between=None) -> list[dict]:
    """Every page of a listing, calling `between(n)` after page n."""
    rows: list[dict] = []
    cursor = None
    for n in range(100):
        sep = "&" if "?" in path else "?"
        url = f"{path}{sep}limit={limit}" + (f"&cursor={cursor}" if cursor else "")
        response = client.get(url, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        rows.extend(body[key])
        cursor = body["next_cursor"]
        if cursor is None:
            return rows
        if between is not None:
            between(n)
    raise AssertionError("demasiadas páginas")


def test_the_users_go_by_pages_without_duplicates_nor_gaps(client):
    person = logged("propietario")
    first = [run(make_user("soporte"))[1] for _ in range(5)]
    added: list[str] = []

    def between(n: int) -> None:
        added.append(run(make_user("soporte"))[1])

    rows = walk(client, "/admin/api/users", "users", person["headers"], 2, between)
    names = [r["username"] for r in rows]
    assert len(names) == len(set(names))
    assert names == sorted(names)
    assert set(first) <= set(names)
    # A user inserted after the cursor shows up in its place; one before it, not again.
    for name in added:
        assert names.count(name) <= 1


def test_an_account_listing_goes_by_pages_and_a_new_row_neither_repeats_nor_hides(client):
    account = furnish()
    person = logged("finanzas")

    async def blocks(at: str) -> None:
        await owner(
            "INSERT INTO account_billing_block (account_id, reason, seat_id, blocked_at,"
            " unblocked_at, unblocked_by) VALUES ($1::uuid, 'payment_failed', $2,"
            " $3::text::timestamptz, $3::text::timestamptz + interval '1 hour', 'payment')",
            account["id"],
            account["seat"],
            at,
        )

    for day in range(1, 8):
        run(blocks(f"2026-0{day}-01"))
    older = []

    def between(n: int) -> None:
        # Newer than everything seen (before the cursor, DESC): not shown on later pages.
        run(blocks("2030-01-01"))
        # Older than everything: shown at the end, once.
        # Each one older than the one before: always after the cursor.
        older.append(f"2019-{12 - n:02d}-01")
        run(blocks(older[-1]))

    path = f"/admin/api/accounts/{account['id']}/lists/blocks"
    rows = walk(client, path, "items", person["headers"], 2, between)
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids))
    dates = [r["blocked_at"] for r in rows]
    assert dates == sorted(dates, reverse=True)
    assert sum(1 for d in dates if d.startswith("2019-")) == len(older)
    assert not any(d.startswith("2030-") for d in dates[1:])
    # The first page of the overview carries the cursor of the next one.
    overview = client.get(f"/admin/api/accounts/{account['id']}", headers=person["headers"])
    assert set(overview.json()["next_cursors"]) >= {"blocks", "seats", "activations"}


def test_the_failed_jobs_go_by_pages(client):
    person = logged("soporte")
    sub = f"{SUB}{uuid.uuid4().hex[:8]}"

    async def failed(at: str | None) -> None:
        await owner(
            "INSERT INTO job (kind, payload, state, finished_at, last_error)"
            " VALUES ('send_mail', $1::jsonb, 'failed', $2::text::timestamptz, 'x')",
            f'{{"to": "{sub}@example.com"}}',
            at,
        )

    for day in range(1, 6):
        run(failed(f"2020-01-0{day}"))
    run(failed(None))

    def between(n: int) -> None:
        run(failed("2031-01-01"))

    rows = walk(
        client, "/admin/api/health/failed-jobs", "failed_jobs", person["headers"], 2, between
    )
    ids = [r["job_id"] for r in rows]
    assert len(ids) == len(set(ids))
    mine = run(
        owner(
            "SELECT id::text AS id FROM job WHERE payload->>'to' = $1 AND finished_at"
            " < '2031-01-01' OR (payload->>'to' = $1 AND finished_at IS NULL)",
            f"{sub}@example.com",
        )
    )
    assert {r["id"] for r in mine} <= set(ids)
    # The one without an end date goes last.
    assert rows[-1]["finished_at"] is None or all(r["finished_at"] for r in rows)


def test_the_audit_goes_by_cursor(client):
    person = logged("propietario")
    first = client.get("/admin/api/audit?limit=3", headers=person["headers"]).json()
    assert "next_before" not in first and first["next_cursor"]
    second = client.get(
        f"/admin/api/audit?limit=3&cursor={first['next_cursor']}", headers=person["headers"]
    ).json()
    seen = [r["id"] for r in first["rows"] + second["rows"]]
    assert seen == sorted(seen, reverse=True) and len(seen) == len(set(seen))
    # No gap: the second page starts right after the first one.
    between = run(
        owner(
            "SELECT count(*) AS n FROM admin_audit_log WHERE id < $1 AND id > $2",
            first["rows"][-1]["id"],
            second["rows"][0]["id"],
        )
    )
    assert between[0]["n"] == 0


@pytest.mark.parametrize("cursor", ["no-es-base64!", "WzEsMl0", "eyJhIjoxfQ"])
def test_a_cursor_that_was_not_made_here_is_refused(client, cursor):
    person = logged("propietario")
    response = client.get(f"/admin/api/users?cursor={cursor}", headers=person["headers"])
    assert response.status_code == 422
    assert response.json()["code"] in ("invalid_cursor", "invalid")


def test_the_limit_has_a_cap(client):
    person = logged("propietario")
    assert client.get("/admin/api/users?limit=201", headers=person["headers"]).status_code == 422
    assert client.get("/admin/api/audit?limit=501", headers=person["headers"]).status_code == 422


def test_the_search_and_the_review_queue_go_by_pages(client):
    accounts = [furnish() for _ in range(3)]
    person = logged("finanzas")
    # furnish() gives all of them the same legal name: a search that finds the three.
    rows = walk(
        client, "/admin/api/accounts?q=Bar%20La%20Parra%20SL", "accounts", person["headers"], 1
    )
    found = [r["account_id"] for r in rows]
    assert len(found) == len(set(found))
    assert {a["id"] for a in accounts} <= set(found)
    # The open blocks of the review queue, oldest first.
    rows = walk(client, "/admin/api/billing/review/lists/blocks", "items", person["headers"], 1)
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids))
    assert {a["block"] for a in accounts} <= set(ids)
    review = client.get("/admin/api/billing/review", headers=person["headers"]).json()
    assert set(review["next_cursors"]) == {
        "documents",
        "seats",
        "refunds",
        "events",
        "paid_without_document",
        "blocks",
    }
    assert (
        client.get("/admin/api/billing/review/lists/nada", headers=person["headers"]).status_code
        == 404
    )
    # Soporte cannot read the lists of the money.
    support = logged("soporte")
    account = accounts[0]["id"]
    assert (
        client.get(
            f"/admin/api/accounts/{account}/lists/charges", headers=support["headers"]
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/admin/api/accounts/{account}/lists/blocks", headers=support["headers"]
        ).status_code
        == 200
    )
