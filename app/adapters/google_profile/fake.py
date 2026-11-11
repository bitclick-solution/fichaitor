"""Fake Google Business Profile for tests and local development (FICHAITOR_FAKE_GOOGLE)."""

from datetime import UTC, date, datetime, timedelta

from app.domain.profile_summary import ProfileHours, Review, ReviewSet
from app.ports.google_profile import GoogleProfileError

FAKE_ACCESS_TOKEN = "fake-access-token"


def demo_reviews(now: datetime | None = None) -> ReviewSet:
    """A profile like the one that sells the product: reviews nobody has answered."""
    now = now or datetime.now(UTC)
    stars = [5, 5, 4, 5, 1, 5, 4, 5, 2, 5, 5, 3, 5]
    reviews = tuple(
        Review(
            rating=rating,
            created_at=now - timedelta(days=3 + 11 * index),
            replied=index >= 9,  # only the oldest ones got an answer
            text="",
        )
        for index, rating in enumerate(stars)
    )
    return ReviewSet(reviews=reviews, total=len(stars), average=round(sum(stars) / len(stars), 1))


def demo_hours(today: date | None = None) -> ProfileHours:
    today = today or datetime.now(UTC).date()
    return ProfileHours(has_regular_hours=True, special_dates=(today + timedelta(days=20),))


class FakeGoogleProfile:
    def __init__(
        self,
        *,
        reviews: ReviewSet | None = None,
        hours: ProfileHours | None = None,
        changed_fields: tuple[str, ...] = (),
        access_token: str = FAKE_ACCESS_TOKEN,
        refresh_error: Exception | None = None,
        read_error: Exception | None = None,
        read_failures: int = 0,
    ) -> None:
        self._reviews = reviews if reviews is not None else demo_reviews()
        self._hours = hours if hours is not None else demo_hours()
        self._changed_fields = changed_fields
        self._access_token = access_token
        self.refresh_error = refresh_error
        self.read_error = read_error  # raised on every read
        self.read_failures = read_failures  # this many reads fail, then they work
        self.refreshed_with: list[str] = []
        self.reads: list[tuple[str, str]] = []

    async def refresh_access_token(self, refresh_token: str) -> str:
        self.refreshed_with.append(refresh_token)
        if self.refresh_error is not None:
            raise self.refresh_error
        return self._access_token

    def _read(self, what: str, location: str) -> None:
        self.reads.append((what, location))
        if self.read_error is not None:
            raise self.read_error
        if self.read_failures > 0:
            self.read_failures -= 1
            raise GoogleProfileError("fallo temporal simulado")

    async def reviews(self, access_token: str, account: str, location: str) -> ReviewSet:
        self._read("reviews", location)
        return self._reviews

    async def hours(self, access_token: str, location: str) -> ProfileHours:
        self._read("hours", location)
        return self._hours

    async def google_updated_fields(self, access_token: str, location: str) -> tuple[str, ...]:
        self._read("google_updated", location)
        return self._changed_fields
