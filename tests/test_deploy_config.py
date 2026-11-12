"""Deployment files that carry a security decision (docs/seguridad/revision-2026-11-12.md).
Read as text: nothing is run, nothing connects anywhere."""

import re
from pathlib import Path

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
