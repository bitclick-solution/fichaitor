"""Integration: communications by email from the backoffice (db/schema_admin.sql,
«Comunicaciones por correo»; contract in docs/contrato-backoffice.md, 10) against the
database, with the roles of the backoffice, the worker and the API.

Covers: owner role only and a fresh TOTP to confirm; the audience (all, beta, plan, one
account) never takes the waiting list, a deleted account nor one with its leave
scheduled; a commercial one never reaches an account that opted out and a service one
does; the count shown before confirming; the test email to the address of the person;
the jobs at a limited pace, emptied once sent; no address kept in the communication; the
audit; the opt-out by the API (idempotent), gone with the account; the retention of 12
months; the listing with a cursor; and scripts/admin_usuario.py correo.

Skipped unless the test URLs are set. They must point to the TEST database:

    FICHAITOR_TEST_DB=fichaitor_com_test bash scripts/preparar_bd_pruebas.sh
"""

import asyncio
import importlib.util
import json
import os
import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.adapters.mailer.fake import FakeMailer
from app.adapters.store.postgres import PostgresStore
from app.adapters.worker_store.communications import PostgresCommunications
from app.admin.app import create_app
from app.admin.auth import Passwords, csrf_token, token_hash
from app.admin.config import AdminSettings
from app.admin.routes_communications import render_preview
from app.admin.session import COOKIE
from app.domain import unsubscribe
from app.domain.jobs import Job
from app.worker.send_mail import SendMail

ROOT = Path(__file__).resolve().parent.parent
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")
ADMIN_URL = os.environ.get("FICHAITOR_TEST_ADMIN_URL")
API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
WORKER_URL = os.environ.get("FICHAITOR_TEST_WORKER_URL")

pytestmark = pytest.mark.skipif(
    not (OWNER_URL and ADMIN_URL and API_URL and WORKER_URL),
    reason="sin base de datos de pruebas configurada",
)

KEY = "cd" * 32
PEPPER = "pimienta-de-pruebas-de-comunicaciones-de-mas-de-32"
SUB = "pytest-com-"
USER_PREFIX = "pytestcom"
SECRET = "secreto-de-baja-de-pruebas-con-mas-de-32-caracteres"
BASE = "https://fichaitor.es"
PER_MINUTE = 30


def run(coro):
    return asyncio.run(coro)


async def owner(sql: str, *args):
    conn = await asyncpg.connect(OWNER_URL)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def as_role(url: str, sql: str, *args):
    conn = await asyncpg.connect(url)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


def settings() -> AdminSettings:
    return AdminSettings(
        database_url=ADMIN_URL or "",
        totp_key=KEY,
        password_pepper=PEPPER,
        argon2_time_cost=1,
        argon2_memory_kib=8,
        argon2_parallelism=1,
        communication_per_minute=PER_MINUTE,
    )


@pytest.fixture(scope="module")
def client():
    app = create_app(settings(), environ={})
    with TestClient(app, base_url="https://localhost:8090") as test_client:
        yield test_client


# --- cleaning -----------------------------------------------------------------------------

MINE = f"(SELECT id FROM account WHERE google_sub LIKE '{SUB}%')"
MY_USERS = f"(SELECT id FROM admin_user WHERE username LIKE '{USER_PREFIX}%')"
MY_COMMS = f"(SELECT id::text FROM admin_communication WHERE created_by IN {MY_USERS})"


