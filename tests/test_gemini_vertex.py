"""Gemini on Vertex AI against a fake transport: the URL, the authentication, the cache
of the token, the errors and what never leaks. Nothing here reaches Google."""

import asyncio
import base64
import json
import logging
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from pydantic import BaseModel, ValidationError

from app.adapters import gemini_auth
from app.adapters.gemini_auth import (
    METADATA_TOKEN_URL,
    SCOPE,
    TOKEN_URL,
    AuthorizedUserTokens,
    MetadataServerTokens,
    ServiceAccountTokens,
    VertexAuthError,
    application_default,
    from_file,
    from_info,
)
from app.adapters.gemini_client import (
    BASE_URL,
    AIStudioBackend,
    GeminiClient,
    GeminiUnavailable,
    GeminiUsage,
    VertexBackend,
    vertex_host,
)
from app.adapters.gemini_provider import gemini_credentials
from app.adapters.screenshot_reader.gemini import GeminiScreenshotReader
from app.adapters.sentiment_model.build import build_sentiment_model
from app.adapters.sentiment_model.gemini import GeminiSentimentModel
from app.screenshot_config import ScreenshotSettings
from app.worker.conversation import build_writer

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
EMAIL = "fichaitor-gemini@proyecto-pruebas.iam.gserviceaccount.com"
ACCESS_TOKEN = "ya29.token-de-pruebas-no-real"
CLIENT_SECRET = "secreto-de-cliente-no-real"
REFRESH_TOKEN = "1//refresco-no-real"
API_KEY = "clave-de-pruebas-no-real"
PROJECT = "proyecto-pruebas"
MODEL = "gemini-3.6-flash"
VERTEX_URL = (
    "https://aiplatform.eu.rep.googleapis.com/v1/projects/proyecto-pruebas/locations/eu"
    "/publishers/google/models/gemini-3.6-flash:generateContent"
)
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


class Answer(BaseModel):
    ok: bool


def sa_info(**overrides) -> dict:
    info = {
        "type": "service_account",
        "project_id": PROJECT,
        "private_key_id": "kid-123",
        "private_key": PEM,
        "client_email": EMAIL,
        "token_uri": "https://atacante.example/token",  # ignored: the endpoint is fixed
    }
    info.update(overrides)
    return info


def secrets() -> list[str]:
    """Everything that must never show up in a log, an error or a repr."""
    body = PEM.split("-----")[2].strip()
    return [ACCESS_TOKEN, CLIENT_SECRET, REFRESH_TOKEN, API_KEY, body[:40], body[-40:]]


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def model_answer(text: str = '{"ok": true}', finish: str = "STOP") -> dict:
    return {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": finish}
        ],
        "usageMetadata": {
            "promptTokenCount": 120,
            "candidatesTokenCount": 8,
            "thoughtsTokenCount": 30,
        },
    }


class FakeGoogle:
    """The token endpoint and Vertex AI, both on one fake transport."""

    def __init__(self, token_status: int = 200, expires_in: object = 3600) -> None:
        self.token_status = token_status
        self.expires_in = expires_in
        self.token_requests: list[httpx.Request] = []
        self.model_requests: list[httpx.Request] = []
        self.model_status = 200
        self.model_payload: dict = model_answer()
        self.issued = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            self.token_requests.append(request)
            if self.token_status != 200:
                return httpx.Response(
                    self.token_status,
                    json={"error": "invalid_grant", "error_description": f"bad {PEM[:60]}"},
                )
            self.issued += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"{ACCESS_TOKEN}-{self.issued}",
                    "expires_in": self.expires_in,
                    "token_type": "Bearer",
                },
            )
        self.model_requests.append(request)
        return httpx.Response(self.model_status, json=self.model_payload)

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


def vertex_client(
    google: FakeGoogle, clock: Clock | None = None, location: str = "eu"
) -> GeminiClient:
    tokens = from_info(sa_info(), clock or Clock())
    return GeminiClient(VertexBackend(PROJECT, location, tokens), MODEL, client=google.http())


def generate(client: GeminiClient, times: int = 1):
    async def run():
        return [
            await client.generate("Sistema", [{"text": "hola"}], SCHEMA, 256, Answer)
            for _ in range(times)
        ]

    return asyncio.run(run())


