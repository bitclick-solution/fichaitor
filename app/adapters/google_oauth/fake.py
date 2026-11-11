"""Fake Google OAuth for tests and local development."""

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
        self._locations = (
            locations
            if locations is not None
            else [
                ProfileLocation("accounts/1", "locations/1", "Bar La Parra (ejemplo)", "p1", True)
            ]
        )
        self._fail_exchange = fail_exchange
        self._fail_locations = fail_locations
        self.exchanged: list[str] = []

    def authorization_url(self, state: str) -> str:
        return f"https://accounts.google.test/auth?state={state}"

    async def exchange(self, code: str) -> TokenSet:
        self.exchanged.append(code)
        if self._fail_exchange:
            raise GoogleOAuthError("fallo simulado")
        return TokenSet("fake-access-token", self._refresh_token, self._scopes)

    async def identity(self, access_token: str) -> Identity:
        return Identity(sub="google-sub-1", email="dueno@example.com")

    async def locations(self, access_token: str) -> list[ProfileLocation]:
        if self._fail_locations:
            raise GoogleOAuthError("fallo simulado")
        return self._locations