async def cleanup() -> None:
    await owner(
        f"DELETE FROM job WHERE kind = 'send_mail' AND payload->>'communication_id' IN {MY_COMMS}"
    )  # noqa: E501, S608
    await owner(f"DELETE FROM job WHERE kind = 'send_mail' AND payload->>'to' LIKE '{SUB}%'")  # noqa: S608
    await owner(f"DELETE FROM admin_communication WHERE created_by IN {MY_USERS}")  # noqa: S608
    for table in ("offboarding", "plan_entitlement", "location_activation_log", "session"):
        await owner(f"DELETE FROM {table} WHERE account_id IN {MINE}")  # noqa: S608
    await owner(f"UPDATE account SET selected_location_id = NULL WHERE id IN {MINE}")  # noqa: S608
    await owner(f"DELETE FROM location WHERE account_id IN {MINE}")  # noqa: S608
    await owner(f"DELETE FROM account WHERE id IN {MINE}")  # noqa: S608
    await owner(f"DELETE FROM waitlist_entry WHERE contact LIKE '{SUB}%'")  # noqa: S608
    await owner(f"DELETE FROM admin_user WHERE username LIKE '{USER_PREFIX}%'")  # noqa: S608


@pytest.fixture(autouse=True)
def clean():
    run(cleanup())
    yield
    run(cleanup())


# --- helpers ------------------------------------------------------------------------------


def logged(role: str, *, fresh: bool = True, email: str | None = None) -> dict:
    async def scenario():
        name = f"{USER_PREFIX}{uuid.uuid4().hex[:10]}"
        user_id = (
            await owner(
                "INSERT INTO admin_user (username, role, password_hash, totp_secret_enc,"
                " must_enroll, email) VALUES ($1, $2, $3, 'v1.sin-secreto', false, $4)"
                " RETURNING id::text",
                name,
                role,
                Passwords(PEPPER, time_cost=1, memory_kib=8, parallelism=1).hash("x" * 12),
                email,
            )
        )[0]["id"]
        token = secrets.token_urlsafe(32)
        verified = "now()" if fresh else "now() - interval '10 minutes'"
        await owner(
            "INSERT INTO admin_session (token_hash, admin_user_id, expires_at, totp_verified_at)"
            f" VALUES ($1, $2::uuid, now() + interval '8 hours', {verified})",
            token_hash(token),
            user_id,
        )
        return user_id, name, token

    user_id, name, token = run(scenario())
    return {
        "id": user_id,
        "name": name,
        "headers": {
            "Cookie": f"{COOKIE}={token}",
            "X-Requested-With": "fichaitor-admin",
            "X-CSRF-Token": csrf_token(PEPPER, token),
        },
    }


def account(*, beta: bool = True, plan: str | None = None) -> dict:
    """An active account of these tests. `beta`: with a beta entitlement in force;
    `plan`: with one active profile on that plan."""

    async def scenario():
        sub = f"{SUB}{uuid.uuid4().hex[:10]}"
        ident = (
            await owner(
                "INSERT INTO account (google_sub, email) VALUES ($1, $2) RETURNING id::text",
                sub,
                f"{sub}@example.com",
            )
        )[0]["id"]
        await owner("DELETE FROM plan_entitlement WHERE account_id = $1::uuid", ident)
        if beta:
            await owner(
                "INSERT INTO plan_entitlement (account_id, plan, quantity, starts_at, ends_at,"
                " source) VALUES ($1::uuid, 'pro', 3, now() - interval '1 day',"
                " now() + interval '30 days', 'beta')",
                ident,
            )
        if plan is not None:
            location = (
                await owner(
                    "INSERT INTO location (account_id, gbp_account, gbp_location, title)"
                    " VALUES ($1::uuid, 'accounts/pytest', $2, 'Bar') RETURNING id",
                    ident,
                    f"locations/{sub}",
                )
            )[0]["id"]
            await owner(
                "UPDATE location SET active_since = now(), plan = $2 WHERE id = $1",
                location,
                plan,
            )
        return {"id": ident, "sub": sub, "email": f"{sub}@example.com"}

    return run(scenario())


def body(**change) -> dict:
    data = {
        "kind": "comercial",
        "subject": "Novedad en Fichaitor",
        "preheader": "Te contamos qué hay de nuevo.",
        "headline": "Hay novedades",
        "paragraphs": ["Primer párrafo.", "Segundo párrafo."],
        "button_label": "Ver la novedad",
        "button_url": "https://fichaitor.es/app#facturacion",
        "audience": "cuenta",
        "audience_plan": None,
        "audience_account": None,
    }
    data.update(change)
    return data


