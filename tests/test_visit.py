"""The visit counter of the home page (app/api/visit.py, docs/contrato-web-api.md): always
204, one `visit` event with a clean source or none, no address nor user agent kept, bots
and non-browsers left out and a limit per client."""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.places.fake import FakePlaces
from app.adapters.store.memory import MemoryStore
from app.api.main import create_app
from app.api.visit import (
    MAX_BODY_BYTES,
    ROUTE,
    VISITS_PER_HOUR,
    VisitCounter,
    build_router,
    clean_source,
)
from app.application.rate_limit import SlidingWindowLimiter
from app.config import Settings

BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"


def make(ip: str = "203.0.113.7", counter: VisitCounter | None = None):
    recorded: list[str | None] = []

    async def record(source: str | None) -> None:
        recorded.append(source)

    app = FastAPI()
    app.include_router(build_router(record, lambda _request: ip, counter))
    return TestClient(app), recorded


def beacon(
    client: TestClient, body: bytes | str = b"", content_type: str = "text/plain", ua=BROWSER
):
    return client.post(
        ROUTE,
        content=body,
        headers={"content-type": content_type, "user-agent": ua},
    )


def test_a_beacon_with_json_in_text_plain_counts_with_its_source():
    client, recorded = make()
    r = beacon(client, json.dumps({"source": "Instagram"}))
    assert r.status_code == 204 and r.content == b""
    assert "set-cookie" not in r.headers
    assert recorded == ["instagram"]


def test_a_form_body_and_an_empty_body_also_count():
    client, recorded = make()
    assert beacon(client, "source=qr_bar-1", "application/x-www-form-urlencoded").status_code == 204
    assert beacon(client, b"").status_code == 204
    assert recorded == ["qr_bar-1", None]


def test_a_source_out_of_the_pattern_is_stored_as_null():
    client, recorded = make()
    for bad in ("a" * 41, "con espacio", "x@y.es", "punto.es", "", "ñandú"):
        beacon(client, json.dumps({"source": bad}))
    beacon(client, "no es json")
    beacon(client, json.dumps(["source"]))
    assert recorded == [None] * 8
    assert clean_source(" Google ") == "google" and clean_source(7) is None


def test_bots_and_clients_that_are_not_browsers_are_not_counted_but_get_204():
    client, recorded = make()
    for ua in (
        "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        "Mozilla/5.0 (compatible; AhrefsSpider)",
        "Mozilla/5.0 facebookexternalhit crawler",
        "Mozilla/5.0 (Macintosh) WhatsApp link Preview",
        "curl/8.4.0",
        "python-httpx/0.27",
        "",
    ):
        assert beacon(client, b"", ua=ua).status_code == 204
    assert recorded == []


def test_the_limit_per_client_stops_inflating_the_count():
    client, recorded = make()
    for _ in range(VISITS_PER_HOUR + 5):
        assert beacon(client).status_code == 204
    assert len(recorded) == VISITS_PER_HOUR


def test_another_client_has_its_own_limit_and_the_address_is_not_kept():
    limiter = SlidingWindowLimiter(1, 3600)
    counter = VisitCounter(limiter)
    first, recorded_a = make("198.51.100.1", counter)
    second, recorded_b = make("198.51.100.2", counter)
    beacon(first)
    beacon(first)
    beacon(second)
    assert len(recorded_a) == 1 and len(recorded_b) == 1
    kept = " ".join(limiter._hits)  # noqa: SLF001
    assert "198.51.100" not in kept


def test_a_body_too_large_is_dropped():
    client, recorded = make()
    assert beacon(client, b"x" * (MAX_BODY_BYTES + 1)).status_code == 204
    assert recorded == []


def test_a_store_that_fails_still_answers_204():
    async def broken(_source):
        raise RuntimeError("base caída")

    app = FastAPI()
    app.include_router(build_router(broken, lambda _r: "203.0.113.9"))
    assert TestClient(app).post(ROUTE, headers={"user-agent": BROWSER}).status_code == 204


def test_the_api_mounts_the_route_and_stores_only_kind_and_source():
    store = MemoryStore()
    client = TestClient(create_app(Settings(_env_file=None), FakePlaces(), store))
    r = client.post(
        ROUTE,
        content=json.dumps({"source": "tiktok"}),
        headers={"content-type": "text/plain;charset=UTF-8", "user-agent": BROWSER},
    )
    assert r.status_code == 204
    assert store.events == [("visit", "tiktok", None, None)]
