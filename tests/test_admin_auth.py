"""Unit: the pieces of the backoffice that need no database (app/admin, paso 12 of
docs/admin-facturacion.md): passwords with Argon2id and pepper, TOTP steps, the
encryption of the secrets, recovery codes, CSRF, the settings, the Host check, the
security headers and the presentation helpers."""

import time

import pyotp
import pytest
from fastapi.testclient import TestClient

from app.admin.app import create_app
from app.admin.auth import (
    PASSWORD_MIN,
    Passwords,
    SecretBox,
    common_passwords,
    csrf_token,
    new_recovery_codes,
    normalize_recovery_code,
    password_problem,
    provisioning_uri,
    token_hash,
    totp_step,
)
from app.admin.config import AdminConfigError, AdminSettings
from app.admin.routes_screens import recurring_revenue, stripe_links, with_links

KEY = "ab" * 32
PEPPER = "pimienta-de-pruebas-con-mas-de-32-caracteres"
FAST = {"time_cost": 1, "memory_kib": 8, "parallelism": 1}


def settings(**overrides) -> AdminSettings:
    values = {
        "database_url": "postgresql://nadie@127.0.0.1:1/nada_test",
        "totp_key": KEY,
        "password_pepper": PEPPER,
        "authserv_id": "mx.fichaitor.test",
        "argon2_time_cost": 1,
        "argon2_memory_kib": 8,
        "argon2_parallelism": 1,
    }
    values.update(overrides)
    return AdminSettings(**values)


# --- passwords -------------------------------------------------------------------------


def test_a_password_is_hashed_with_argon2id_and_the_pepper():
    passwords = Passwords(PEPPER, **FAST)
    stored = passwords.hash("una frase larga de verdad")
    assert stored.startswith("$argon2id$")
    assert passwords.verify(stored, "una frase larga de verdad")
    assert not passwords.verify(stored, "una frase larga de verdat")
    # Without the pepper, the hash is useless: another pepper does not verify it.
    assert not Passwords("otra-pimienta-distinta-de-mas-de-32-c", **FAST).verify(
        stored, "una frase larga de verdad"
    )


def test_a_missing_hash_is_checked_against_a_dummy_and_fails():
    passwords = Passwords(PEPPER, **FAST)
    assert not passwords.verify(None, "lo que sea")
    assert not passwords.verify("no-es-un-hash", "lo que sea")


def test_a_hash_with_other_parameters_needs_a_rehash():
    weak = Passwords(PEPPER, **FAST)
    strong = Passwords(PEPPER, time_cost=2, memory_kib=16, parallelism=1)
    stored = weak.hash("una frase larga de verdad")
    assert strong.needs_rehash(stored) and not weak.needs_rehash(stored)
    assert strong.verify(stored, "una frase larga de verdad")


def test_the_default_parameters_are_those_of_the_design():
    defaults = AdminSettings()
    assert (defaults.argon2_time_cost, defaults.argon2_memory_kib, defaults.argon2_parallelism) == (
        3,
        65536,
        4,
    )


@pytest.mark.parametrize(
    ("password", "problem"),
    [
        ("corta", "password_too_short"),
        ("x" * 129, "password_too_long"),
        ("Password1234", "password_common"),
        ("CONTRASEÑA123", "password_common"),
        ("qwertyuiop123", "password_common"),
        ("aaaaaaaaaaaaaa", "password_common"),
        ("abcdefghijklmn", "password_common"),
        ("123456789012", "password_common"),
        ("ana-y-su-gato-mimi", "password_has_username"),
        ("caballo correcto grapa", None),
    ],
)
def test_the_password_policy(password, problem):
    assert password_problem(password, "ana") == problem


def test_the_list_of_common_passwords_ships_with_the_image():
    common = common_passwords()
    assert len(common) >= 100
    assert all(len(p) >= PASSWORD_MIN for p in common)


# --- TOTP ------------------------------------------------------------------------------