def create(client, me, **change) -> str:
    reply = client.post("/admin/api/communications", json=body(**change), headers=me["headers"])
    assert reply.status_code == 201, reply.text
    return reply.json()["communication_id"]


def confirmed_after_test(client, me, cid) -> dict:
    assert (
        client.post(f"/admin/api/communications/{cid}/test", headers=me["headers"]).status_code
        == 202
    )
    count = client.get(f"/admin/api/communications/{cid}/audience", headers=me["headers"]).json()
    reply = client.post(
        f"/admin/api/communications/{cid}/confirm",
        json={"expected_recipients": count["recipients"]},
        headers=me["headers"],
    )
    assert reply.status_code == 202, reply.text
    return reply.json()


def jobs_of(cid: str) -> list[dict]:
    rows = run(
        owner(
            "SELECT id::text, payload, run_at, max_attempts, idempotency_key FROM job"
            " WHERE kind = 'send_mail' AND payload->>'communication_id' = $1 ORDER BY run_at",
            cid,
        )
    )
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]


def recipients_of(cid: str) -> set[str]:
    return {j["payload"]["to"] for j in jobs_of(cid) if not j["payload"].get("test")}


def audit(user_id: str) -> list[dict]:
    rows = run(
        owner(
            "SELECT action, account_id::text, target, detail FROM admin_audit_log"
            " WHERE admin_user = $1::uuid ORDER BY id",
            user_id,
        )
    )
    return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]


# --- roles and TOTP ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["soporte", "finanzas"])
def test_only_the_owner_role_writes_reads_or_sends_communications(client, role):
    me = logged(role, email="persona@bitclick.es")
    fake = str(uuid.uuid4())
    calls = [
        ("post", "/admin/api/communications", body(audience="todas")),
        ("get", "/admin/api/communications", None),
        ("get", f"/admin/api/communications/{fake}", None),
        ("get", f"/admin/api/communications/{fake}/preview", None),
        ("get", f"/admin/api/communications/{fake}/audience", None),
        ("post", f"/admin/api/communications/{fake}/test", None),
        ("post", f"/admin/api/communications/{fake}/confirm", {"expected_recipients": 1}),
    ]
    for method, url, payload in calls:
        reply = (
            getattr(client, method)(url, json=payload, headers=me["headers"])
            if payload
            else getattr(client, method)(url, headers=me["headers"])
        )
        assert reply.status_code == 403, url
        assert reply.json()["code"] == "role_not_allowed", url
    denied = [a for a in audit(me["id"]) if a["action"] == "app_denied"]
    assert len(denied) == len(calls)


def test_confirming_asks_for_a_fresh_totp(client):
    me = logged("propietario", fresh=False, email="propietario@bitclick.es")
    target = account()
    cid = create(client, me, audience_account=target["id"])
    assert (
        client.post(f"/admin/api/communications/{cid}/test", headers=me["headers"]).status_code
        == 202
    )
    reply = client.post(
        f"/admin/api/communications/{cid}/confirm",
        json={"expected_recipients": 1},
        headers=me["headers"],
    )
    assert reply.status_code == 403 and reply.json()["code"] == "totp_required"
    assert recipients_of(cid) == set()
    assert any(
        a["action"] == "app_denied" and a["detail"] == {"code": "totp_required"}
        for a in audit(me["id"])
    )


def test_the_database_refuses_the_functions_to_any_other_role():
    for url in (API_URL, WORKER_URL):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            run(as_role(url, "SELECT admin_communications('x', 10, NULL, NULL)"))
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        run(
            as_role(
                ADMIN_URL, "SELECT fichaitor_communication_unsubscribe(gen_random_uuid(), 'enlace')"
            )
        )
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        run(as_role(ADMIN_URL, "SELECT fichaitor_communication_purge()"))
    for url in (API_URL, WORKER_URL, ADMIN_URL):
        for table in ("admin_communication", "communication_optout"):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                run(as_role(url, f"SELECT 1 FROM {table} LIMIT 1"))  # noqa: S608