def b64json(part: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


# --- the request ----------------------------------------------------------------------


def test_vertex_request_has_the_same_body_as_ai_studio_and_a_bearer_token():
    google = FakeGoogle()
    ((answer, usage),) = generate(vertex_client(google))
    assert answer == Answer(ok=True)
    assert usage == GeminiUsage(input_tokens=120, output_tokens=38)

    vertex = google.model_requests[0]
    assert str(vertex.url) == VERTEX_URL
    assert vertex.headers["authorization"] == f"Bearer {ACCESS_TOKEN}-1"
    assert "x-goog-api-key" not in vertex.headers
    assert "x-goog-user-project" not in vertex.headers

    seen: list[httpx.Request] = []

    def studio(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=model_answer())

    aistudio = GeminiClient(
        API_KEY, MODEL, client=httpx.AsyncClient(transport=httpx.MockTransport(studio))
    )
    ((studio_answer, studio_usage),) = generate(aistudio)
    assert (studio_answer, studio_usage) == (answer, usage)
    assert str(seen[0].url) == f"{BASE_URL}/models/{MODEL}:generateContent"
    assert seen[0].headers["x-goog-api-key"] == API_KEY
    assert "authorization" not in seen[0].headers
    # Same structured output, same limits, same thinking level: the body is identical.
    assert json.loads(vertex.content) == json.loads(seen[0].content)
    config = json.loads(vertex.content)["generationConfig"]
    assert config["responseJsonSchema"] == SCHEMA
    assert config["responseMimeType"] == "application/json"
    assert config["maxOutputTokens"] == 256
    assert config["thinkingConfig"] == {"thinkingLevel": "LOW"}


@pytest.mark.parametrize(
    ("location", "host"),
    [
        ("eu", "aiplatform.eu.rep.googleapis.com"),
        ("us", "aiplatform.us.rep.googleapis.com"),
        ("global", "aiplatform.googleapis.com"),
        ("europe-west1", "europe-west1-aiplatform.googleapis.com"),
    ],
)
def test_the_host_follows_the_location(location, host):
    assert vertex_host(location) == host
    backend = VertexBackend(PROJECT, location, MetadataServerTokens(Clock()))
    assert backend.url("models/gemini-3.8-flash") == (
        f"https://{host}/v1/projects/{PROJECT}/locations/{location}"
        "/publishers/google/models/gemini-3.8-flash:generateContent"
    )


@pytest.mark.parametrize(
    ("project", "location"),
    [("", "eu"), ("Mayusculas-no", "eu"), ("p/../../x", "eu"), (PROJECT, "eu/../x"), (PROJECT, "")],
)
def test_project_and_location_cannot_bend_the_url(project, location):
    with pytest.raises(ValueError):
        VertexBackend(project, location, MetadataServerTokens(Clock()))


def test_same_answer_checks_as_ai_studio():
    google = FakeGoogle()
    client = vertex_client(google)
    google.model_payload = model_answer(finish="MAX_TOKENS")
    with pytest.raises(GeminiUnavailable, match="finish reason MAX_TOKENS"):
        generate(client)
    google.model_payload = model_answer(text='{"ok": "quizas"}')
    with pytest.raises(GeminiUnavailable, match="does not match the schema"):
        generate(client)
    google.model_status = 429
    with pytest.raises(GeminiUnavailable, match="status 429"):
        generate(client)
    google.model_status = 500
    with pytest.raises(GeminiUnavailable, match="status 500"):
        generate(client)
    # No retries: one request per call, as on AI Studio.
    assert len(google.model_requests) == 4
    assert len(google.token_requests) == 1


def test_timeout_and_connection_errors_are_fixed_strings():
    def slow(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={"access_token": ACCESS_TOKEN, "expires_in": 3600})
        raise httpx.ReadTimeout("timed out", request=request)

    tokens = from_info(sa_info(), Clock())
    http = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    client = GeminiClient(VertexBackend(PROJECT, "eu", tokens), MODEL, client=http)
    with pytest.raises(GeminiUnavailable, match="^timeout$"):
        generate(client)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    tokens = from_info(sa_info(), Clock())
    http = httpx.AsyncClient(transport=httpx.MockTransport(down))
    client = GeminiClient(VertexBackend(PROJECT, "eu", tokens), MODEL, client=http)
    with pytest.raises(GeminiUnavailable, match="^credentials: token endpoint connection error$"):
        generate(client)


def test_the_default_client_keeps_the_configured_timeout():
    backend = VertexBackend(PROJECT, "eu", from_info(sa_info(), Clock()))
    reader = GeminiScreenshotReader(backend, MODEL, timeout_seconds=7)
    assert reader._client.timeout == httpx.Timeout(7)
    assert reader._url == VERTEX_URL
    sentiment = GeminiSentimentModel(backend, MODEL, timeout_seconds=9)
    assert sentiment._gemini._client.timeout == httpx.Timeout(9)


# --- the signed assertion and the token ---------------------------------------------------


def test_the_assertion_is_a_jwt_signed_with_the_service_account_key():
    google = FakeGoogle()
    clock = Clock(1_800_000_000.0)
    generate(vertex_client(google, clock))

    request = google.token_requests[0]
    assert str(request.url) == TOKEN_URL  # never the token_uri of the file
    form = dict(httpx.QueryParams(request.content.decode()))
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    header, claims, signature = form["assertion"].split(".")
    assert b64json(header) == {"alg": "RS256", "typ": "JWT", "kid": "kid-123"}
    assert b64json(claims) == {
        "iss": EMAIL,
        "scope": SCOPE,
        "aud": TOKEN_URL,
        "iat": 1_800_000_000,
        "exp": 1_800_003_600,
    }
    raw = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    KEY.public_key().verify(raw, f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())


def test_the_token_is_cached_until_shortly_before_it_expires():
    google = FakeGoogle(expires_in=3600)
    clock = Clock()
    client = vertex_client(google, clock)
    generate(client, times=3)
    assert len(google.token_requests) == 1

    clock.now += 3600 - gemini_auth.REFRESH_MARGIN_SECONDS - 1
    generate(client)
    assert len(google.token_requests) == 1

    clock.now += 2  # inside the margin: renewed before it dies
    generate(client)
    assert len(google.token_requests) == 2
    assert google.model_requests[-1].headers["authorization"] == f"Bearer {ACCESS_TOKEN}-2"


def test_a_long_or_missing_lifetime_is_not_trusted():
    google = FakeGoogle(expires_in=10**9)
    clock = Clock()
    client = vertex_client(google, clock)
    generate(client)
    clock.now += gemini_auth.MAX_TOKEN_SECONDS
    generate(client)
    assert len(google.token_requests) == 2

    google = FakeGoogle(expires_in="mucho")
    clock = Clock()
    client = vertex_client(google, clock)
    generate(client)
    clock.now += gemini_auth.REFRESH_MARGIN_SECONDS + 1
    generate(client)
    assert len(google.token_requests) == 2


def test_concurrent_calls_ask_for_one_token():
    google = FakeGoogle()
    client = vertex_client(google)

    async def run():
        await asyncio.gather(
            *(client.generate("S", [{"text": "x"}], SCHEMA, 64, Answer) for _ in range(5))
        )

    asyncio.run(run())
    assert len(google.token_requests) == 1
    assert len(google.model_requests) == 5


def test_a_refused_token_is_forgotten_and_the_next_call_gets_a_new_one():
    google = FakeGoogle()
    client = vertex_client(google)
    generate(client)
    google.model_status = 401
    with pytest.raises(GeminiUnavailable, match="status 401"):
        generate(client)
    google.model_status = 200
    generate(client)
    assert len(google.token_requests) == 2


def test_a_failed_token_request_is_a_fixed_error_and_is_retried_on_the_next_call():
    google = FakeGoogle(token_status=400)
    client = vertex_client(google)
    with pytest.raises(GeminiUnavailable) as info:
        generate(client)
    assert str(info.value) == "credentials: token endpoint status 400"
    assert info.value.__cause__ is None and info.value.__suppress_context__
    assert google.model_requests == []

    google.token_status = 200
    generate(client)
    assert len(google.token_requests) == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"token_type": "Bearer", "expires_in": 3600},
        {"access_token": "", "expires_in": 3600},
        {"access_token": "dos partes", "expires_in": 3600},
        {"access_token": ACCESS_TOKEN, "token_type": "MAC"},
        ["no", "es", "un", "objeto"],
    ],
)
def test_an_unusable_token_answer_is_refused(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    tokens = from_info(sa_info(), Clock())
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(VertexAuthError):
        asyncio.run(tokens.token(http))


def test_authorized_user_refreshes_and_names_the_quota_project():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={"access_token": ACCESS_TOKEN, "expires_in": 3599})
        return httpx.Response(200, json=model_answer())

    tokens = from_info(
        {
            "type": "authorized_user",
            "client_id": "id.apps.googleusercontent.com",
            "client_secret": CLIENT_SECRET,
            "refresh_token": REFRESH_TOKEN,
            "quota_project_id": PROJECT,
        },
        Clock(),
    )
    assert isinstance(tokens, AuthorizedUserTokens)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    generate(GeminiClient(VertexBackend(PROJECT, "eu", tokens), MODEL, client=http))
    form = dict(httpx.QueryParams(seen[0].content.decode()))
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == REFRESH_TOKEN
    assert seen[1].headers["x-goog-user-project"] == PROJECT


