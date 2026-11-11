"""Google Business Profile, read side, for the worker.

Token refresh, reviews (My Business v4), profile hours and Google's own changes
(Business Information v1). Endpoints verified in docs/fase0/matriz-api.md.

Error messages carry only a status code: never a token, a request body or a
response body (Google may echo what it was sent).
"""

import asyncio
import re
from datetime import date, datetime

import httpx

from app.domain.profile_summary import (
    ProfileHours,
    Review,
    ReviewSet,
    original_text,
    parse_diff_mask,
    star_rating,
)
from app.ports.google_profile import GoogleProfileError, GoogleRejected, ReconnectRequired

TOKEN_URL = "https://oauth2.googleapis.com/token"
REVIEWS_URL = "https://mybusiness.googleapis.com/v4/{account}/{location}/reviews"
LOCATION_URL = "https://mybusinessbusinessinformation.googleapis.com/v1/{location}"
GOOGLE_UPDATED_URL = LOCATION_URL + ":getGoogleUpdated"

HOURS_MASK = "name,regularHours,specialHours"
# getGoogleUpdated compares these fields between the owner's version and Google's.
UPDATED_MASK = (
    "title,phoneNumbers,categories,storefrontAddress,websiteUri,"
    "regularHours,specialHours,openInfo,profile"
)
RETRIES = 3  # Business Information answers 503 now and then
REVIEWS_PAGE_SIZE = 50  # the maximum Google accepts
MAX_REVIEW_PAGES = 200  # 10.000 reviews; also stops a page token that never ends

_ACCOUNT = re.compile(r"accounts/[A-Za-z0-9_-]{1,64}")
_LOCATION = re.compile(r"locations/[A-Za-z0-9_-]{1,64}")
_OAUTH_ERROR = re.compile(r"[a-z_]{1,40}")


class RealGoogleProfile:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        client: httpx.AsyncClient | None = None,
        retry_delay: float = 2.0,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._client = client or httpx.AsyncClient(timeout=15)
        self._retry_delay = retry_delay

    async def close(self) -> None:
        await self._client.aclose()

    async def refresh_access_token(self, refresh_token: str) -> str:
        last = "sin respuesta"
        for attempt in range(RETRIES):
            try:
                resp = await self._client.post(
                    TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                    },
                )
            except httpx.HTTPError:
                last = "error de red"
            else:
                status = resp.status_code
                if status == 200:
                    token = _json(resp).get("access_token")
                    if not isinstance(token, str) or not token:
                        raise GoogleProfileError("renovación sin token de acceso")
                    return token
                code = _oauth_error_code(resp)
                if code == "invalid_client":
                    # Our own credentials are wrong: asking the owner to reconnect
                    # would not fix it.
                    raise GoogleRejected(f"credenciales de la aplicación rechazadas ({status})")
                if code == "invalid_grant" or status == 401:
                    raise ReconnectRequired(f"permiso de Google caducado o revocado ({status})")
                if status < 500 and status != 429:
                    raise GoogleRejected(f"renovación rechazada (estado {status})")
                last = f"estado {status}"
            if attempt < RETRIES - 1:
                await asyncio.sleep(self._retry_delay)
        raise GoogleProfileError(f"no se pudo renovar el acceso ({last})")

    async def reviews(self, access_token: str, account: str, location: str) -> ReviewSet:
        url = REVIEWS_URL.format(account=_checked(_ACCOUNT, account), location=_loc(location))
        found: list[Review] = []
        total: int | None = None
        average: float | None = None
        page_token: str | None = None
        for _ in range(MAX_REVIEW_PAGES):
            params: dict[str, str | int] = {"pageSize": REVIEWS_PAGE_SIZE}
            if page_token:
                params["pageToken"] = page_token
            body = await self._get(url, access_token, params)
            found.extend(_review(item) for item in body.get("reviews") or [])
            if isinstance(body.get("totalReviewCount"), int):
                total = body["totalReviewCount"]
            if isinstance(body.get("averageRating"), int | float):
                average = float(body["averageRating"])
            page_token = body.get("nextPageToken")
            if not page_token:
                return ReviewSet(reviews=tuple(found), total=total, average=average)
        raise GoogleProfileError("demasiadas páginas de reseñas")

    async def hours(self, access_token: str, location: str) -> ProfileHours:
        body = await self._get(
            LOCATION_URL.format(location=_loc(location)), access_token, {"readMask": HOURS_MASK}
        )
        periods = (body.get("regularHours") or {}).get("periods") or []
        special = (body.get("specialHours") or {}).get("specialHourPeriods") or []
        dates = (_date(period.get("startDate")) for period in special)
        return ProfileHours(
            has_regular_hours=bool(periods),
            special_dates=tuple(d for d in dates if d is not None),
        )

    async def google_updated_fields(self, access_token: str, location: str) -> tuple[str, ...]:
        body = await self._get(
            GOOGLE_UPDATED_URL.format(location=_loc(location)),
            access_token,
            {"readMask": UPDATED_MASK},
        )
        return parse_diff_mask(body.get("diffMask"))

    async def _get(self, url: str, access_token: str, params: dict) -> dict:
        last = "sin respuesta"
        for attempt in range(RETRIES):
            try:
                resp = await self._client.get(
                    url, params=params, headers={"Authorization": f"Bearer {access_token}"}
                )
            except httpx.HTTPError:
                last = "error de red"
            else:
                status = resp.status_code
                if status == 200:
                    return _json(resp)
                if status == 401:
                    raise ReconnectRequired("Google no acepta el acceso (401)")
                if status < 500 and status != 429:
                    raise GoogleRejected(f"Google rechazó la lectura (estado {status})")
                last = f"estado {status}"
            if attempt < RETRIES - 1:
                await asyncio.sleep(self._retry_delay)
        raise GoogleProfileError(f"Google no devolvió datos ({last})")


def _json(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise GoogleProfileError("respuesta de Google ilegible")
    return body


def _oauth_error_code(resp: httpx.Response) -> str:
    """The 'error' field of an OAuth error, only if it looks like a plain code."""
    try:
        code = resp.json().get("error")
    except (ValueError, AttributeError):
        return ""
    return code if isinstance(code, str) and _OAUTH_ERROR.fullmatch(code) else ""


def _checked(pattern: re.Pattern[str], name: str) -> str:
    # Resource names go into the URL path: accept only the exact documented shape.
    if not isinstance(name, str) or not pattern.fullmatch(name):
        raise GoogleRejected("identificador de ficha no válido")
    return name


def _loc(location: str) -> str:
    return _checked(_LOCATION, location)


def _review(item: dict) -> Review:
    return Review(
        rating=star_rating(item.get("starRating")),
        created_at=_datetime(item.get("createTime")),
        replied=bool(item.get("reviewReply")),
        text=original_text(item.get("comment")),
    )


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _date(value: object) -> date | None:
    if not isinstance(value, dict):
        return None
    try:
        return date(int(value["year"]), int(value["month"]), int(value["day"]))
    except (KeyError, TypeError, ValueError):
        return None
