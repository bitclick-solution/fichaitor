from dataclasses import dataclass
from typing import Protocol

SCOPES = ("openid", "email", "https://www.googleapis.com/auth/business.manage")
BUSINESS_SCOPE = "https://www.googleapis.com/auth/business.manage"


class GoogleOAuthError(Exception):
    pass


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str | None
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class Identity:
    sub: str
    email: str


@dataclass(frozen=True)
class ProfileLocation:
    account: str
    location: str
    title: str
    place_id: str = ""
    verified: bool = False


class GoogleOAuth(Protocol):
    def authorization_url(self, state: str) -> str: ...

    async def exchange(self, code: str) -> TokenSet: ...

    async def identity(self, access_token: str) -> Identity: ...

    async def locations(self, access_token: str) -> list[ProfileLocation]: ...