def test_metadata_server_is_asked_with_its_header():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"access_token": ACCESS_TOKEN, "expires_in": 3599})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert asyncio.run(MetadataServerTokens(Clock()).token(http)) == ACCESS_TOKEN
    assert str(seen[0].url) == METADATA_TOKEN_URL
    assert seen[0].headers["metadata-flavor"] == "Google"


# --- the credentials file and ADC ---------------------------------------------------------


def write(tmp_path: Path, content: str | bytes, name: str = "cuenta.json") -> Path:
    path = tmp_path / name
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return path


def test_a_service_account_file_is_read(tmp_path):
    tokens = from_file(write(tmp_path, json.dumps(sa_info())))
    assert isinstance(tokens, ServiceAccountTokens)
    assert tokens.email == EMAIL
    assert repr(tokens) == "ServiceAccountTokens()"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('{"type": "service_account", "private_key": "' + PEM[:200], "not JSON"),
        (json.dumps(["x"]), "not a JSON object"),
        (json.dumps({"type": "external_account"}), "neither service_account"),
        (json.dumps(sa_info(private_key="-----BEGIN PRIVATE KEY-----\nAAAA\n")), "unreadable"),
        (json.dumps(sa_info(client_email="")), "lacks client_email"),
        (json.dumps(sa_info(private_key="")), "lacks private_key"),
        ("x" * (gemini_auth.MAX_FILE_BYTES + 1), "too big"),
    ],
)
def test_a_broken_file_says_what_is_wrong_and_never_what_it_holds(tmp_path, content, message):
    path = write(tmp_path, content)
    with pytest.raises(VertexAuthError, match=message) as info:
        from_file(path)
    assert info.value.__cause__ is None and info.value.__context__ is None
    for secret in secrets():
        assert secret not in str(info.value)


