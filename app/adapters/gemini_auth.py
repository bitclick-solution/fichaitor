"""Access tokens of Google Cloud for Vertex AI, without the client libraries.

Three sources, as Application Default Credentials (ADC) finds them:

- a service account key (JSON): a JWT signed with its private key (RS256) is exchanged
  at `https://oauth2.googleapis.com/token` for an access token (OAuth 2.0, JWT bearer);
- an `authorized_user` file (`gcloud auth application-default login`): its refresh token
  is exchanged at the same endpoint;
- the metadata server, when the process runs on Google Cloud.

The token is cached until shortly before it expires; one refresh at a time.

Nothing here is logged or printed: not the file, not the key, not the assertion, not the
token. The errors carry fixed strings, never the provider's answer nor the file's text
(a JSON error quotes the document it failed on). The token endpoint is fixed: a
`token_uri` in the file is ignored, so a tampered file cannot send the signed
assertion elsewhere.
"""

import asyncio
import base64
import json
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/cloud-platform"
METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
# The assertion may live one hour at most (Google's limit).
ASSERTION_LIFETIME_SECONDS = 3600
# A token is renewed this long before it expires, so a call never carries a dead one.
REFRESH_MARGIN_SECONDS = 300
# Whatever the answer says, a token is not kept longer than this.
MAX_TOKEN_SECONDS = 3600
# A credentials file is a few KB; anything bigger is not one.
MAX_FILE_BYTES = 64_000


class VertexAuthError(Exception):
    """No token. The message is a fixed string: never the file, the key or the answer."""


class TokenSource(Protocol):
    @property
    def quota_project(self) -> str | None: ...

    async def token(self, client: httpx.AsyncClient) -> str: ...

    def invalidate(self) -> None: ...


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class _CachedToken:
    """The cache and the lock shared by the three sources."""

    quota_project: str | None = None

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:  # never the key nor the token
        return f"{type(self).__name__}()"

    def invalidate(self) -> None:
        """Forget the token (the API refused it): the next call asks for a new one."""
        self._token = None
        self._expires_at = 0.0

    def _fresh(self) -> str | None:
        if self._token and self._clock() < self._expires_at - REFRESH_MARGIN_SECONDS:
            return self._token
        return None

    async def token(self, client: httpx.AsyncClient) -> str:
        cached = self._fresh()
        if cached:
            return cached
        async with self._lock:
            cached = self._fresh()  # another call may have refreshed it meanwhile
            if cached:
                return cached
            now = self._clock()
            token, expires_in = await self._fetch(client, now)
            self._token = token
            self._expires_at = now + expires_in
            return token

    async def _fetch(self, client: httpx.AsyncClient, now: float) -> tuple[str, float]:
        raise NotImplementedError


def _read_token(resp: httpx.Response) -> tuple[str, float]:
    if resp.status_code != 200:
        raise VertexAuthError(f"token endpoint status {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError:
        raise VertexAuthError("token endpoint body is not JSON") from None
    if not isinstance(payload, dict):
        raise VertexAuthError("token endpoint body is not an object")
    token = payload.get("access_token")
    if not isinstance(token, str) or not token or any(c.isspace() for c in token):
        raise VertexAuthError("token endpoint gave no access token")
    if str(payload.get("token_type", "Bearer")).lower() != "bearer":
        raise VertexAuthError("token endpoint gave a token that is not Bearer")
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, bool) or not isinstance(expires_in, int | float) or expires_in <= 0:
        expires_in = REFRESH_MARGIN_SECONDS * 2  # unknown: short, so it is renewed soon
    return token, float(min(expires_in, MAX_TOKEN_SECONDS))


async def _post(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    try:
        return await client.post(url, **kwargs)
    except httpx.TimeoutException:
        raise VertexAuthError("token endpoint timeout") from None
    except httpx.HTTPError:
        raise VertexAuthError("token endpoint connection error") from None


class ServiceAccountTokens(_CachedToken):
    def __init__(
        self,
        email: str,
        private_key: rsa.RSAPrivateKey,
        key_id: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(clock)
        self.email = email
        self._key = private_key
        self._key_id = key_id

    def assertion(self, now: float) -> str:
        header: dict[str, str] = {"alg": "RS256", "typ": "JWT"}
        if self._key_id:
            header["kid"] = self._key_id
        issued = int(now)
        claims = {
            "iss": self.email,
            "scope": SCOPE,
            "aud": TOKEN_URL,
            "iat": issued,
            "exp": issued + ASSERTION_LIFETIME_SECONDS,
        }
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + _b64url(json.dumps(claims, separators=(",", ":")).encode())
        )
        signature = self._key.sign(
            signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
        )
        return f"{signing_input}.{_b64url(signature)}"

    async def _fetch(self, client: httpx.AsyncClient, now: float) -> tuple[str, float]:
        resp = await _post(
            client,
            TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": self.assertion(now),
            },
        )
        return _read_token(resp)