# --- drafts, preview and test ------------------------------------------------------------------


def test_the_button_can_only_point_to_fichaitor_in_the_process_and_in_the_database(client):
    me = logged("propietario")
    reply = client.post(
        "/admin/api/communications",
        json=body(audience="todas", button_url="https://evil.example/fichaitor.es/"),
        headers=me["headers"],
    )
    assert reply.status_code == 422 and reply.json()["code"] == "invalid"
    assert "https://evil" not in reply.text
    # Even if the process skipped its check, the database refuses it.
    session = me["headers"]["Cookie"].split("=", 1)[1]
    with pytest.raises(asyncpg.CheckViolationError):
        run(
            as_role(
                ADMIN_URL,
                "SELECT admin_communication_save($1, NULL, 'comercial', 'A', 'B', 'C',"
                " ARRAY['p'], 'Ir', 'https://fichaitor.es.evil.com/', 'todas', NULL, NULL)",
                token_hash(session),
            )
        )


def test_a_draft_is_saved_shown_previewed_and_changed_until_it_is_sent(client):
    me = logged("propietario", email="propietario@bitclick.es")
    target = account()
    cid = create(client, me, audience_account=target["id"])
    shown = client.get(f"/admin/api/communications/{cid}", headers=me["headers"]).json()
    assert shown["state"] == "borrador" and shown["created_by"] == me["name"]
    assert shown["paragraphs"] == ["Primer párrafo.", "Segundo párrafo."]
    assert shown["test_is_current"] is False

    preview = client.get(f"/admin/api/communications/{cid}/preview", headers=me["headers"]).json()
    assert preview == render_preview(shown)
    assert "Darme de baja de estas comunicaciones" in preview["html"]
    assert {h["name"] for h in preview["headers"]} == {"List-Unsubscribe", "List-Unsubscribe-Post"}

    html = client.get(f"/admin/api/communications/{cid}/preview.html", headers=me["headers"])
    assert html.status_code == 200 and html.text == preview["html"]
    assert html.headers["content-type"].startswith("text/html")
    assert "style-src 'unsafe-inline'" in html.headers["content-security-policy"]
    assert "script-src" not in html.headers["content-security-policy"]
    assert html.headers["x-frame-options"] == "SAMEORIGIN"
    # Every other answer keeps the strict policy.
    strict = client.get(f"/admin/api/communications/{cid}", headers=me["headers"])
    assert "frame-ancestors 'none'" in strict.headers["content-security-policy"]

    reply = client.put(
        f"/admin/api/communications/{cid}",
        json=body(kind="servicio", audience="todas", headline="Otro titular"),
        headers=me["headers"],
    )
    assert reply.status_code == 200
    shown = client.get(f"/admin/api/communications/{cid}", headers=me["headers"]).json()
    assert (shown["kind"], shown["audience"], shown["headline"]) == (
        "servicio",
        "todas",
        "Otro titular",
    )


def test_the_test_goes_to_the_address_of_the_person_and_confirming_needs_one_of_this_version(
    client,
):
    without = logged("propietario")
    target = account()
    cid = create(client, without, audience_account=target["id"])
    reply = client.post(f"/admin/api/communications/{cid}/test", headers=without["headers"])
    assert reply.status_code == 422 and "admin_usuario.py correo" in reply.json()["message"]

    me = logged("propietario", email="propietario@bitclick.es")
    cid = create(client, me, audience_account=target["id"])
    confirm = {"expected_recipients": 1}
    reply = client.post(
        f"/admin/api/communications/{cid}/confirm", json=confirm, headers=me["headers"]
    )
    assert reply.status_code == 422 and "prueba" in reply.json()["message"]

    reply = client.post(f"/admin/api/communications/{cid}/test", headers=me["headers"])
    assert reply.status_code == 202
    assert reply.json() == {"sent_to": "p***o@bitclick.es"}
    (test,) = jobs_of(cid)
    assert test["payload"] == {
        "template": "comunicacion",
        "to": "propietario@bitclick.es",
        "communication_id": cid,
        "test": True,
    }
    # A change after the test asks for another one.
    client.put(
        f"/admin/api/communications/{cid}",
        json=body(audience_account=target["id"], headline="Cambiado"),
        headers=me["headers"],
    )
    reply = client.post(
        f"/admin/api/communications/{cid}/confirm", json=confirm, headers=me["headers"]
    )
    assert reply.status_code == 422


