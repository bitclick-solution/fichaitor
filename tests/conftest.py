"""Guard of the integration tests: they only ever run against a database made for them.

The integration tests create and delete rows and try statements that must be denied.
Run against a database with real data, one mistake deletes it (it happened on
2026-11-12: see docs/retencion.md, "Pruebas"). So the three URLs must point to a
database whose name ends in `_test`; otherwise the whole run stops before any test.

Create the test database with scripts/preparar_bd_pruebas.sh.
"""

import os
from collections.abc import Mapping
from urllib.parse import urlsplit

import pytest

TEST_URL_VARIABLES = (
    "FICHAITOR_TEST_DATABASE_URL",
    "FICHAITOR_TEST_OWNER_URL",
    "FICHAITOR_TEST_WORKER_URL",
    "FICHAITOR_TEST_CONVERSATION_URL",
)
REQUIRED_SUFFIX = "_test"


def database_name(url: str) -> str:
    """Name of the database a PostgreSQL URL points to; empty if it names none."""
    try:
        return urlsplit(url).path.lstrip("/").split("/")[0]
    except ValueError:
        return ""


def unsafe_test_urls(env: Mapping[str, str]) -> list[str]:
    """Names of the variables that are set and do not point to a `..._test` database."""
    return [
        name
        for name in TEST_URL_VARIABLES
        if env.get(name) and not database_name(env[name]).endswith(REQUIRED_SUFFIX)
    ]


def guard_message(names: list[str]) -> str:
    # Only the names: the URLs carry passwords.
    return (
        "Pruebas detenidas: "
        + ", ".join(names)
        + f" no apunta a una base de datos cuyo nombre termine en «{REQUIRED_SUFFIX}». "
        "Las pruebas de integración crean y borran filas: nunca se ejecutan contra la base "
        "de desarrollo ni contra una con datos reales. Crea la de pruebas con "
        "scripts/preparar_bd_pruebas.sh y usa .../fichaitor_test en todas las variables."
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    names = unsafe_test_urls(os.environ)
    if names:
        pytest.exit(guard_message(names), returncode=2)