class AuthorizedUserTokens(_CachedToken):
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        quota_project: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(clock)
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self.quota_project = quota_project

    async def _fetch(self, client: httpx.AsyncClient, now: float) -> tuple[str, float]:
        resp = await _post(
            client,
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
            },
        )
        return _read_token(resp)


class MetadataServerTokens(_CachedToken):
    """The service account attached to the machine, when it runs on Google Cloud."""

    async def _fetch(self, client: httpx.AsyncClient, now: float) -> tuple[str, float]:
        try:
            resp = await client.get(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
        except httpx.TimeoutException:
            raise VertexAuthError("metadata server timeout") from None
        except httpx.HTTPError:
            raise VertexAuthError("no credentials: no file and no metadata server") from None
        return _read_token(resp)


def _text(info: Mapping[str, object], field: str) -> str:
    value = info.get(field)
    if not isinstance(value, str) or not value.strip():
        raise VertexAuthError(f"credentials file lacks {field}")
    return value


def from_info(info: object, clock: Callable[[], float] = time.time) -> TokenSource:
    """A token source from the parsed JSON of a credentials file."""
    if not isinstance(info, dict):
        raise VertexAuthError("credentials file is not a JSON object")
    kind = info.get("type")
    if kind == "service_account":
        pem = _text(info, "private_key")
        key = _load_key(pem)
        if key is None:
            raise VertexAuthError("credentials file has an unreadable private key")
        if not isinstance(key, rsa.RSAPrivateKey):
            raise VertexAuthError("credentials file has a private key that is not RSA")
        key_id = info.get("private_key_id")
        return ServiceAccountTokens(
            _text(info, "client_email"), key, key_id if isinstance(key_id, str) else None, clock
        )
    if kind == "authorized_user":
        quota = info.get("quota_project_id")
        return AuthorizedUserTokens(
            _text(info, "client_id"),
            _text(info, "client_secret"),
            _text(info, "refresh_token"),
            quota if isinstance(quota, str) and quota else None,
            clock,
        )
    raise VertexAuthError("credentials file is neither service_account nor authorized_user")


def from_file(path: str | Path, clock: Callable[[], float] = time.time) -> TokenSource:
    """Read once, at start-up. Errors say what is wrong, never what the file holds."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise VertexAuthError("credentials file cannot be read") from None
    if len(raw) > MAX_FILE_BYTES:
        raise VertexAuthError("credentials file is too big")
    info = _parse_json(raw)
    if info is _NOT_JSON:
        raise VertexAuthError("credentials file is not JSON")
    return from_info(info, clock)


_NOT_JSON = object()


def _parse_json(raw: bytes) -> object:
    """The parsed file, or _NOT_JSON. The error is dropped here and not raised from:
    a JSONDecodeError keeps the whole document in `.doc`, and an exception raised inside
    the `except` would keep it reachable as its context."""
    try:
        return json.loads(raw)
    except ValueError:
        return _NOT_JSON


def _load_key(pem: str) -> object | None:
    """The private key, or None. Same reason as _parse_json for not raising inside."""
    try:
        return serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except (ValueError, TypeError):
        return None


def _well_known_file(env: Mapping[str, str]) -> Path | None:
    if os.name == "nt":
        base = env.get("APPDATA")
        return Path(base) / "gcloud" / "application_default_credentials.json" if base else None
    config = env.get("CLOUDSDK_CONFIG")
    if config:
        return Path(config) / "application_default_credentials.json"
    home = env.get("HOME")
    if not home:
        return None
    return Path(home) / ".config" / "gcloud" / "application_default_credentials.json"


def application_default(
    env: Mapping[str, str] | None = None, clock: Callable[[], float] = time.time
) -> TokenSource:
    """ADC in Google's order: GOOGLE_APPLICATION_CREDENTIALS, the file of gcloud, then the
    metadata server (only reachable on Google Cloud; elsewhere the call fails then)."""
    env = os.environ if env is None else env
    explicit = env.get("GOOGLE_APPLICATION_CREDENTIALS")
    if explicit:
        return from_file(explicit, clock)
    well_known = _well_known_file(env)
    if well_known is not None and well_known.is_file():
        return from_file(well_known, clock)
    return MetadataServerTokens(clock)