# --- audience -----------------------------------------------------------------------------------


def test_a_commercial_one_never_reaches_who_opted_out_and_a_service_one_does(client):
    me = logged("propietario", email="propietario@bitclick.es")
    stays, leaves = account(plan="pro_whatsapp"), account(plan="pro_whatsapp")
    run(PostgresStore_unsubscribe(leaves["id"]))

    commercial = create(client, me, audience="plan", audience_plan="pro_whatsapp")
    counts = client.get(
        f"/admin/api/communications/{commercial}/audience", headers=me["headers"]
    ).json()
    confirmed_after_test(client, me, commercial)
    assert stays["email"] in recipients_of(commercial)
    assert leaves["email"] not in recipients_of(commercial)
    assert counts["excluded"] >= 1 and counts["per_minute"] == PER_MINUTE

    service = create(client, me, kind="servicio", audience="plan", audience_plan="pro_whatsapp")
    confirmed_after_test(client, me, service)
    assert {stays["email"], leaves["email"]} <= recipients_of(service)


def PostgresStore_unsubscribe(account_id: str):  # noqa: N802 - reads as what it does
    async def scenario():
        store = PostgresStore(API_URL)
        await store.connect()
        try:
            await store.unsubscribe_communications(account_id, "enlace")
        finally:
            await store.close()

    return scenario()


def test_the_waiting_list_deleted_accounts_and_scheduled_leaves_are_never_in_the_audience(client):
    me = logged("propietario", email="propietario@bitclick.es")
    active = account()
    deleted = account()
    leaving = account()
    waiting = f"{SUB}{uuid.uuid4().hex[:8]}@example.com"
    run(
        owner(
            "INSERT INTO waitlist_entry (channel, contact, consent_version) VALUES"
            " ('email', $1, 'waitlist-2026-11-11')",
            waiting,
        )
    )
    run(owner("UPDATE account SET deleted_at = now() WHERE id = $1::uuid", deleted["id"]))
    run(
        owner(
            "INSERT INTO offboarding (account_id, scope, google_sub, confirmed_at, delete_at)"
            " VALUES ($1::uuid, 'cuenta', $2, now(), now() + interval '48 hours')",
            leaving["id"],
            leaving["sub"],
        )
    )
    cid = create(client, me, kind="servicio", audience="todas")
    confirmed_after_test(client, me, cid)
    sent = recipients_of(cid)
    assert active["email"] in sent
    assert waiting not in sent
    assert leaving["email"] not in sent
    assert all(not to.startswith(deleted["sub"]) for to in sent)
    # Nothing of the waiting list in any job of this communication.
    assert not any(j["payload"]["to"] == waiting for j in jobs_of(cid))


def test_beta_plan_and_one_account_choose_their_accounts(client):
    me = logged("propietario", email="propietario@bitclick.es")
    beta, free = account(beta=True), account(beta=False, plan="gratis")
    cid = create(client, me, kind="servicio", audience="beta")
    confirmed_after_test(client, me, cid)
    assert beta["email"] in recipients_of(cid) and free["email"] not in recipients_of(cid)

    cid = create(client, me, kind="servicio", audience="plan", audience_plan="gratis")
    confirmed_after_test(client, me, cid)
    assert free["email"] in recipients_of(cid) and beta["email"] not in recipients_of(cid)

    cid = create(client, me, kind="servicio", audience="cuenta", audience_account=beta["id"])
    confirmed_after_test(client, me, cid)
    assert recipients_of(cid) == {beta["email"]}


