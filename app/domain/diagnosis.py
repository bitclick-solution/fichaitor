"""Public profile diagnosis: pure scoring rules, no I/O.

Input is what Google exposes publicly about a place. Anything that needs the
owner's authorization (unanswered reviews, holiday hours, owner description)
is reported as a locked check, never guessed.
"""

from dataclasses import dataclass, field
from enum import StrEnum

GENERIC_TYPES = frozenset({"point_of_interest", "establishment", "food", "store"})
FOOD_TYPES = frozenset({"restaurant", "bar", "cafe", "bakery", "meal_takeaway", "meal_delivery"})
PHOTOS_VISIBLE_CAP = 10  # Google returns at most 10 photos per place
FEW_REVIEWS = 10
LOW_RATING = 4.0
MAX_FINDINGS = 5


class Impact(StrEnum):
    HIGH = "alto"
    MEDIUM = "medio"
    LOW = "bajo"


_IMPACT_ORDER = {Impact.HIGH: 0, Impact.MEDIUM: 1, Impact.LOW: 2}


@dataclass(frozen=True)
class PlaceSnapshot:
    place_id: str
    name: str
    address: str = ""
    primary_type: str = ""
    primary_type_label: str = ""
    types: tuple[str, ...] = ()
    operational: bool = True
    has_regular_hours: bool = False
    photo_count: int = 0
    rating: float | None = None
    review_count: int = 0
    website: str = ""
    phone: str = ""
    service_attributes: int = 0  # how many service flags (delivery, terrace...) are declared
    maps_url: str = ""


@dataclass(frozen=True)
class Finding:
    code: str
    title: str
    detail: str
    impact: Impact
    penalty: int


@dataclass(frozen=True)
class LockedCheck:
    code: str
    title: str


@dataclass(frozen=True)
class Diagnosis:
    place_id: str
    name: str
    address: str
    category: str
    score: int
    label: str
    findings: tuple[Finding, ...]
    locked_checks: tuple[LockedCheck, ...] = field(default_factory=tuple)
    maps_url: str = ""


LOCKED_CHECKS = (
    LockedCheck("unanswered_reviews", "Cuántas reseñas tienes sin responder"),
    LockedCheck("holiday_hours", "Si tus horarios de festivos están puestos"),
    LockedCheck("owner_description", "Si tu descripción está completa"),
    LockedCheck("google_edits", "Si Google ha cambiado algo de tu ficha sin avisarte"),
)


def _label(score: int) -> str:
    if score >= 85:
        return "Bien"
    if score >= 60:
        return "Mejorable"
    return "Necesita atención"


def _collect(p: PlaceSnapshot) -> list[Finding]:
    out: list[Finding] = []

    if not p.operational:
        out.append(
            Finding(
                "not_operational",
                "Tu ficha no figura como abierta",
                "Google muestra el negocio como cerrado. Quien te busca pasa al siguiente.",
                Impact.HIGH,
                30,
            )
        )

    if not p.has_regular_hours:
        out.append(
            Finding(
                "no_hours",
                "No tienes horario publicado",
                "Sin horario, Google no puede decir que estás abierto y te enseña menos.",
                Impact.HIGH,
                20,
            )
        )

    if p.photo_count == 0:
        out.append(
            Finding(
                "no_photos",
                "Tu ficha no tiene fotos",
                "Es lo primero que mira quien compara dos negocios.",
                Impact.HIGH,
                15,
            )
        )
    elif p.photo_count < 5:
        out.append(
            Finding(
                "few_photos",
                f"Solo tienes {p.photo_count} fotos",
                "Las fichas con más fotos reciben más visitas y más rutas.",
                Impact.MEDIUM,
                10,
            )
        )
    elif p.photo_count < PHOTOS_VISIBLE_CAP:
        out.append(
            Finding(
                "some_photos",
                f"Tienes {p.photo_count} fotos; hay margen",
                "Añadir fotos recientes mantiene la ficha viva.",
                Impact.LOW,
                5,
            )
        )

    if p.rating is not None and p.review_count > 0 and p.rating < LOW_RATING:
        nota = f"{p.rating:.1f}".replace(".", ",")
        out.append(
            Finding(
                "low_rating",
                f"Tu nota media es {nota}",
                "Por debajo de 4, mucha gente descarta el negocio sin leer más.",
                Impact.HIGH,
                10,
            )
        )

    if p.review_count < FEW_REVIEWS:
        titulo = (
            "No tienes reseñas" if p.review_count == 0 else f"Solo tienes {p.review_count} reseñas"
        )
        out.append(
            Finding(
                "few_reviews",
                titulo,
                "Con pocas reseñas, la nota no da confianza y Google te muestra menos.",
                Impact.MEDIUM,
                10,
            )
        )
    # No "stale reviews" rule: Google returns at most 5 reviews sorted by relevance, not
    # by date, so the newest of them is not the last review the business received. The
    # only reliable case (5 reviews or fewer) is already covered by few_reviews.

    if not p.website:
        out.append(
            Finding(
                "no_website",
                "No tienes web enlazada",
                "Pierdes los clics de quien quiere ver la carta, los precios o reservar.",
                Impact.MEDIUM,
                8,
            )
        )

    if not p.phone:
        out.append(
            Finding(
                "no_phone",
                "No tienes teléfono publicado",
                "Quien quiere llamar no puede hacerlo desde la ficha.",
                Impact.MEDIUM,
                8,
            )
        )

    extra = [t for t in p.types if t != p.primary_type and t not in GENERIC_TYPES]
    if not extra:
        out.append(
            Finding(
                "no_secondary_categories",
                "Solo tienes una categoría",
                "Con categorías secundarias apareces en más búsquedas.",
                Impact.MEDIUM,
                6,
            )
        )

    if p.primary_type in FOOD_TYPES and p.service_attributes == 0:
        out.append(
            Finding(
                "no_service_attributes",
                "No indicas tus servicios",
                "Terraza, para llevar, reservas: son filtros que la gente usa al buscar.",
                Impact.LOW,
                5,
            )
        )

    return out


def diagnose(place: PlaceSnapshot) -> Diagnosis:
    found = _collect(place)
    score = max(0, 100 - sum(f.penalty for f in found))
    ranked = sorted(found, key=lambda f: (_IMPACT_ORDER[f.impact], -f.penalty))
    return Diagnosis(
        place_id=place.place_id,
        name=place.name,
        address=place.address,
        category=place.primary_type_label,
        score=score,
        label=_label(score),
        findings=tuple(ranked[:MAX_FINDINGS]),
        locked_checks=LOCKED_CHECKS,
        maps_url=place.maps_url,
    )
