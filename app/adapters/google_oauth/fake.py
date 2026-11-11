"""Fake Google OAuth for tests and local development."""

from app.adapters.places.fake import DEMO
from app.ports.google_oauth import (
    SCOPES,
    GoogleOAuthError,
    Identity,
    ProfileLocation,
    TokenSet,
)


class FakeGoogleOAuth:
    def __init__(
        self,
        *,
        refresh_token: str | None = "fake-refresh-token",
        scopes: tuple[str, ...] = SCOPES,
        locations: list[ProfileLocation] | None = None,
        fail_exchange: bool = False,
        fail_locations: bool = False,
    ) -> None:
        self._refresh_token = refresh_token
        self._scopes = scopes
        # Same place as the fake Places gateway, so the whole journey works without Google.
        self.profile_locations = (
            locations
            if locations is not None
            else [ProfileLocation("accounts/1", "locations/1", DEMO.name, DEMO.place_id, True)]
        )
        self.identity_value = Identity(sub="google-sub-1", email="dueno@example.com")
        self._fail_exchange = fail_exchange
        self.fail_locations = fail_locations
        self.exchanged: list[str] = []

    def authorization_url(self, state: str) -> str:
        return f"https://accounts.google.test/auth?state={state}"

    async def exchange(self, code: str) -> TokenSet:
        self.exchanged.append(code)
        if self._fail_exchange:
            raise GoogleOAuthError("fallo simulado")
        return TokenSet("fake-access-token", self._refresh_token, self._scopes)

    async def identity(self, access_token: str) -> Identity:
        return self.identity_value

    async def locations(self, access_token: str) -> list[ProfileLocation]:
        if self.fail_locations:
            raise GoogleOAuthError("fallo simulado")
        return self.profile_locations
