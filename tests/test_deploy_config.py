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
