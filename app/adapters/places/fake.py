"""In-memory places gateway for tests and local development."""

from app.domain.diagnosis import PlaceSnapshot
from app.ports.places import PlacesUnavailable

DEMO = PlaceSnapshot(
    place_id="demo-bar-la-parra",
    name="Bar La Parra (ejemplo)",
    address="Calle de ejemplo 1, Granada",
    primary_type="bar",
    primary_type_label="Bar",
    types=("bar", "point_of_interest", "establishment"),
    has_regular_hours=True,
    photo_count=6,
    rating=4.3,
    review_count=48,
    website="",
    phone="958 00 00 00",
    service_attributes=0,
)


class FakePlaces:
    def __init__(self, places: dict[str, PlaceSnapshot] | None = None, fail: bool = False) -> None:
        self._places = places if places is not None else {"demo": DEMO}
        self._fail = fail
        self.calls = 0  # text searches (step 1)
        self.id_calls: list[str] = []  # lookups by identifier (step 2)

    async def find_id(self, query: str) -> str | None:
        self.calls += 1
        if self._fail:
            raise PlacesUnavailable("fallo simulado")
        q = query.lower()
        for key, place in self._places.items():
            if key in q:
                return place.place_id
        return None

    async def get(self, place_id: str) -> PlaceSnapshot | None:
        self.id_calls.append(place_id)
        if self._fail:
            raise PlacesUnavailable("fallo simulado")
        return next((p for p in self._places.values() if p.place_id == place_id), None)