def test_a_single_account_that_opted_out_has_no_commercial_recipient(client):
    me = logged("propietario", email="propietario@bitclick.es")
    target = account()
    run(PostgresStore_unsubscribe(target["id"]))
    cid = create(client, me, audience_account=target["id"])
    counts = client.get(f"/admin/api/communications/{cid}/audience", headers=me["headers"]).json()
    assert counts == {"recipients": 0, "excluded": 1, "per_minute": PER_MINUTE}
    client.post(f"/admin/api/communications/{cid}/test", headers=me["headers"])
    reply = client.post(
        f"/admin/api/communications/{cid}/confirm",
        json={"expected_recipients": 1},
        headers=me["headers"],
    )
    assert reply.status_code == 422
    assert recipients_of(cid) == set()


# --- confirming and sending -------------------------------------------------------------------


def test_confirming_enqueues_one_job_per_recipient_at_the_pace_and_only_once(client):
    me = logged("propietario", email="propietario@bitclick.es")
    accounts = [account(plan="pro") for _ in range(3)]
    cid = create(client, me, kind="servicio", audience="plan", audience_plan="pro")
    client.post(f"/admin/api/communications/{cid}/test", headers=me["headers"])
    count = client.get(f"/admin/api/communications/{cid}/audience", headers=me["headers"]).json()
    wrong = client.post(
        f"/admin/api/communications/{cid}/confirm",
        json={"expected_recipients": count["recipients"] + 1},
        headers=me["headers"],
    )
    assert wrong.status_code == 422 and "ha cambiado" in wrong.json()["message"]

    done = client.post(
        f"/admin/api/communications/{cid}/confirm",
        json={"expected_recipients": count["recipients"]},
        headers=me["headers"],
    ).json()
    assert done["recipients"] == count["recipients"] >= 3 and done["per_minute"] == PER_MINUTE
    sends = [j for j in jobs_of(cid) if not j["payload"].get("test")]
    assert len(sends) == done["recipients"]
    assert {a["email"] for a in accounts} <= {j["payload"]["to"] for j in sends}
    gaps = {
        round((b["run_at"] - a["run_at"]).total_seconds(), 3)
        for a, b in zip(sends, sends[1:], strict=False)
    }
    assert gaps == {60 / PER_MINUTE}
    first = sends[0]["payload"]
    assert set(first) == {"template", "to", "communication_id", "account_id"}
    assert all(
        j["idempotency_key"] == f"comunicacion:{cid}:{j['payload']['account_id']}" for j in sends
    )

    again = client.post(
        f"/admin/api/communications/{cid}/confirm",
        json={"expected_recipients": count["recipients"]},
        headers=me["headers"],
    )
    assert again.status_code == 422
    changed = client.put(
        f"/admin/api/communications/{cid}", json=body(audience="todas"), headers=me["headers"]
    )
    assert changed.status_code == 422
    shown = client.get(f"/admin/api/communications/{cid}", headers=me["headers"]).json()
    assert shown["state"] == "enviando" and shown["confirmed_by"] == me["name"]


def test_the_communication_keeps_no_address_and_the_audit_says_who_sent_what_to_how_many(client):
    me = logged("propietario", email="propietario@bitclick.es")
    target = account()
    cid = create(client, me, audience_account=target["id"])
    confirmed_after_test(client, me, cid)
    row = run(
        owner(
            "SELECT row_to_json(c)::text AS r FROM admin_communication c WHERE id = $1::uuid", cid
        )
    )
    stored = row[0]["r"]
    assert target["email"] not in stored and "@" not in stored
    actions = audit(me["id"])
    names = [a["action"] for a in actions]
    for action in (
        "communication_saved",
        "communication_test_sent",
        "communication_audience_read",
        "communication_sent",
    ):
        assert action in names
    sent = next(a for a in actions if a["action"] == "communication_sent")
    assert sent["target"] == cid and sent["account_id"] == target["id"]
    assert sent["detail"] == {
        "kind": "comercial",
        "audience": "cuenta",
        "plan": None,
        "recipients": 1,
        "excluded": 0,
        "per_minute": PER_MINUTE,
    }
    assert "@" not in json.dumps([a["detail"] for a in actions])


