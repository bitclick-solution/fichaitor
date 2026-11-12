"""Fake Google Business Profile for tests and local development (FICHAITOR_FAKE_GOOGLE)."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from app.domain.profile_summary import (
    MediaSummary,
    ProfileDetails,
    ProfileFacts,
    ProfileHours,
    Review,
    ReviewSet,
    collect_facts,
)
from app.ports.google_profile import GoogleProfileError, GoogleRejected

FAKE_ACCESS_TOKEN = "fake-access-token"
DEMO_ATTRIBUTES = 3


# Invented reviews of an invented bar. No real person wrote any of them.
_DEMO_REVIEWS: tuple[tuple[int, str], ...] = (
    (5, "Las mejores tapas del barrio y un trato estupendo. Volveremos seguro."),
    (5, ""),
    (4, "Buen sitio para tomar algo. La terraza se llena pronto, mejor ir temprano."),
    (5, "El arroz, espectacular. Nos atendieron muy rápido aunque estaba lleno."),
    (1, "Esperamos cuarenta minutos y la comida llegó fría. Nadie vino a disculparse."),
    (5, ""),
    (4, "Raciones generosas y buen precio. El café, mejorable."),
    (5, "Fuimos con niños y nos trataron de maravilla."),
    (2, "La comida bien, pero el ruido era insoportable y tardaron mucho en cobrarnos."),
    (5, "Sitio de toda la vida. Las croquetas, de diez."),
    (5, ""),
    (3, "Correcto, sin más. Esperaba algo mejor por las opiniones."),
    (5, "Muy recomendable para desayunar."),
)
_DEMO_FIRST_REPLIED = 9  # only the oldest ones got an answer
_DEMO_REPLY = "Muchas gracias por tu reseña. Nos alegra que estuvieras a gusto."


def demo_reviews(now: datetime | None = None) -> ReviewSet:
    """A profile like the one that sells the product: reviews nobody has answered."""
    now = now or datetime.now(UTC)
    reviews = []
    for index, (rating, text) in enumerate(_DEMO_REVIEWS):
        created = now - timedelta(days=3 + 11 * index)
        replied = index >= _DEMO_FIRST_REPLIED
        reviews.append(
            Review(
                rating=rating,
                created_at=created,
                replied=replied,
                text=text,
                review_id=f"demo-{index + 1}",
                reply_text=_DEMO_REPLY if replied else None,
                reply_at=created + timedelta(days=1) if replied else None,
            )
        )
    stars = [rating for rating, _ in _DEMO_REVIEWS]
    return ReviewSet(
        reviews=tuple(reviews), total=len(stars), average=round(sum(stars) / len(stars), 1)
    )


def demo_hours(today: date | None = None) -> ProfileHours:
    today = today or datetime.now(UTC).date()
    return ProfileHours(has_regular_hours=True, special_dates=(today + timedelta(days=20),))


def demo_details(today: date | None = None, hours: ProfileHours | None = None) -> ProfileDetails:
    """A profile with room to improve: short description, no website, one category."""
    return ProfileDetails(
        hours=hours if hours is not None else demo_hours(today),
        title="",  # the panel then shows the title the connection listed
        address="Calle de ejemplo 1, 18010 Granada",
        category="Bar",
        additional_categories=0,
        description_length=64,
        has_phone=True,
        has_website=False,
        service_count=2,
        is_open=True,
        place_id="",
        maps_url="",
    )


def demo_media(now: datetime | None = None) -> MediaSummary:
    now = now or datetime.now(UTC)
    return MediaSummary(count=4, latest_at=now - timedelta(days=200))


def demo_facts(now: datetime | None = None) -> ProfileFacts:
    """What a sync of the demo profile stores."""
    now = now or datetime.now(UTC)
    return collect_facts(demo_details(now.date()), demo_media(now), DEMO_ATTRIBUTES, now.date())


class FakeGoogleProfile:
    def __init__(
        self,
        *,
        reviews: ReviewSet | None = None,
        hours: ProfileHours | None = None,
        details: ProfileDetails | None = None,
        media: MediaSummary | None = None,
        attributes: int = DEMO_ATTRIBUTES,
        changed_fields: tuple[str, ...] = (),
        access_token: str = FAKE_ACCESS_TOKEN,
        refresh_error: Exception | None = None,
        read_error: Exception | None = None,
        read_failures: int = 0,
        optional_error: Exception | None = None,
        reply_error: Exception | None = None,
        reply_failures: int = 0,
    ) -> None:
        self._reviews = reviews if reviews is not None else demo_reviews()
        # `hours` alone keeps the rest of the demo profile; `details` replaces it all.
        self._details = details if details is not None else demo_details(hours=hours)
        self._media = media if media is not None else demo_media()
        self._attributes = attributes
        self._changed_fields = changed_fields
        self._access_token = access_token
        self.refresh_error = refresh_error
        self.read_error = read_error  # raised on every read
        self.read_failures = read_failures  # this many reads fail, then they work
        self.optional_error = optional_error  # raised by attributes and media only
        self.reply_error = reply_error  # raised on every write
        self.reply_failures = reply_failures  # this many writes fail, then they work
        # A write that fails temporarily may still have reached Google.
        self.failed_writes_arrive = False
        self.refreshed_with: list[str] = []
        self.reads: list[tuple[str, str]] = []
        # Every write that reached "Google": (account, location, review id, text).
        self.replies: list[tuple[str, str, str, str]] = []
        self.write_attempts = 0
        self._replied: dict[tuple[str, str], tuple[str, datetime]] = {}  # by profile and review

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
        return replace(
            self._reviews, reviews=tuple(self._answered(location, r) for r in self._reviews.reviews)
        )

    def _answered(self, location: str, review: Review) -> Review:
        sent = self._replied.get((location, review.review_id))
        if sent is None:
            return review
        return replace(review, replied=True, reply_text=sent[0], reply_at=sent[1])

    def _find(self, location: str, review_id: str) -> Review:
        for review in self._reviews.reviews:
            if review.review_id == review_id:
                return self._answered(location, review)
        raise GoogleRejected("la reseña no existe (estado 404)", 404)

    async def current_reply(
        self, access_token: str, account: str, location: str, review_id: str
    ) -> str | None:
        self._read("review", location)
        return self._find(location, review_id).reply_text

    async def reply_to_review(
        self, access_token: str, account: str, location: str, review_id: str, text: str
    ) -> datetime | None:
        self.write_attempts += 1
        if self.reply_error is not None:
            raise self.reply_error
        self._find(location, review_id)
        failing = self.reply_failures > 0
        if failing:
            self.reply_failures -= 1
        if not failing or self.failed_writes_arrive:
            now = datetime.now(UTC)
            self.replies.append((account, location, review_id, text))
            self._replied[(location, review_id)] = (text, now)
        if failing:
            raise GoogleProfileError("fallo temporal simulado", 503)
        return now

    async def details(self, access_token: str, location: str) -> ProfileDetails:
        self._read("details", location)
        return self._details

    async def google_updated_fields(self, access_token: str, location: str) -> tuple[str, ...]:
        self._read("google_updated", location)
        return self._changed_fields

    async def attribute_count(self, access_token: str, location: str) -> int:
        self._read("attributes", location)
        if self.optional_error is not None:
            raise self.optional_error
        return self._attributes

    async def media(self, access_token: str, account: str, location: str) -> MediaSummary:
        self._read("media", location)
        if self.optional_error is not None:
            raise self.optional_error
        return self._media
