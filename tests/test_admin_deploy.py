"""Deployment of the backoffice (paso 12 of docs/admin-facturacion.md): it starts with the
rest (no profile), it is published only on the loopback of the server, it has a health
check, its users are managed from a one-off container with only db.env, and deploy.sh
checks admin.env."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = (ROOT / "deploy" / "docker-compose.prod.yml").read_text(encoding="utf-8")
DEPLOY_SH = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
BASH = shutil.which("bash")
USABLE_BASH = BASH if BASH and "system32" not in BASH.lower() else None
needs_bash = pytest.mark.skipif(USABLE_BASH is None, reason="sin bash")


def _service(name: str) -> str:
    start = COMPOSE.index(f"\n  {name}:\n")
    following = re.search(r"\n  [a-z][a-z-]*:\n", COMPOSE[start + 1 :])
    return COMPOSE[start : start + 1 + following.start()] if following else COMPOSE[start:]


def _code(block: str) -> str:
    return "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))


def _functions(*names: str) -> str:
    found = []
    for name in names:
        match = re.search(rf"^{name}\(\) \{{.*?^\}}$", DEPLOY_SH, re.M | re.S)
        assert match, name
        found.append(match.group(0))
    return "\n".join(found)


def test_the_backoffice_starts_with_the_rest_and_only_on_the_loopback():
    block = _code(_service("fichaitor-admin"))
    assert "profiles:" not in block
    assert 'command: ["python", "-m", "app.admin"]' in block
    assert re.findall(r'^\s+- "([^"]+)"$', block.split("ports:")[1], re.M)[0] == (
        "127.0.0.1:8090:8090"
    )
    assert "bitclick-network" not in block and "fichaitor-interna" in block
    assert "admin.env" in block and "db.env" not in block
    for other in ("api.env", "worker.env", "conversation.env", "billing.env"):
        assert other not in block
    assert "read_only: true" in block
    assert "http://127.0.0.1:8090/healthz" in block and "disable: true" not in block
    # Nothing else of production listens on 8090, and nothing on 0.0.0.0.
    rest = COMPOSE.replace("127.0.0.1:8090:8090", "")
    assert re.search(r"\b8090:8090\b", rest) is None
    assert "0.0.0.0" not in _code(COMPOSE)


def test_the_users_of_the_backoffice_are_managed_from_a_one_off_container():
    block = _code(_service("fichaitor-admin-usuario"))
    assert 'profiles: ["herramientas"]' in block
    assert 'entrypoint: ["python", "scripts/admin_usuario.py"]' in block
    assert 'restart: "no"' in block and "ports:" not in block
    assert "db.env" in block and "FICHAITOR_OWNER_DB_HOST: fichaitor-db" in block
    for other in ("api.env", "worker.env", "conversation.env", "billing.env", "admin.env"):
        assert other not in block
    assert "COPY scripts/admin_usuario.py ./scripts/admin_usuario.py" in DOCKERFILE
    # The process of the backoffice never has the owner's password.
    assert "FICHAITOR_OWNER_DATABASE_URL" not in _service("fichaitor-admin")


def test_deploy_waits_for_the_backoffice_and_checks_its_image():
    body = _functions("health_check")
    assert 'esperar_salud "${ADMIN_CONTAINER}"' in body
    assert '"$(imagen_en_uso "${ADMIN_CONTAINER}")"' in body
    assert 'ADMIN_CONTAINER="fichaitor-admin"' in DEPLOY_SH
    assert "comprobar_backoffice" in _functions("comprobar_entorno")


def _backoffice_check(tmp_path, admin: str) -> subprocess.CompletedProcess:
    (tmp_path / "admin.env").write_text(admin, encoding="utf-8")
    script = "\n".join(
        [
            "set -euo pipefail",
            _functions("log", "err", "die", "valor_env", "exigir_variables"),
            _functions("comprobar_backoffice"),
            "ADMIN_ENV=admin.env",
            "comprobar_backoffice",
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


KEY = "0123456789abcdef" * 4  # gitleaks:allow (clave falsa de pruebas)
PEPPER = "f" * 64
GOOD = (
    f"FICHAITOR_ADMIN_DATABASE_URL=x\nFICHAITOR_ADMIN_TOTP_KEY={KEY}\n"
    f"FICHAITOR_ADMIN_PASSWORD_PEPPER={PEPPER}\nFICHAITOR_ADMIN_AUTHSERV_ID=mx.example\n"
)


@needs_bash
@pytest.mark.parametrize(
    ("admin", "ok"),
    [
        (GOOD, True),
        (GOOD.replace(f"FICHAITOR_ADMIN_TOTP_KEY={KEY}", "FICHAITOR_ADMIN_TOTP_KEY="), False),
        (GOOD.replace(KEY, "corta"), False),
        (GOOD.replace(PEPPER, "pimienta"), False),
        (GOOD.replace(PEPPER, KEY), False),
        (GOOD.replace("FICHAITOR_ADMIN_AUTHSERV_ID=mx.example", ""), False),
        (GOOD + "FICHAITOR_SMTP_PASSWORD=x\n", False),
        (GOOD + "FICHAITOR_GOOGLE_CLIENT_SECRET=\n", False),
    ],
)
def test_deploy_checks_the_secrets_of_admin_env(tmp_path, admin, ok):
    result = _backoffice_check(tmp_path, admin)
    assert (result.returncode == 0) is ok, result.stderr
    assert KEY not in result.stderr and PEPPER not in result.stderr


def test_the_env_example_of_the_backoffice_has_what_the_process_reads():
    example = (ROOT / "deploy" / "env.admin.example").read_text(encoding="utf-8")
    for name in (
        "FICHAITOR_ADMIN_DATABASE_URL",
        "FICHAITOR_ADMIN_TOTP_KEY",
        "FICHAITOR_ADMIN_PASSWORD_PEPPER",
        "FICHAITOR_ADMIN_AUTHSERV_ID",
    ):
        assert re.search(rf"^{name}=$", example, re.M), name
    assert not re.search(r"^[A-Z0-9_]*(STRIPE_|ODOO_|SMTP|GOOGLE)", example, re.M)