def test_a_missing_file_does_not_quote_its_path_content(tmp_path):
    with pytest.raises(VertexAuthError, match="cannot be read"):
        from_file(tmp_path / "no-existe.json")


def test_a_key_that_is_not_rsa_is_refused():
    other = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    with pytest.raises(VertexAuthError, match="not RSA"):
        from_info(sa_info(private_key=other.decode()))


def test_adc_order(tmp_path):
    sa = write(tmp_path, json.dumps(sa_info()))
    assert isinstance(
        application_default({"GOOGLE_APPLICATION_CREDENTIALS": str(sa)}), ServiceAccountTokens
    )
    gcloud = tmp_path / "gcloud"
    gcloud.mkdir()
    write(
        gcloud,
        json.dumps(
            {
                "type": "authorized_user",
                "client_id": "id",
                "client_secret": CLIENT_SECRET,
                "refresh_token": REFRESH_TOKEN,
            }
        ),
        "application_default_credentials.json",
    )
    env = {"CLOUDSDK_CONFIG": str(gcloud), "APPDATA": str(tmp_path), "HOME": str(tmp_path)}
    assert isinstance(application_default(env), AuthorizedUserTokens)
    empty = {"CLOUDSDK_CONFIG": str(tmp_path / "nada"), "APPDATA": str(tmp_path / "nada")}
    assert isinstance(application_default(empty), MetadataServerTokens)


# --- settings and the builders --------------------------------------------------------------


def settings(**kwargs) -> ScreenshotSettings:
    return ScreenshotSettings(_env_file=None, **kwargs)


def test_ai_studio_stays_the_default():
    s = settings(gemini_api_key=API_KEY)
    assert s.gemini_provider == "aistudio"
    assert s.vertex_location == "eu"
    assert gemini_credentials(s) == API_KEY
    assert gemini_credentials(settings(gemini_api_key="")) is None


def test_vertex_needs_a_project():
    with pytest.raises(ValidationError, match="FICHAITOR_VERTEX_PROJECT"):
        settings(gemini_provider="vertex")
    with pytest.raises(ValidationError):
        settings(gemini_provider="vertexai", vertex_project=PROJECT)
    # The fake model needs no provider at all.
    assert settings(gemini_provider="vertex", fake_screenshot=True).fake_screenshot