def test_the_worker_sends_counts_and_empties_every_payload(client):
    me = logged("propietario", email="propietario@bitclick.es")
    first, second = account(plan="pro"), account(plan="pro")
    cid = create(client, me, audience="cuenta", audience_account=first["id"])
    confirmed_after_test(client, me, cid)
    jobs = jobs_of(cid)

    async def scenario():
        pool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=2)
        mailer = FakeMailer()
        try:
            from app.adapters.worker_store.postgres import PostgresWorkerStore

            send = SendMail(
                PostgresWorkerStore(pool),
                mailer,
                BASE,
                communications=PostgresCommunications(pool),
                unsubscribe_secret=SECRET,
            )
            for job in jobs:
                await send(Job(job["id"], "send_mail", job["payload"]))
        finally:
            await pool.close()
        return mailer.sent

    sent = run(scenario())
    assert [m.to for m in sent] == ["propietario@bitclick.es", first["email"]]
    assert sent[0].subject.startswith("[Prueba] ")
    token = unsubscribe.sign(SECRET, first["id"])
    assert f"{BASE}/baja-comunicaciones#{token}" in sent[1].html
    assert dict(sent[1].headers)["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert all(j["payload"] == {} for j in jobs_of_any(cid, [j["id"] for j in jobs]))
    shown = client.get(f"/admin/api/communications/{cid}", headers=me["headers"]).json()
    assert (shown["state"], shown["sent"], shown["failed"], shown["skipped"]) == (
        "enviada",
        1,
        0,
        0,
    )
    assert shown["finished_at"] is not None
    _ = second


def jobs_of_any(cid: str, ids: list[str]) -> list[dict]:
    rows = run(owner("SELECT payload FROM job WHERE id = ANY($1::uuid[])", ids))
    assert len(rows) == len(ids)
    return [{"payload": json.loads(r["payload"])} for r in rows]


def test_an_account_that_opts_out_after_the_confirmation_is_skipped_by_the_worker(client):
    me = logged("propietario", email="propietario@bitclick.es")
    target = account()
    cid = create(client, me, audience_account=target["id"])
    confirmed_after_test(client, me, cid)
    run(PostgresStore_unsubscribe(target["id"]))

    async def deliver():
        pool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=1)
        try:
            comms = PostgresCommunications(pool)
            mail = await comms.mail(cid, target["id"])
            await comms.result(cid, "skipped")
            return mail
        finally:
            await pool.close()

    mail = run(deliver())
    assert mail["deliver"] is False and mail["kind"] == "comercial"
    shown = client.get(f"/admin/api/communications/{cid}", headers=me["headers"]).json()
    assert (shown["state"], shown["skipped"]) == ("enviada", 1)


# --- the opt-out ----------------------------------------------------------------------------------


def test_the_opt_out_of_the_api_is_idempotent_and_goes_with_the_account():
    target = account()
    run(PostgresStore_unsubscribe(target["id"]))
    run(PostgresStore_unsubscribe(target["id"]))
    rows = run(
        owner("SELECT source FROM communication_optout WHERE account_id = $1::uuid", target["id"])
    )
    assert [r["source"] for r in rows] == ["enlace"]
    # An account that does not exist leaves nothing, and no error says so.
    run(PostgresStore_unsubscribe(str(uuid.uuid4())))
    run(owner("UPDATE account SET deleted_at = now() WHERE id = $1::uuid", target["id"]))
    assert (
        run(owner("SELECT 1 FROM communication_optout WHERE account_id = $1::uuid", target["id"]))
        == []
    )


# --- listing, retention and the script ---------------------------------------------------


