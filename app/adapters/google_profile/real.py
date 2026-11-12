"""Google Business Profile for the worker.

Token refresh, reviews and media (My Business v4), the profile itself, its attributes
and Google's own changes (Business Information v1). Endpoints verified in
docs/fase0/matriz-api.md.

One write: the reply to a review (`reviews.updateReply`, My Business v4),
`PUT {account}/{location}/reviews/{reviewId}/reply` with `{"comment": text}`. It
creates the reply or replaces the one there is. Taken from Google's reference; it has
not been run against a real profile yet (docs/fase0/matriz-api.md, "Pendiente").
https://developers.google.com/my-business/reference/rest/v4/accounts.locations.reviews/updateReply

Error messages carry only a status code: never a token, a request body or a
response body (Google may echo what it was sent).
"""

import asyncio
import re
from datetime import date, datetime, timedelta

import httpx

from app.domain.actions import GBP_REVIEW_ID
from app.domain.profile_summary import (
    MediaSummary,
    ProfileDetails,
    ProfileHours,
    Review,
    ReviewSet,
    original_text,
    parse_diff_mask,
    star_rating,
)
from app.ports.google_profile import GoogleProfileError, GoogleRejected, ReconnectRequired

TOKEN_URL = "https://oauth2.googleapis.com/token"
# https://developers.google.com/identity/protocols/oauth2/web-server#tokenrevoke
# POST, application/x-www-form-urlencoded, parameter `token` (an access token or a
# refresh token). 200 when revoked; 400 with an error code otherwise.
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
REVIEWS_URL = "https://mybusiness.googleapis.com/v4/{account}/{location}/reviews"
REVIEW_URL = REVIEWS_URL + "/{review_id}"
REPLY_URL = REVIEW_URL + "/reply"
LOCATION_URL = "https://mybusinessbusinessinformation.googleapis.com/v1/{location}"
GOOGLE_UPDATED_URL = LOCATION_URL + ":getGoogleUpdated"
ATTRIBUTES_URL = LOCATION_URL + "/attributes"
MEDIA_URL = "https://mybusiness.googleapis.com/v4/{account}/{location}/media"