def test_a_code_gives_its_step_within_one_step_either_side():
    secret = pyotp.random_base32()
    otp = pyotp.TOTP(secret)
    now = time.time()
    step = int(now // 30)
    assert totp_step(secret, otp.at(now), now) == step
    assert totp_step(secret, otp.at(now - 30), now) == step - 1
    assert totp_step(secret, otp.at(now + 30), now) == step + 1
    assert totp_step(secret, otp.at(now + 90), now) is None
    assert totp_step(secret, "12345", now) is None
    assert totp_step(secret, "abcdef", now) is None
    assert totp_step(secret, otp.at(now)[:3] + " " + otp.at(now)[3:], now) == step


def test_the_qr_uri_is_what_google_authenticator_reads():
    uri = provisioning_uri("JBSWY3DPEHPK3PXP", "ana")
    assert uri.startswith("otpauth://totp/Fichaitor%20backoffice:ana?")
    assert "secret=JBSWY3DPEHPK3PXP" in uri and "issuer=Fichaitor%20backoffice" in uri


def test_a_totp_secret_is_sealed_to_its_user():
    box = SecretBox(KEY)
    sealed = box.seal_totp("JBSWY3DPEHPK3PXP", "ana")
    assert "JBSWY3DPEHPK3PXP" not in sealed and len(sealed) <= 512
    assert box.open_totp(sealed, "ana") == "JBSWY3DPEHPK3PXP"
    # Moved to another user's row, or opened with another key: nothing.
    assert box.open_totp(sealed, "eva") is None
    assert SecretBox("cd" * 32).open_totp(sealed, "ana") is None
    assert box.open_totp("v1.basura", "ana") is None
    assert box.open_totp(None, "ana") is None


def test_the_enrollment_state_is_bound_to_its_link_and_expires():
    box = SecretBox(KEY)
    now = time.time()
    state = box.seal_state("JBSWY3DPEHPK3PXP", "a" * 64, now)
    assert box.open_state(state, "a" * 64, now + 60) == "JBSWY3DPEHPK3PXP"
    assert box.open_state(state, "b" * 64, now + 60) is None
    assert box.open_state(state, "a" * 64, now + 16 * 60) is None


def test_a_wrong_key_is_refused():
    with pytest.raises(ValueError):
        SecretBox("ab" * 16)


# --- recovery codes, tokens and CSRF -----------------------------------------------------


def test_ten_recovery_codes_different_and_normalized():
    codes = new_recovery_codes()
    assert len(codes) == 10 and len(set(codes)) == 10
    assert all(len(normalize_recovery_code(c)) == 12 for c in codes)
    assert normalize_recovery_code(" K7M2-q9xa 4tpz ") == "k7m2q9xa4tpz"


def test_the_csrf_token_depends_on_the_session_and_the_pepper():
    first = csrf_token(PEPPER, "sesion-1")
    assert first == csrf_token(PEPPER, "sesion-1")
    assert first != csrf_token(PEPPER, "sesion-2")
    assert first != csrf_token("otra-pimienta-distinta-de-mas-de-32-c", "sesion-1")
    assert len(token_hash("x")) == 64


# --- settings ---------------------------------------------------------------------------


def test_the_settings_need_the_url_the_key_and_the_pepper():
    with pytest.raises(AdminConfigError) as exc:
        AdminSettings().check({})
    message = str(exc.value)
    assert "FICHAITOR_ADMIN_DATABASE_URL" in message
    assert "FICHAITOR_ADMIN_TOTP_KEY" in message
    assert "FICHAITOR_ADMIN_PASSWORD_PEPPER" in message
    settings().check({})


@pytest.mark.parametrize(
    "name",
    [
        "FICHAITOR_STRIPE_SECRET_KEY",
        "FICHAITOR_ODOO_API_KEY",
        "FICHAITOR_SMTP_PASSWORD",
        "FICHAITOR_GOOGLE_CLIENT_SECRET",
        "FICHAITOR_TOKEN_PRIVATE_KEY",
    ],
)
def test_the_backoffice_refuses_to_start_with_keys_of_other_processes(name):
    with pytest.raises(AdminConfigError) as exc:
        settings().check({name: "secreto"})
    assert name in str(exc.value) and "secreto" not in str(exc.value)


def test_the_settings_never_read_the_env_file():
    assert AdminSettings.model_config.get("env_file") is None


# --- HTTP without a database: Host and headers --------------------------------------------


@pytest.fixture
def client():
    app = create_app(settings(), store=object(), environ={})
    with TestClient(app, base_url="https://localhost:8090") as test_client:
        yield test_client


@pytest.mark.parametrize(
    "host", ["evil.example.com", "localhost", "127.0.0.1", "localhost:8091", "fichaitor.es", ""]
)
def test_a_foreign_host_is_refused(client, host):
    response = client.get("/healthz", headers={"Host": host})
    assert response.status_code == 400
    assert response.json()["code"] == "host_not_allowed"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


@pytest.mark.parametrize("host", ["localhost:8090", "127.0.0.1:8090", "LOCALHOST:8090"])
def test_the_tunnel_hosts_are_accepted(client, host):
    assert client.get("/healthz", headers={"Host": host}).json() == {"ok": True}


def test_every_answer_carries_the_security_headers(client):
    for response in (client.get("/healthz"), client.get("/no-existe"), client.get("/")):
        csp = response.headers["content-security-policy"]
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert "http" not in csp.replace("'self'", "")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "access-control-allow-origin" not in response.headers
        assert "server" not in response.headers


def test_there_is_no_cors_nor_openapi(client):
    preflight = client.options(
        "/admin/api/login",
        headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in preflight.headers
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Requested-With": "XMLHttpRequest"},
        {"X-Requested-With": "fichaitor-admin", "Origin": "https://evil.example.com"},
        {"X-Requested-With": "fichaitor-admin", "Sec-Fetch-Site": "cross-site"},
    ],
)
def test_a_request_that_changes_something_must_come_from_the_page(client, headers):
    response = client.post(
        "/admin/api/login", json={"username": "ana", "password": "x"}, headers=headers
    )
    assert response.status_code == 403 and response.json()["code"] == "csrf_failed"


