"""Google Places API (New) adapter. Read-only, public data.

A diagnosis takes two requests: Text Search asking only for the identifier (SKU
"Essentials IDs Only", free) and Place Details with the full mask (SKU "Enterprise +
Atmosphere"). One Text Search with the full mask costs 40 USD per 1,000; this way it
is 25. See docs/fase0/places-coste.md.
"""

from urllib.parse import quote

import httpx

from app.domain.diagnosis import PlaceSnapshot
from app.ports.places import PlacesUnavailable

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
SERVICE_FLAGS = (
    "delivery",
    "takeout",
    "dineIn",
    "reservable",
    "outdoorSeating",
    "servesBreakfast",
    "servesLunch",
    "servesDinner",
)
FIELDS = (
    "id",
    "displayName",
    "formattedAddress",
    "businessStatus",
    "rating",
    "userRatingCount",
    "regularOpeningHours.periods",
    "photos.name",
    "websiteUri",
    "nationalPhoneNumber",
    "primaryType",
    "primaryTypeDisplayName",
    "types",
    "googleMapsUri",
    *SERVICE_FLAGS,
)
# Any other field in this mask moves the search to a paid SKU.
SEARCH_FIELD_MASK = "places.id"
DETAILS_FIELD_MASK = ",".join(FIELDS)


def to_snapshot(raw: dict) -> PlaceSnapshot:
    return PlaceSnapshot(
        place_id=raw["id"],
        name=(raw.get("displayName") or {}).get("text", ""),
        address=raw.get("formattedAddress", ""),
        primary_type=raw.get("primaryType", ""),
        primary_type_label=(raw.get("primaryTypeDisplayName") or {}).get("text", ""),
        types=tuple(raw.get("types", [])),
        operational=raw.get("businessStatus", "OPERATIONAL") == "OPERATIONAL",
        has_regular_hours=bool((raw.get("regularOpeningHours") or {}).get("periods")),
        photo_count=len(raw.get("photos", [])),
        rating=raw.get("rating"),
        review_count=raw.get("userRatingCount", 0),
        website=raw.get("websiteUri", ""),
        phone=raw.get("nationalPhoneNumber", ""),
        service_attributes=sum(1 for f in SERVICE_FLAGS if raw.get(f) is True),
        maps_url=raw.get("googleMapsUri", ""),
    )


class GooglePlaces:
    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=10)

    async def find_id(self, query: str) -> str | None:
        try:
            resp = await self._client.post(
                SEARCH_URL,
                headers={"X-Goog-Api-Key": self._api_key, "X-Goog-FieldMask": SEARCH_FIELD_MASK},
                json={
                    "textQuery": query,
                    "languageCode": "es",
                    "regionCode": "ES",
                    "maxResultCount": 1,
                },
            )
        except httpx.HTTPError as exc:
            raise PlacesUnavailable(str(exc)) from exc
        if resp.status_code != 200:
            raise PlacesUnavailable(f"Places respondió {resp.status_code}")
        places = resp.json().get("places", [])
        return (places[0].get("id") or None) if places else None

    async def get(self, place_id: str) -> PlaceSnapshot | None:
        try:
            resp = await self._client.get(
                DETAILS_URL.format(place_id=quote(place_id, safe="")),
                headers={"X-Goog-Api-Key": self._api_key, "X-Goog-FieldMask": DETAILS_FIELD_MASK},
                params={"languageCode": "es", "regionCode": "ES"},
            )
        except httpx.HTTPError as exc:
            raise PlacesUnavailable(str(exc)) from exc
        # 404: unknown or retired identifier. 400: not a valid identifier at all.
        if resp.status_code in (400, 404):
            return None
        if resp.status_code != 200:
            raise PlacesUnavailable(f"Places respondió {resp.status_code}")
        return to_snapshot(resp.json())
