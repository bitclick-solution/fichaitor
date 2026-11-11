"""Google Places API (New) adapter. Read-only, public data."""

from datetime import date

import httpx

from app.domain.diagnosis import PlaceSnapshot
from app.ports.places import PlacesUnavailable

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
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
    "reviews.publishTime",
    "googleMapsUri",
    *SERVICE_FLAGS,
)
FIELD_MASK = ",".join(f"places.{f}" for f in FIELDS)


def to_snapshot(raw: dict) -> PlaceSnapshot:
    review_dates = [
        date.fromisoformat(r["publishTime"][:10])
        for r in raw.get("reviews", [])
        if r.get("publishTime")
    ]
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
        latest_review_date=max(review_dates) if review_dates else None,
        website=raw.get("websiteUri", ""),
        phone=raw.get("nationalPhoneNumber", ""),
        service_attributes=sum(1 for f in SERVICE_FLAGS if raw.get(f) is True),
        maps_url=raw.get("googleMapsUri", ""),
    )


class GooglePlaces:
    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=10)

    async def find(self, query: str) -> PlaceSnapshot | None:
        try:
            resp = await self._client.post(
                SEARCH_URL,
                headers={"X-Goog-Api-Key": self._api_key, "X-Goog-FieldMask": FIELD_MASK},
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
        return to_snapshot(places[0]) if places else None