# Everything the diagnosis of a connected profile needs, in one read.
DETAILS_MASK = (
    "name,title,storefrontAddress,categories,profile,phoneNumbers,websiteUri,"
    "regularHours,specialHours,serviceItems,openInfo,metadata"
)
# getGoogleUpdated compares these fields between the owner's version and Google's.
UPDATED_MASK = (
    "title,phoneNumbers,categories,storefrontAddress,websiteUri,"
    "regularHours,specialHours,openInfo,profile"
)
RETRIES = 3  # Business Information answers 503 now and then
REVIEWS_PAGE_SIZE = 50  # the maximum Google accepts
MAX_REVIEW_PAGES = 200  # 10.000 reviews; also stops a page token that never ends
MEDIA_PAGE_SIZE = 100
MAX_MEDIA_PAGES = 5  # enough to date the newest photo; the count comes in every page
MAX_SPECIAL_PERIOD_DAYS = 31  # a longer special-hours period counts up to here
CLOSED_STATUSES = frozenset({"CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"})

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

    async def revoke_token(self, refresh_token: str) -> None:
        # The token goes in the body, never in the URL: URLs end up in logs.
        try:
            resp = await self._client.post(REVOKE_URL, data={"token": refresh_token})
        except httpx.HTTPError:
            raise GoogleProfileError("sin respuesta de Google al revocar") from None
        status = resp.status_code
        if status == 200:
            return
        if status == 400 and _oauth_error_code(resp) == "invalid_token":
            return  # expired or revoked already: there is nothing left to revoke
        if status < 500 and status != 429:
            raise GoogleRejected(f"revocación rechazada (estado {status})", status)
        raise GoogleProfileError(f"no se pudo revocar el acceso (estado {status})", status)

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

    async def details(self, access_token: str, location: str) -> ProfileDetails:
        body = await self._get(
            LOCATION_URL.format(location=_loc(location)),
            access_token,
            {"readMask": DETAILS_MASK},
        )
        periods = _dict(body.get("regularHours")).get("periods") or []
        special = _dict(body.get("specialHours")).get("specialHourPeriods") or []
        categories = _dict(body.get("categories"))
        metadata = _dict(body.get("metadata"))
        services = body.get("serviceItems")
        additional = categories.get("additionalCategories")
        return ProfileDetails(
            hours=ProfileHours(
                has_regular_hours=bool(periods),
                special_dates=tuple(
                    day for period in special if isinstance(period, dict) for day in _days(period)
                ),
            ),
            title=_text(body.get("title")),
            address=_address(_dict(body.get("storefrontAddress"))),
            category=_text(_dict(categories.get("primaryCategory")).get("displayName")),
            additional_categories=len(additional) if isinstance(additional, list) else 0,
            description_length=len(_text(_dict(body.get("profile")).get("description"))),
            has_phone=bool(_text(_dict(body.get("phoneNumbers")).get("primaryPhone"))),
            has_website=bool(_text(body.get("websiteUri"))),
            service_count=len(services) if isinstance(services, list) else 0,
            is_open=_dict(body.get("openInfo")).get("status") not in CLOSED_STATUSES,
            place_id=_text(metadata.get("placeId")),
            maps_url=_text(metadata.get("mapsUri")),
        )

    async def attribute_count(self, access_token: str, location: str) -> int:
        body = await self._get(ATTRIBUTES_URL.format(location=_loc(location)), access_token, {})
        attributes = body.get("attributes")
        return len(attributes) if isinstance(attributes, list) else 0

    async def media(self, access_token: str, account: str, location: str) -> MediaSummary:
        url = MEDIA_URL.format(account=_checked(_ACCOUNT, account), location=_loc(location))
        total: int | None = None
        seen = 0
        latest: datetime | None = None
        page_token: str | None = None
        complete = False
        for _ in range(MAX_MEDIA_PAGES):
            params: dict[str, str | int] = {"pageSize": MEDIA_PAGE_SIZE}
            if page_token:
                params["pageToken"] = page_token
            body = await self._get(url, access_token, params)
            items = [item for item in body.get("mediaItems") or [] if isinstance(item, dict)]
            seen += len(items)
            for item in items:
                created = _datetime(item.get("createTime"))
                if created is not None and (latest is None or created > latest):
                    latest = created
            if total is None and isinstance(body.get("totalMediaItemCount"), int):
                total = body["totalMediaItemCount"]
            page_token = body.get("nextPageToken")
            if not page_token:
                complete = True
                break
        # Google does not say in which order the items come: the newest date is only
        # known when every item has been read.
        return MediaSummary(
            count=max(total if total is not None else seen, 0),
            latest_at=latest if complete else None,
        )

    async def google_updated_fields(self, access_token: str, location: str) -> tuple[str, ...]:
        body = await self._get(
            GOOGLE_UPDATED_URL.format(location=_loc(location)),
            access_token,
            {"readMask": UPDATED_MASK},
        )
        return parse_diff_mask(body.get("diffMask"))

    async def current_reply(
        self, access_token: str, account: str, location: str, review_id: str
    ) -> str | None:
        body = await self._get(_review_url(REVIEW_URL, account, location, review_id), access_token)
        reply = body.get("reviewReply")
        if not isinstance(reply, dict):
            return None
        comment = reply.get("comment")
        return comment if isinstance(comment, str) else ""

    async def reply_to_review(
        self, access_token: str, account: str, location: str, review_id: str, text: str
    ) -> datetime | None:
        url = _review_url(REPLY_URL, account, location, review_id)
        # One request, no retry here: see the port. The error messages are fixed
        # strings with a status code; Google's answer may quote the reply.
        try:
            resp = await self._client.put(
                url, json={"comment": text}, headers={"Authorization": f"Bearer {access_token}"}
            )
        except httpx.HTTPError:
            raise GoogleProfileError("Google no respondió a la escritura") from None
        status = resp.status_code
        if status == 200:
            try:
                body = resp.json()
            except ValueError:
                body = None
            return _datetime(body.get("updateTime")) if isinstance(body, dict) else None
        if status == 401:
            raise ReconnectRequired("Google no acepta el acceso (401)")
        if status < 500 and status != 429:
            raise GoogleRejected(f"Google rechazó la respuesta (estado {status})", status)
        raise GoogleProfileError(f"Google no aceptó la escritura (estado {status})", status)

    async def _get(self, url: str, access_token: str, params: dict | None = None) -> dict:
        last = "sin respuesta"
        for attempt in range(RETRIES):
            try:
                resp = await self._client.get(
                    url, params=params or {}, headers={"Authorization": f"Bearer {access_token}"}
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
                    raise GoogleRejected(f"Google rechazó la lectura (estado {status})", status)
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


def _review_url(template: str, account: str, location: str, review_id: str) -> str:
    return template.format(
        account=_checked(_ACCOUNT, account),
        location=_loc(location),
        review_id=_checked(GBP_REVIEW_ID, review_id),
    )


def _review(item: dict) -> Review:
    """What is kept of a review. The `reviewer` field (name and photo of whoever wrote
    it) is never read: data minimisation."""
    reply = item.get("reviewReply")
    replied = bool(reply)
    reply_text = None
    reply_at = None
    if replied:
        reply = _dict(reply)
        reply_text = reply["comment"] if isinstance(reply.get("comment"), str) else ""
        reply_at = _datetime(reply.get("updateTime"))
    review_id = item.get("reviewId")
    return Review(
        rating=star_rating(item.get("starRating")),
        created_at=_datetime(item.get("createTime")),
        replied=replied,
        text=original_text(item.get("comment")),
        review_id=review_id if isinstance(review_id, str) else "",
        reply_text=reply_text,
        reply_at=reply_at,
    )


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _address(raw: dict) -> str:
    """'Calle Elvira 1, 18010 Granada' from Google's postal address."""
    lines = [_text(line) for line in raw.get("addressLines") or []]
    town = " ".join(p for p in (_text(raw.get("postalCode")), _text(raw.get("locality"))) if p)
    return ", ".join(part for part in (*lines, town) if part)


def _days(period: dict) -> list[date]:
    """Every day of a special-hours period: its start date, or from start to end."""
    start = _date(period.get("startDate"))
    if start is None:
        return []
    end = _date(period.get("endDate")) or start
    length = min(max((end - start).days, 0), MAX_SPECIAL_PERIOD_DAYS - 1)
    return [start + timedelta(days=offset) for offset in range(length + 1)]


def _date(value: object) -> date | None:
    if not isinstance(value, dict):
        return None
    try:
        return date(int(value["year"]), int(value["month"]), int(value["day"]))
    except (KeyError, TypeError, ValueError):
        return None
