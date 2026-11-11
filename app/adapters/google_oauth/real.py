"""Google OAuth 2.0 (server-side flow) and Business Profile account listing."""

import asyncio
from urllib.parse import urlencode

import httpx

from app.ports.google_oauth import (
    SCOPES,
    GoogleOAuthError,
    Identity,
    ProfileLocation,
    TokenSet,
)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
ACCOUNTS_URL = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
LOCATIONS_URL = "https://mybusinessbusinessinformation.googleapis.com/v1/{account}/locations"
RETRIES = 3  # Business Information answers 503 now and then on the first call


class RealGoogleOAuth:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        client: httpx.AsyncClient | None = None,
        retry_delay: float = 2.0,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._client = client or httpx.AsyncClient(timeout=15)
        self._retry_delay = retry_delay

    def authorization_url(self, state: str) -> str:
        return f"{AUTH_URL}?" + urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": self._redirect_uri,
                "response_type": "code",
                "scope": " ".join(SCOPES),
                "access_type": "offline",
                "prompt": "consent",  # forces a refresh token on every connection
                "include_granted_scopes": "false",
                "state": state,
            }
        )

    async def exchange(self, code: str) -> TokenSet:
        try:
            resp = await self._client.post(
                TOKEN_URL,
                data={
                    "code": code,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "redirect_uri": self._redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
        except httpx.HTTPError as exc:
            raise GoogleOAuthError("sin respuesta de Google") from exc
        if resp.status_code != 200:
            # The body may echo the code; never include it in the error.
            raise GoogleOAuthError(f"intercambio rechazado ({resp.status_code})")
        body = resp.json()
        return TokenSet(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            scopes=tuple(body.get("scope", "").split()),
        )

    async def _get(self, url: str, access_token: str, params: dict | None = None) -> dict:
        last = "sin respuesta"
        for attempt in range(RETRIES):
            try:
                resp = await self._client.get(
                    url, params=params, headers={"Authorization": f"Bearer {access_token}"}
                )
            except httpx.HTTPError:
                last = "error de red"
            else:
                if resp.status_code == 200:
                    return resp.json()
                last = f"estado {resp.status_code}"
                if resp.status_code < 500:
                    break
            if attempt < RETRIES - 1:
                await asyncio.sleep(self._retry_delay)
        raise GoogleOAuthError(f"Google no devolvió datos ({last})")

    async def identity(self, access_token: str) -> Identity:
        body = await self._get(USERINFO_URL, access_token)
        return Identity(sub=body["sub"], email=body.get("email", ""))

    async def locations(self, access_token: str) -> list[ProfileLocation]:
        accounts = (await self._get(ACCOUNTS_URL, access_token)).get("accounts", [])
        found: list[ProfileLocation] = []
        for account in accounts:
            body = await self._get(
                LOCATIONS_URL.format(account=account["name"]),
                access_token,
                {"readMask": "name,title,metadata", "pageSize": 100},
            )
            for loc in body.get("locations", []):
                meta = loc.get("metadata") or {}
                found.append(
                    ProfileLocation(
                        account=account["name"],
                        location=loc["name"],
                        title=loc.get("title", ""),
                        place_id=meta.get("placeId", ""),
                        verified=bool(meta.get("hasVoiceOfMerchant")),
                    )
                )
        return found
