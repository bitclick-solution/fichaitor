"""Deployment files that carry a security decision (docs/seguridad/revision-2026-11-12.md).
Read as text: nothing is run, nothing connects anywhere."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
NGINX = (ROOT / "web" / "nginx.conf").read_text(encoding="utf-8")


def _location(prefix: str) -> str:
    """The body of the nginx location block that starts with that line."""
    start = NGINX.index(prefix)
    return NGINX[start : NGINX.index("\n    }", start)]


def test_the_deploy_never_skips_checking_the_identity_of_the_server():
    # B1: without SSH_KNOWN_HOSTS the deploy stops instead of trusting any server.
    assert "StrictHostKeyChecking no" not in DEPLOY
    assert 'comprobacion="no"' not in DEPLOY
    block = DEPLOY[DEPLOY.index("- name: Preparar SSH") :]
    block = block[: block.index("- name:", 1)]
    assert re.search(r'if \[\[ -z "\$\{SSH_KNOWN_HOSTS\}" \]\]; then\s+echo "::error::', block)
    assert "StrictHostKeyChecking yes" in block


def test_the_api_has_a_limit_of_simultaneous_requests_per_visitor():
    # M3: slow requests from one address cannot take every connection of the API.
    assert re.search(r"limit_conn_zone \$binary_remote_addr zone=api_conn_ip:", NGINX)
    assert re.search(r"limit_conn api_conn_ip \d+;", _location("location /api/ {"))


def test_the_screenshot_is_streamed_to_the_api_and_not_buffered_by_nginx():
    # M3: nginx does not hold 7 MB per upload; the API refuses an IP over its limit
    # before reading the body.
    block = _location("location = /api/v1/public/diagnosis/screenshot {")
    assert "proxy_request_buffering off;" in block
    size = re.search(r"client_body_buffer_size (\d+)k;", block)
    assert size is not None and int(size.group(1)) <= 64
    assert re.search(r"limit_conn captura_conn_ip \d;", block)


# --- second review (docs/seguridad/revision-2026-11-12-b.md) ---------------------------

DEPLOY_SH = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
COMPOSE = (ROOT / "deploy" / "docker-compose.prod.yml").read_text(encoding="utf-8")
BASH = shutil.which("bash")
# The bash of WSL (System32) does not see the paths of Windows: only a real one is used.
USABLE_BASH = BASH if BASH and "system32" not in BASH.lower() else None


def _service(name: str) -> str:
    """The block of one service of the production Compose file."""
    start = COMPOSE.index(f"\n  {name}:\n")
    following = re.search(r"\n  [a-z][a-z-]*:\n", COMPOSE[start + 1 :])
    return COMPOSE[start : start + 1 + following.start()] if following else COMPOSE[start:]


def _functions(*names: str) -> str:
    """The definitions of those shell functions in deploy.sh, as they are."""
    found = []
    for name in names:
        match = re.search(
            rf"^{name}\(\) \{{.*?^\}}$|^{name}\(\) \{{[^\n]*\}}$", DEPLOY_SH, re.M | re.S
        )
        assert match, name
        found.append(match.group(0))
    return "\n".join(found)


def _check(tmp_path, function: str, files: dict[str, str]) -> subprocess.CompletedProcess:
    """Run one check of deploy.sh on env files written in tmp_path."""
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    script = "\n".join(
        [
            "set -euo pipefail",
            _functions("log", "err", "die", "valor_env", "prohibir_modos_falsos", function),
            "API_ENV=api.env WORKER_ENV=worker.env CONVERSATION_ENV=conversation.env",
            f'{function} "${{1:-}}"',
        ]
    )
    (tmp_path / "check.sh").write_text(script, encoding="utf-8", newline="\n")
    return subprocess.run(  # noqa: S603 - a fixed script of this repository
        [USABLE_BASH, "check.sh", "api.env"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


needs_bash = pytest.mark.skipif(USABLE_BASH is None, reason="sin bash")


@pytest.mark.parametrize("service", ["fichaitor-api", "fichaitor-worker", "fichaitor-conversation"])
def test_production_fixes_every_fake_mode_to_false(service):
    # B13: whatever the env file says, no fake mode in production; the fake mailer
    # would write codes and restore links to the log.
    block = _service(service)
    assert 'FICHAITOR_FAKE_MAIL: "false"' in block
    fixed = set(re.findall(r'(FICHAITOR_FAKE_[A-Z_]+): "false"', block))
    assert {"FICHAITOR_FAKE_MAIL", "FICHAITOR_FAKE_SCREENSHOT"} <= fixed


@needs_bash
@pytest.mark.parametrize(
    "line",
    [
        "FICHAITOR_FAKE_MAIL=true",
        "FICHAITOR_FAKE_MAIL=True",
        "FICHAITOR_FAKE_MAIL=1",
        'FICHAITOR_FAKE_MAIL="yes"',
        "FICHAITOR_FAKE_ANYTHING_NEW=on",
        "FICHAITOR_FAKE_GOOGLE=true",
    ],
)
def test_deploy_refuses_any_fake_mode(tmp_path, line):
    result = _check(tmp_path, "prohibir_modos_falsos", {"api.env": f"X=1\n{line}\n"})
    assert result.returncode != 0
    assert "FICHAITOR_FAKE_" in result.stderr


@needs_bash
def test_deploy_lets_fake_modes_set_to_false_through(tmp_path):
    text = "FICHAITOR_FAKE_MAIL=false\nFICHAITOR_FAKE_GOOGLE=0\nFICHAITOR_FAKE_PLACES=\n"
    assert _check(tmp_path, "prohibir_modos_falsos", {"api.env": text}).returncode == 0


SECRET = "a" * 32
PEPPER = "b" * 32


@needs_bash
@pytest.mark.parametrize(
    ("api", "conversation", "ok"),
    [
        # I10: the pepper must exist and be long, the webhook secret too.
        (
            f"FICHAITOR_TELEGRAM_WEBHOOK_SECRET={SECRET}\nFICHAITOR_LINK_CODE_PEPPER={PEPPER}\n",
            f"FICHAITOR_LINK_CODE_PEPPER={PEPPER}\n",
            True,
        ),
        (
            f"FICHAITOR_TELEGRAM_WEBHOOK_SECRET={SECRET}\nFICHAITOR_LINK_CODE_PEPPER=\n",
            "FICHAITOR_LINK_CODE_PEPPER=\n",
            False,
        ),
        (
            f"FICHAITOR_TELEGRAM_WEBHOOK_SECRET={SECRET}\nFICHAITOR_LINK_CODE_PEPPER={PEPPER[:31]}\n",
            f"FICHAITOR_LINK_CODE_PEPPER={PEPPER[:31]}\n",
            False,
        ),
        (
            f"FICHAITOR_TELEGRAM_WEBHOOK_SECRET={SECRET}\nFICHAITOR_LINK_CODE_PEPPER={PEPPER}\n",
            f"FICHAITOR_LINK_CODE_PEPPER={'c' * 32}\n",
            False,
        ),
        (
            f"FICHAITOR_TELEGRAM_WEBHOOK_SECRET={SECRET[:31]}\nFICHAITOR_LINK_CODE_PEPPER={PEPPER}\n",
            f"FICHAITOR_LINK_CODE_PEPPER={PEPPER}\n",
            False,
        ),
        (f"FICHAITOR_LINK_CODE_PEPPER={PEPPER}\n", f"FICHAITOR_LINK_CODE_PEPPER={PEPPER}\n", False),
        # I22: production has no long polling (the Compose forces it off and the webhook is
        # always on): asking for it is refused, and the secret is measured anyway.
        (
            f"FICHAITOR_LINK_CODE_PEPPER={PEPPER}\n",
            f"FICHAITOR_LINK_CODE_PEPPER={PEPPER}\nFICHAITOR_TELEGRAM_POLLING=true\n",
            False,
        ),
        (
            f"FICHAITOR_TELEGRAM_WEBHOOK_SECRET={SECRET}\nFICHAITOR_LINK_CODE_PEPPER={PEPPER}\n",
            f"FICHAITOR_LINK_CODE_PEPPER={PEPPER}\nFICHAITOR_TELEGRAM_POLLING=1\n",
            False,
        ),
        (
            f"FICHAITOR_TELEGRAM_WEBHOOK_SECRET={SECRET}\nFICHAITOR_LINK_CODE_PEPPER={PEPPER}\n",
            f"FICHAITOR_LINK_CODE_PEPPER={PEPPER}\nFICHAITOR_TELEGRAM_POLLING=false\n",
            True,
        ),
    ],
)
def test_deploy_requires_long_telegram_secrets(tmp_path, api, conversation, ok):
    result = _check(
        tmp_path,
        "comprobar_secretos_telegram",
        {"api.env": api, "conversation.env": conversation, "worker.env": ""},
    )
    assert (result.returncode == 0) is ok, result.stderr
    assert SECRET not in result.stderr and PEPPER not in result.stderr


def test_the_webhook_route_has_a_limit_per_address_and_a_small_body():
    # I10: Telegram sends from few addresses, so the limit is high; it still exists.
    block = _location("location ^~ /api/v1/webhooks/ {")
    assert re.search(r"limit_conn webhook_conn_ip \d+;", block)
    assert re.search(r"limit_req zone=webhook_ip burst=\d+ nodelay;", block)
    assert "client_max_body_size 64k;" in block
    assert re.search(r"limit_conn_zone \$binary_remote_addr zone=webhook_conn_ip:", NGINX)
    assert re.search(r"limit_req_zone \$binary_remote_addr zone=webhook_ip:", NGINX)


# --- third review (docs/seguridad/revision-2026-11-12-c.md) ----------------------------


def test_only_main_or_a_version_tag_reaches_the_deploy_job():
    # I19: a workflow_dispatch from another branch runs that branch's deploy.yml, so this
    # is not the real barrier (the rules of the environment `production` are); it stops
    # the honest mistake.
    block = DEPLOY[DEPLOY.index("\n  desplegar:\n") :]
    block = block[: block.index("    steps:")]
    condition = re.search(r"^    if: (.+)$", block, re.M)
    assert condition is not None
    assert "github.ref == 'refs/heads/main'" in condition.group(1)
    assert "startsWith(github.ref, 'refs/tags/v')" in condition.group(1)
    assert "environment: production" in block


def test_the_email_leave_tool_runs_in_a_one_off_container_with_only_db_env():
    # I20: not inside the worker; the owner's password only in a container of its own.
    block = _service("fichaitor-baja")
    assert "profiles:" in block and "herramientas" in block
    assert "db.env" in block
    for other in ("api.env", "worker.env", "conversation.env"):
        assert other not in block
    assert 'restart: "no"' in block
    assert "scripts/baja_manual.py" in block
    assert "FICHAITOR_OWNER_DB_HOST: fichaitor-db" in block
    assert "read_only: true" in block and "no-new-privileges" in COMPOSE
    assert "FICHAITOR_OWNER_DATABASE_URL" not in _service("fichaitor-worker")


# --- paywall (2026-11-13) --------------------------------------------------------------


def test_the_api_is_stopped_with_the_conversation_before_the_schema_and_restarted_on_failure():
    # The old API does not know the new schema: it is stopped while the schema is applied
    # and started again, with the old version, if the schema fails.
    body = _functions("do_deploy")
    stop = body.index('compose stop "${API_CONTAINER}" fichaitor-conversation')
    apply = body.index("if ! aplicar_esquema; then")
    restart = body.index('compose start "${API_CONTAINER}" fichaitor-conversation')
    assert stop < apply < restart
    assert 'API_CONTAINER="fichaitor-api"' in DEPLOY_SH


def test_the_schema_includes_billing_and_the_backoffice_in_one_transaction_in_order():
    assert (
        "ESQUEMAS=(schema.sql schema_worker.sql schema_channels.sql schema_billing.sql "
        "schema_admin.sql)" in DEPLOY_SH
    )
    assert "--single-transaction" in _functions("aplicar_esquema")


@needs_bash
@pytest.mark.parametrize(
    ("api", "conversation", "ok"),
    [
        (f"FICHAITOR_ALLOWANCE_PEPPER={PEPPER}\n", f"FICHAITOR_ALLOWANCE_PEPPER={PEPPER}\n", True),
        ("FICHAITOR_ALLOWANCE_PEPPER=\n", "FICHAITOR_ALLOWANCE_PEPPER=\n", False),
        (
            f"FICHAITOR_ALLOWANCE_PEPPER={PEPPER[:31]}\n",
            f"FICHAITOR_ALLOWANCE_PEPPER={PEPPER[:31]}\n",
            False,
        ),
        (
            f"FICHAITOR_ALLOWANCE_PEPPER={PEPPER}\n",
            f"FICHAITOR_ALLOWANCE_PEPPER={'c' * 32}\n",
            False,
        ),
        (f"FICHAITOR_ALLOWANCE_PEPPER={PEPPER}\n", "", False),
    ],
)
def test_deploy_requires_the_same_long_allowance_pepper(tmp_path, api, conversation, ok):
    result = _check(
        tmp_path,
        "comprobar_pimienta_gratis",
        {"api.env": api, "conversation.env": conversation, "worker.env": ""},
    )
    assert (result.returncode == 0) is ok, result.stderr
    assert PEPPER not in result.stderr


@needs_bash
@pytest.mark.parametrize(
    ("api", "worker", "ok"),
    [
        (
            f"FICHAITOR_UNSUBSCRIBE_SECRET={PEPPER}\n",
            f"FICHAITOR_UNSUBSCRIBE_SECRET={PEPPER}\n",
            True,
        ),
        ("FICHAITOR_UNSUBSCRIBE_SECRET=\n", "FICHAITOR_UNSUBSCRIBE_SECRET=\n", False),
        (
            f"FICHAITOR_UNSUBSCRIBE_SECRET={PEPPER[:31]}\n",
            f"FICHAITOR_UNSUBSCRIBE_SECRET={PEPPER[:31]}\n",
            False,
        ),
        (
            f"FICHAITOR_UNSUBSCRIBE_SECRET={PEPPER}\n",
            f"FICHAITOR_UNSUBSCRIBE_SECRET={'c' * 32}\n",
            False,
        ),
        (f"FICHAITOR_UNSUBSCRIBE_SECRET={PEPPER}\n", "", False),
    ],
)
def test_deploy_requires_the_same_long_unsubscribe_secret(tmp_path, api, worker, ok):
    result = _check(
        tmp_path,
        "comprobar_secreto_baja",
        {"api.env": api, "worker.env": worker, "conversation.env": ""},
    )
    assert (result.returncode == 0) is ok, result.stderr
    assert PEPPER not in result.stderr


def test_the_env_examples_carry_the_unsubscribe_secret_in_the_api_and_the_worker():
    for name in ("env.api.example", "env.worker.example"):
        assert "FICHAITOR_UNSUBSCRIBE_SECRET=" in (ROOT / "deploy" / name).read_text(
            encoding="utf-8"
        )
    for name in ("env.conversation.example", "env.admin.example", "env.billing.example"):
        text = (ROOT / "deploy" / name).read_text(encoding="utf-8")
        assert "FICHAITOR_UNSUBSCRIBE_SECRET" not in text


TEST_KEY = "rk_test_" + "a" * 24
LIVE_KEY = "rk_live_" + "b" * 24
BILLING_OK = f"FICHAITOR_STRIPE_SECRET_KEY={TEST_KEY}\n"


def _cobro(tmp_path, files: dict[str, str]) -> subprocess.CompletedProcess:
    files = {"api.env": "", "conversation.env": "", "worker.env": "", "admin.env": "", **files}
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    script = "\n".join(
        [
            "set -euo pipefail",
            _functions("log", "err", "die", "valor_env", "exigir_variables", "comprobar_cobro"),
            "API_ENV=api.env WORKER_ENV=worker.env CONVERSATION_ENV=conversation.env",
            "BILLING_ENV=billing.env ADMIN_ENV=admin.env",
            "comprobar_cobro",
        ]
    )
    (tmp_path / "check.sh").write_text(script, encoding="utf-8", newline="\n")
    return subprocess.run(  # noqa: S603 - a fixed script of this repository
        [USABLE_BASH, "check.sh"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@needs_bash
@pytest.mark.parametrize(
    ("api", "billing", "ok"),
    [
        ("", BILLING_OK, True),
        ("FICHAITOR_STRIPE_WEBHOOK_SECRET=whsec_" + "a" * 32 + "\n", BILLING_OK, True),
        ("FICHAITOR_STRIPE_WEBHOOK_SECRET=secreto\n", BILLING_OK, False),
        (f"FICHAITOR_STRIPE_SECRET_KEY={TEST_KEY}\n", BILLING_OK, False),
        ("FICHAITOR_ODOO_API_KEY=x\n", BILLING_OK, False),
        ("", "", False),  # the billing process needs its key
        ("", "FICHAITOR_STRIPE_SECRET_KEY=no-es-una-clave\n", False),
        ("", f"FICHAITOR_STRIPE_SECRET_KEY={LIVE_KEY}\n", False),
        ("", f"FICHAITOR_STRIPE_SECRET_KEY={LIVE_KEY}\nFICHAITOR_STRIPE_LIVEMODE=true\n", True),
        ("", f"FICHAITOR_STRIPE_SECRET_KEY={TEST_KEY}\nFICHAITOR_STRIPE_LIVEMODE=true\n", False),
        ("", BILLING_OK + "FICHAITOR_ODOO_URL=https://erp.example\n", False),  # incomplete
    ],
)
def test_deploy_keeps_the_stripe_key_out_of_the_api_and_live_keys_explicit(
    tmp_path, api, billing, ok
):
    result = _cobro(tmp_path, {"api.env": api, "billing.env": billing})
    assert (result.returncode == 0) is ok, result.stderr
    assert LIVE_KEY not in result.stderr and TEST_KEY not in result.stderr


@needs_bash
@pytest.mark.parametrize(
    "admin",
    [
        "FICHAITOR_STRIPE_SECRET_KEY=\n",
        "FICHAITOR_ODOO_URL=https://erp.example\n",
        "STRIPE_API_KEY=x\n",
        "export FICHAITOR_ODOO_API_KEY=x\n",
    ],
)
def test_the_backoffice_env_carries_nothing_of_stripe_nor_odoo(tmp_path, admin):
    result = _cobro(tmp_path, {"billing.env": BILLING_OK, "admin.env": admin})
    assert result.returncode != 0 and "admin.env" in result.stderr


@needs_bash
def test_a_complete_odoo_in_billing_env_passes(tmp_path):
    odoo = "".join(
        f"{name}=1\n"
        for name in (
            "FICHAITOR_ODOO_URL",
            "FICHAITOR_ODOO_DB",
            "FICHAITOR_ODOO_API_KEY",
            "FICHAITOR_ODOO_JOURNAL_ID",
            "FICHAITOR_ODOO_PAYMENT_JOURNAL_ID",
            "FICHAITOR_ODOO_TAX_IVA_21",
            "FICHAITOR_ODOO_TAX_NO_SUJETO",
            "FICHAITOR_ODOO_TAX_ISP_UE",
            "FICHAITOR_ODOO_TAX_EXTRACOMUNITARIO",
            "FICHAITOR_ODOO_PRODUCT_PRO",
            "FICHAITOR_ODOO_PRODUCT_PRO_WHATSAPP",
        )
    )
    admin = "FICHAITOR_ADMIN_DATABASE_URL=x\nFICHAITOR_ADMIN_TOTP_KEY=y\n"
    result = _cobro(tmp_path, {"billing.env": BILLING_OK + odoo, "admin.env": admin})
    assert result.returncode == 0, result.stderr


def _code(block: str) -> str:
    """A block of the Compose file without its comments."""
    return "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))


def test_billing_runs_in_the_internal_network_only_and_without_fake_modes():
    block = _code(_service("fichaitor-billing"))
    assert 'command: ["python", "-m", "app.billing"]' in block
    assert "billing.env" in block and "bitclick-network" not in block
    assert "ports:" not in block
    assert 'FICHAITOR_FAKE_STRIPE: "false"' in block and 'FICHAITOR_FAKE_ODOO: "false"' in block
    for other in ("fichaitor-api", "fichaitor-worker", "fichaitor-conversation", "fichaitor-web"):
        assert "billing.env" not in _code(_service(other))


def test_the_backoffice_listens_only_on_the_loopback_of_the_server():
    block = _code(_service("fichaitor-admin"))
    assert re.findall(r'^\s+- "([^"]+)"$', block.split("ports:")[1], re.M)[0] == (
        "127.0.0.1:8090:8090"
    )
    assert "bitclick-network" not in block and "admin.env" in block
    assert (
        re.search(r"\b(?:0\.0\.0\.0:)?8090:8090", COMPOSE.replace("127.0.0.1:8090:8090", ""))
        is None
    )
    assert "STRIPE" not in block and "ODOO" not in block
    # Every other published port of production: none.
    published = re.findall(r'^\s+- "?([0-9.:]+:[0-9]+)"?$', COMPOSE, re.M)
    assert published == ["127.0.0.1:8090:8090"]


def test_deploy_requires_and_checks_the_billing_and_backoffice_env_files():
    assert 'exigir_privado "${BILLING_ENV}"' in DEPLOY_SH
    assert 'exigir_privado "${ADMIN_ENV}"' in DEPLOY_SH
    assert 'prohibir_modos_falsos "${BILLING_ENV}"' in DEPLOY_SH
    assert 'prohibir_modos_falsos "${ADMIN_ENV}"' in DEPLOY_SH
    assert 'comprobar_worker "${BILLING_CONTAINER}"' in DEPLOY_SH
    for name in ("env.billing.example", "env.admin.example"):
        assert (ROOT / "deploy" / name).exists()
    admin = (ROOT / "deploy" / "env.admin.example").read_text(encoding="utf-8")
    assert not re.search(r"^[A-Z0-9_]*(STRIPE_|ODOO_)", admin, re.M)
    billing = (ROOT / "deploy" / "env.billing.example").read_text(encoding="utf-8")
    assert "FICHAITOR_STRIPE_SECRET_KEY=\n" in billing and "FICHAITOR_ODOO_API_KEY=\n" in billing


def test_the_deploy_workflow_copies_every_schema_the_deploy_applies():
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    for name in ("schema_billing.sql", "schema_admin.sql"):
        assert workflow.count(f"db/{name}") >= 2, name


def test_init_roles_creates_the_billing_and_backoffice_users_only_with_their_env_files():
    init = (ROOT / "deploy" / "init-roles.sh").read_text(encoding="utf-8")
    assert 'if [[ -f "${DEPLOY_DIR}/billing.env" ]]; then' in init
    assert "preparar_usuario fichaitor_billing_prod fichaitor_billing" in init
    assert 'if [[ -f "${DEPLOY_DIR}/admin.env" ]]; then' in init
    assert "preparar_usuario fichaitor_admin_prod fichaitor_admin" in init


def test_the_env_examples_carry_the_allowance_pepper_where_it_is_used():
    for name in ("env.api.example", "env.conversation.example"):
        assert "FICHAITOR_ALLOWANCE_PEPPER=" in (ROOT / "deploy" / name).read_text(encoding="utf-8")
    worker = (ROOT / "deploy" / "env.worker.example").read_text(encoding="utf-8")
    assert "FICHAITOR_ALLOWANCE_PEPPER" not in worker
    assert "FICHAITOR_STRIPE_SECRET_KEY" not in (ROOT / "deploy" / "env.api.example").read_text(
        encoding="utf-8"
    )


# --- WhatsApp (docs/canales.md, «WhatsApp») --------------------------------------------

WA_ID = "106540352242922"
WA_SECRET = "f" * 32
WA_VERIFY = "v" * 64
WA_TOKEN = "EAAG" + "t" * 196
WA_API = (
    f"FICHAITOR_WHATSAPP_PHONE_NUMBER_ID={WA_ID}\nFICHAITOR_WHATSAPP_APP_SECRET={WA_SECRET}\n"
    f"FICHAITOR_WHATSAPP_VERIFY_TOKEN={WA_VERIFY}\n"
    "FICHAITOR_WHATSAPP_DISPLAY_NUMBER=+34 600 000 000\n"
)
WA_CONVERSATION = (
    f"FICHAITOR_WHATSAPP_PHONE_NUMBER_ID={WA_ID}\nFICHAITOR_WHATSAPP_ACCESS_TOKEN={WA_TOKEN}\n"
)


@needs_bash
@pytest.mark.parametrize(
    ("api", "conversation", "worker", "ok"),
    [
        ("", "", "", True),  # WhatsApp off
        (WA_API, WA_CONVERSATION, "", True),
        (WA_API, "", "", False),  # no token where it sends
        ("", WA_CONVERSATION, "", False),  # nothing receives
        (WA_API.replace(WA_SECRET, WA_SECRET[:31]), WA_CONVERSATION, "", False),
        (WA_API.replace(WA_VERIFY, "corto"), WA_CONVERSATION, "", False),
        (WA_API, WA_CONVERSATION.replace(WA_TOKEN, WA_TOKEN[:99]), "", False),
        (WA_API, WA_CONVERSATION.replace(WA_ID, "999999999"), "", False),
        (WA_API.replace("+34 600 000 000", "sin número"), WA_CONVERSATION, "", False),
        # Each secret only in its process.
        (WA_API + f"FICHAITOR_WHATSAPP_ACCESS_TOKEN={WA_TOKEN}\n", WA_CONVERSATION, "", False),
        (WA_API, WA_CONVERSATION + f"FICHAITOR_WHATSAPP_APP_SECRET={WA_SECRET}\n", "", False),
        (WA_API, WA_CONVERSATION + f"FICHAITOR_WHATSAPP_VERIFY_TOKEN={WA_VERIFY}\n", "", False),
        (WA_API, WA_CONVERSATION, f"FICHAITOR_WHATSAPP_ACCESS_TOKEN={WA_TOKEN}\n", False),
    ],
)
def test_deploy_requires_whatsapp_complete_split_by_process_and_long(
    tmp_path, api, conversation, worker, ok
):
    result = _check(
        tmp_path,
        "comprobar_whatsapp",
        {"api.env": api, "conversation.env": conversation, "worker.env": worker},
    )
    assert (result.returncode == 0) is ok, result.stderr
    for secret in (WA_SECRET, WA_VERIFY, WA_TOKEN):
        assert secret not in result.stderr


def test_the_env_examples_split_whatsapp_by_process():
    api = (ROOT / "deploy" / "env.api.example").read_text(encoding="utf-8")
    conversation = (ROOT / "deploy" / "env.conversation.example").read_text(encoding="utf-8")
    worker = (ROOT / "deploy" / "env.worker.example").read_text(encoding="utf-8")
    local = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("APP_SECRET", "VERIFY_TOKEN", "DISPLAY_NUMBER", "PHONE_NUMBER_ID"):
        assert f"\nFICHAITOR_WHATSAPP_{name}=" in api
    for name in ("ACCESS_TOKEN", "PHONE_NUMBER_ID"):
        assert f"\nFICHAITOR_WHATSAPP_{name}=" in conversation
    assert "\nFICHAITOR_WHATSAPP_ACCESS_TOKEN=" not in api
    assert "\nFICHAITOR_WHATSAPP_APP_SECRET=" not in conversation
    assert "FICHAITOR_WHATSAPP" not in worker
    for name in ("PHONE_NUMBER_ID", "ACCESS_TOKEN", "APP_SECRET", "VERIFY_TOKEN", "DISPLAY_NUMBER"):
        assert f"\nFICHAITOR_WHATSAPP_{name}=" in local