def test_vertex_is_built_from_the_settings_without_any_key(tmp_path, monkeypatch):
    monkeypatch.setenv("FICHAITOR_GEMINI_PROVIDER", "vertex")
    monkeypatch.setenv("FICHAITOR_VERTEX_PROJECT", PROJECT)
    monkeypatch.setenv(
        "FICHAITOR_VERTEX_CREDENTIALS_FILE", str(write(tmp_path, json.dumps(sa_info())))
    )
    monkeypatch.setenv("FICHAITOR_GEMINI_MODEL", "gemini-3.8-flash")
    s = ScreenshotSettings(_env_file=None)
    backend = gemini_credentials(s)
    assert isinstance(backend, VertexBackend)
    assert (backend.project, backend.location) == (PROJECT, "eu")

    sentiment = build_sentiment_model(s, 5)
    assert isinstance(sentiment, GeminiSentimentModel)
    assert sentiment._gemini._url.startswith("https://aiplatform.eu.rep.googleapis.com/v1/")
    assert sentiment._gemini._url.endswith("/models/gemini-3.8-flash:generateContent")
    writer = build_writer(s)
    assert isinstance(writer, GeminiScreenshotReader)
    assert writer._url == sentiment._gemini._url

    # Same builders, AI Studio: as before.
    monkeypatch.delenv("FICHAITOR_GEMINI_PROVIDER")
    studio = settings(gemini_api_key=API_KEY)
    assert isinstance(build_sentiment_model(studio, 5)._gemini._backend, AIStudioBackend)
    assert isinstance(build_writer(studio), GeminiScreenshotReader)
    assert build_writer(settings(gemini_api_key="")) is None


def test_vertex_with_a_missing_file_stops_at_start_up(tmp_path):
    s = settings(
        gemini_provider="vertex",
        vertex_project=PROJECT,
        vertex_credentials_file=str(tmp_path / "no-esta.json"),
    )
    with pytest.raises(VertexAuthError, match="cannot be read"):
        build_sentiment_model(s, 5)


def test_vertex_without_a_file_uses_adc(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "GOOGLE_APPLICATION_CREDENTIALS", str(write(tmp_path, json.dumps(sa_info())))
    )
    backend = gemini_credentials(settings(gemini_provider="vertex", vertex_project=PROJECT))
    assert isinstance(backend._tokens, ServiceAccountTokens)


# --- nothing leaks --------------------------------------------------------------------------


def test_no_secret_in_logs_errors_or_reprs(caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    google = FakeGoogle()
    path = write(tmp_path, json.dumps(sa_info()))
    tokens = from_file(path, Clock())
    backend = VertexBackend(PROJECT, "eu", tokens)
    client = GeminiClient(backend, MODEL, client=google.http())
    generate(client)
    google.model_status = 403
    with pytest.raises(GeminiUnavailable) as refused:
        generate(client)
    google.token_status = 401
    tokens.invalidate()
    with pytest.raises(GeminiUnavailable) as no_token:
        generate(client)

    studio = AIStudioBackend(API_KEY)
    texts = [
        caplog.text,
        str(refused.value),
        str(no_token.value),
        repr(tokens),
        repr(backend),
        repr(studio),
        repr(settings(gemini_api_key=API_KEY)),
    ]
    for text in texts:
        for secret in secrets():
            assert secret not in text
    # The token endpoint saw the signed assertion; the model, only the access token.
    assert all("assertion" not in r.content.decode() for r in google.model_requests)


# --- deployment -------------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]


def test_production_mounts_the_vertex_key_read_only_in_the_three_services():
    compose = (ROOT / "deploy" / "docker-compose.prod.yml").read_text(encoding="utf-8")
    mount = "&secreto-vertex ${FICHAITOR_ENV_DIR:-.}/vertex:/run/secrets/vertex:ro"
    assert mount in compose
    for service in ("fichaitor-api", "fichaitor-worker", "fichaitor-conversation"):
        start = compose.index(f"\n  {service}:\n")
        block = compose[start : compose.index("\n  fichaitor-", start + 1)]
        assert "      - *secreto-vertex\n" in block, service
    assert compose.count("*secreto-vertex") == 3  # not in the database nor in the web


def test_the_key_never_enters_the_image_and_the_examples_keep_ai_studio():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert {"**/vertex", "**/cuenta-servicio*.json"} <= set(ignored)
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "vertex" not in dockerfile.lower()
    for name in ("api", "worker", "conversation"):
        example = (ROOT / "deploy" / f"env.{name}.example").read_text(encoding="utf-8")
        lines = set(example.splitlines())
        assert "FICHAITOR_GEMINI_PROVIDER=aistudio" in lines, name
        assert "FICHAITOR_VERTEX_PROJECT=" in lines, name
        assert "FICHAITOR_VERTEX_LOCATION=eu" in lines, name
        assert (
            "FICHAITOR_VERTEX_CREDENTIALS_FILE=/run/secrets/vertex/cuenta-servicio.json" in lines
        ), name


def test_the_project_number_is_accepted_too():
    backend = VertexBackend("544696777336", "eu", MetadataServerTokens(Clock()))
    assert "/projects/544696777336/locations/eu/" in backend.url(MODEL)