def test_a_validation_error_never_echoes_what_was_sent(client):
    response = client.post(
        "/admin/api/login",
        json={"username": "", "password": "mi-secreto-de-verdad"},
        headers={"X-Requested-With": "fichaitor-admin"},
    )
    assert response.status_code == 422
    assert "mi-secreto-de-verdad" not in response.text
    assert response.json()["code"] == "validation"


# --- presentation helpers --------------------------------------------------------------


def test_the_links_to_stripe_follow_the_mode():
    assert stripe_links(False)["invoice"] == "https://dashboard.stripe.com/test/invoices/{id}"
    assert stripe_links(True)["customer"] == "https://dashboard.stripe.com/customers/{id}"
    billing = with_links(
        {
            "livemode": False,
            "customer": {"stripe_customer_id": "cus_A1"},
            "seats": [{"stripe_subscription_id": "sub_B2"}],
            "charges": [{"stripe_invoice_id": "in_C3", "payment_intent_id": "pi_D4"}],
            "documents": [{"odoo_move_id": 77, "stripe_invoice_id": "in_C3"}],
        },
        "https://erp.bitclick.solutions/",
    )
    assert billing["customer"]["stripe_url"].endswith("/test/customers/cus_A1")
    assert billing["seats"][0]["stripe_url"].endswith("/test/subscriptions/sub_B2")
    assert billing["charges"][0]["payment_url"].endswith("/test/payments/pi_D4")
    assert billing["documents"][0]["odoo_url"] == (
        "https://erp.bitclick.solutions/odoo/account.move/77"
    )


def test_arr_is_the_next_price_of_the_seats_in_force_and_mrr_a_twelfth():
    revenue = recurring_revenue(
        [
            {"plan": "pro", "next_price_key": "pro_first_yearly", "seats": 1},
            {"plan": "pro", "next_price_key": "pro_additional_yearly", "seats": 2},
            {"plan": "pro_whatsapp", "next_price_key": None, "seats": 1},
        ]
    )
    assert revenue == {"arr_cents": 3500 + 2 * 3325 + 5900, "mrr_cents": 1338, "estimated_seats": 1}