def test_the_history_goes_by_pages_with_a_cursor(client):
    me = logged("propietario")
    ids = [create(client, me, audience="todas", subject=f"Asunto {n}") for n in range(3)]
    first = client.get("/admin/api/communications?limit=2", headers=me["headers"]).json()
    assert [c["communication_id"] for c in first["communications"]][:2] == ids[::-1][:2]
    assert first["next_cursor"] is not None and first["per_minute"] == PER_MINUTE
    assert "paragraphs" not in first["communications"][0]
    second = client.get(
        f"/admin/api/communications?limit=2&cursor={first['next_cursor']}", headers=me["headers"]
    ).json()
    assert second["communications"][0]["communication_id"] == ids[0]


def test_communications_are_deleted_after_12_months_by_the_worker():
    me = logged("propietario")
    rows = run(
        owner(
            "INSERT INTO admin_communication (kind, subject, preheader, headline, paragraphs,"
            " audience, created_by, created_at, updated_at) VALUES"
            " ('servicio', 'Vieja', 'P', 'T', ARRAY['p'], 'todas', $1::uuid,"
            "  now() - interval '13 months', now() - interval '13 months'),"
            " ('servicio', 'Nueva', 'P', 'T', ARRAY['p'], 'todas', $1::uuid,"
            "  now() - interval '11 months', now() - interval '11 months')"
            " RETURNING id::text, subject",
            me["id"],
        )
    )
    gone = run(as_role(WORKER_URL, "SELECT fichaitor_communication_purge() AS n"))[0]["n"]
    assert gone >= 1
    left = {
        r["subject"]
        for r in run(
            owner(
                "SELECT subject FROM admin_communication WHERE id = ANY($1::uuid[])",
                [r["id"] for r in rows],
            )
        )
    }
    assert left == {"Nueva"}


def test_the_retention_of_the_worker_runs_the_purge():
    from app.adapters.worker_store.postgres import PostgresWorkerStore
    from app.domain.offboarding import RetentionPolicy

    me = logged("propietario")
    run(
        owner(
            "INSERT INTO admin_communication (kind, subject, preheader, headline, paragraphs,"
            " audience, created_by, created_at, updated_at) VALUES ('servicio', 'Vieja', 'P',"
            " 'T', ARRAY['p'], 'todas', $1::uuid, now() - interval '13 months',"
            " now() - interval '13 months')",
            me["id"],
        )
    )

    async def scenario():
        pool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=2)
        try:
            return await PostgresWorkerStore(pool).run_retention(
                datetime.now(UTC), RetentionPolicy()
            )
        finally:
            await pool.close()

    counts = run(scenario())
    assert counts["admin_communication"] >= 1


def test_the_owner_sets_the_address_of_the_tests_with_the_script():
    spec = importlib.util.spec_from_file_location(
        "admin_usuario", ROOT / "scripts" / "admin_usuario.py"
    )
    admin_usuario = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(admin_usuario)
    me = logged("propietario")
    out: list[str] = []

    async def scenario(argv):
        conn = await asyncpg.connect(OWNER_URL)
        try:
            return await admin_usuario.run(admin_usuario.parse_args(argv), conn, out.append)
        finally:
            await conn.close()

    assert run(scenario(["correo", "--usuario", me["name"], "--correo", " Ana@Bitclick.es "])) == 0
    assert (
        run(owner("SELECT email FROM admin_user WHERE id = $1::uuid", me["id"]))[0]["email"]
        == "ana@bitclick.es"
    )
    assert run(scenario(["correo", "--usuario", me["name"], "--quitar"])) == 0
    assert (
        run(owner("SELECT email FROM admin_user WHERE id = $1::uuid", me["id"]))[0]["email"] is None
    )
    assert all("@" not in line for line in out)
    rows = run(
        owner(
            "SELECT detail FROM admin_audit_log WHERE action = 'user_email_set' AND target = $1",
            me["id"],
        )
    )
    assert rows and "@" not in "".join(r["detail"] for r in rows)
