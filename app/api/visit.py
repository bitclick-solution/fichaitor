"""Visits to the home page, counted without personal data (docs/contrato-web-api.md).

The page calls `POST /api/v1/public/visit` with `navigator.sendBeacon`. One `funnel_event`
of kind `visit` with, at most, where the visitor came from (`source`). Nothing else is
kept: no IP address, no user agent, no cookie, no identifier. The answer is always 204,
also when the visit is not counted (a bot, the limit, a body that does not parse), so it
tells nothing to whoever calls.

The limit per client lives only in this process. The key is an HMAC of the address with a
random key made at start and never written anywhere: the address is not kept, not even
in memory, and the key cannot be reversed to it after a restart.
"""

import hashlib
import hmac
import json
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs

from fastapi import APIRouter, Request, Response

from app.application.rate_limit import SlidingWindowLimiter

log = logging.getLogger("fichaitor.visit")

ROUTE = "/api/v1/public/visit"
MAX_BODY_BYTES = 512
VISITS_PER_HOUR = 10
SOURCE = re.compile(r"[a-z0-9_-]{1,40}")
# A simple test: a user agent that says it is a robot or a link preview, or that is not
# a browser at all (every browser in use sends one that starts with "Mozilla/").
_BOT = re.compile(r"bot|crawl|spider|preview", re.IGNORECASE)

Record = Callable[[str | None], Awaitable[None]]


def clean_source(value: object) -> str | None:
    """`source` if it is 1 to 40 characters of [a-z0-9_-] once in lower case; else None."""
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if SOURCE.fullmatch(value) else None


def source_from_body(body: bytes, content_type: str) -> str | None:
    """The source sent by the page: a JSON object (text/plain, the default of sendBeacon
    with a string, or application/json) or a form (`source=…`). Empty: None."""
    if not body:
        return None
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if content_type.split(";")[0].strip().lower() == "application/x-www-form-urlencoded":
        values = parse_qs(text, max_num_fields=5).get("source")
        return clean_source(values[0]) if values else None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return clean_source(data.get("source")) if isinstance(data, dict) else None


def is_browser(user_agent: str) -> bool:
    return user_agent.startswith("Mozilla/") and not _BOT.search(user_agent)


class VisitCounter:
    """Whether a visit counts: from a browser and under the limit of its client."""

    def __init__(self, limiter: SlidingWindowLimiter | None = None) -> None:
        self._limiter = limiter or SlidingWindowLimiter(VISITS_PER_HOUR, 3600)
        self._key = secrets.token_bytes(32)

    def _client_key(self, ip: str) -> str:
        return hmac.new(self._key, ip.encode(), hashlib.sha256).hexdigest()[:32]

    def counts(self, ip: str, user_agent: str) -> bool:
        if not is_browser(user_agent):
            return False
        return self._limiter.allow(self._client_key(ip))


def build_router(
    record: Record, client_ip: Callable[[Request], str], counter: VisitCounter | None = None
) -> APIRouter:
    router = APIRouter()
    counter = counter or VisitCounter()

    @router.post(ROUTE, status_code=204, response_class=Response)
    async def visit(request: Request) -> Response:
        done = Response(status_code=204)
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
            return done
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BODY_BYTES:
                return done
        if not counter.counts(client_ip(request), request.headers.get("user-agent", "")):
            return done
        source = source_from_body(body, request.headers.get("content-type", ""))
        try:
            await record(source)
        except Exception as exc:  # counting must never answer an error to the page
            log.warning("visita no registrada: %s", type(exc).__name__)
        return done

    return router
