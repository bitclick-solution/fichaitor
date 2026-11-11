"""Diagnosis of a connected profile: pure scoring rules, no I/O.

Input is what the worker read from the owner's own Business Profile, not what Google
shows in public. Same output as the public diagnosis (app/domain/diagnosis.py), with
the same texts and weights where the criterion is the same, plus the checks that only
the owner's authorization allows. Nothing is locked here.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from app.domain.calendar import MONTHS_ES, NATIONAL, Holiday
from app.domain.diagnosis import (
    FEW_PHOTOS,
    FEW_PHOTOS_PENALTY,
    NO_HOURS,
    NO_PHONE,
    NO_PHOTOS_PENALTY,
    NO_SECONDARY_CATEGORIES,
    NO_SERVICE_ATTRIBUTES_PENALTY,
    NO_WEBSITE,
    NOT_OPERATIONAL,
    SOME_PHOTOS_PENALTY,
    Diagnosis,
    Finding,
    Impact,
    few_reviews_finding,
    low_rating_finding,
    score_and_rank,
)
from app.domain.profile_summary import ProfileFacts, field_label

HOLIDAY_WINDOW_DAYS = 30
HOLIDAY_URGENT_DAYS = 7  # a holiday this close without hours is a high-impact finding
SHORT_DESCRIPTION = 150  # characters
MAX_DESCRIPTION = 750  # what Google accepts
ENOUGH_PHOTOS = 10


@dataclass(frozen=True)
class ConnectedProfile:
    """The stored facts of a profile and the figures of its summary."""

    name: str  # title of the profile as the connection listed it
    facts: ProfileFacts
    review_count: int = 0
    average_rating: float | None = None
    unanswered_count: int = 0
    unanswered_low_count: int = 0
    has_regular_hours: bool = False
    google_changed_fields: tuple[str, ...] = ()
    place_id: str = ""  # from the connection; used when the facts carry none


def _join_es(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} y {parts[-1]}"


def _day_es(day: date) -> str:
    return f"{day.day} de {MONTHS_ES[day.month - 1]}"


def _unanswered(count: int, low: int) -> Finding | None:
    if count <= 0:
        return None
    title = (
        "Tienes 1 reseña sin responder" if count == 1 else f"Tienes {count} reseñas sin responder"
    )
    if low <= 0:
        return Finding(
            "unanswered_reviews",
            title,
            "Responder demuestra que hay alguien detrás y da confianza a quien te compara.",
            Impact.MEDIUM,
            8,
        )
    if count == 1:
        which = "Es de 1 o 2 estrellas."
    elif low == 1:
        which = "Una es de 1 o 2 estrellas."
    else:
        which = f"{low} son de 1 o 2 estrellas."
    return Finding(
        "unanswered_reviews",
        title,
        f"{which} Una crítica sin respuesta es lo que más frena a quien te compara.",
        Impact.HIGH,
        15,
    )


def holidays_without_hours(
    holidays: Iterable[Holiday], special_dates: Iterable[date], today: date
) -> tuple[date, ...]:
    """National holidays of the next 30 days for which the profile has no special hours.

    Only national ones: the region of the business is not known yet, and a regional
    holiday of another region would be a false alarm.
    """
    covered = set(special_dates)
    last = today + timedelta(days=HOLIDAY_WINDOW_DAYS)
    return tuple(
        sorted(
            {
                h.date
                for h in holidays
                if h.scope == NATIONAL
                and h.verified
                and h.municipality is None
                and today <= h.date <= last
                and h.date not in covered
            }
        )
    )


def _holiday_hours(missing: tuple[date, ...], today: date) -> Finding | None:
    if not missing:
        return None
    if len(missing) == 1:
        title = f"Sin horario para el festivo del {_day_es(missing[0])}"
        what = f"El {_day_es(missing[0])} es festivo nacional"
    else:
        title = f"Sin horario para {len(missing)} festivos próximos"
        days = _join_es([f"el {_day_es(d)}" for d in missing])
        what = f"{days[0].upper()}{days[1:]} son festivos nacionales"
    urgent = (missing[0] - today).days <= HOLIDAY_URGENT_DAYS
    return Finding(
        "no_holiday_hours",
        title,
        f"{what} y no has indicado si abres. "
        "Google avisa a quien te busca de que tu horario puede no ser el real.",
        Impact.HIGH if urgent else Impact.MEDIUM,
        8,
    )


def _description(length: int) -> Finding | None:
    if length <= 0:
        return Finding(
            "no_description",
            "No tienes descripción",
            "Es tu sitio para contar qué haces y qué te distingue. "
            f"Google admite hasta {MAX_DESCRIPTION} caracteres.",
            Impact.MEDIUM,
            8,
        )
    if length < SHORT_DESCRIPTION:
        return Finding(
            "short_description",
            "Tu descripción es muy corta",
            f"Tiene {length} caracteres de los {MAX_DESCRIPTION} que admite Google. "
            "Cuenta qué haces, para quién y qué te distingue.",
            Impact.LOW,
            4,
        )
    return None


def _google_changed(fields: tuple[str, ...]) -> Finding | None:
    labels = list(dict.fromkeys(field_label(f) for f in fields))
    if not labels:
        return None
    return Finding(
        "google_changed",
        "Google ha cambiado datos de tu ficha",
        f"Ha cambiado: {_join_es(labels)}. Comprueba que lo que se ve ahora es correcto.",
        Impact.MEDIUM,
        8,
    )


def _photos(count: int | None) -> Finding | None:
    # The count is of what the owner uploaded: photos added by customers are not in it.
    if count is None or count >= ENOUGH_PHOTOS:
        return None
    if count == 0:
        return Finding(
            "no_photos",
            "No has subido ninguna foto",
            "Es lo primero que mira quien compara dos negocios.",
            Impact.HIGH,
            NO_PHOTOS_PENALTY,
        )
    if count < FEW_PHOTOS:
        return Finding(
            "few_photos",
            "Solo has subido 1 foto" if count == 1 else f"Solo has subido {count} fotos",
            "Las fichas con más fotos reciben más visitas y más rutas.",
            Impact.MEDIUM,
            FEW_PHOTOS_PENALTY,
        )
    return Finding(
        "some_photos",
        f"Has subido {count} fotos; hay margen",
        "Añadir fotos recientes mantiene la ficha viva.",
        Impact.LOW,
        SOME_PHOTOS_PENALTY,
    )


def _collect(p: ConnectedProfile, holidays: Iterable[Holiday], today: date) -> list[Finding]:
    facts = p.facts
    # Holiday hours make no sense on a profile without regular hours or marked as closed:
    # those two findings already say what to fix first.
    missing: tuple[date, ...] = ()
    if p.has_regular_hours and facts.is_open:
        missing = holidays_without_hours(holidays, facts.special_dates, today)
    # With the same impact and weight, the order here is the order shown.
    found: list[Finding | None] = [
        None if facts.is_open else NOT_OPERATIONAL,
        None if p.has_regular_hours else NO_HOURS,
        _unanswered(p.unanswered_count, p.unanswered_low_count),
        _holiday_hours(missing, today),
        _photos(facts.photo_count),
        low_rating_finding(p.average_rating, p.review_count),
        few_reviews_finding(p.review_count),
        _description(facts.description_length),
        _google_changed(p.google_changed_fields),
        None if facts.has_website else NO_WEBSITE,
        None if facts.has_phone else NO_PHONE,
        None if facts.additional_categories > 0 else NO_SECONDARY_CATEGORIES,
    ]
    if facts.attribute_count == 0:
        found.append(
            Finding(
                "no_service_attributes",
                "No indicas tus servicios",
                "Accesibilidad, formas de pago, servicios: son filtros que la gente usa al buscar.",
                Impact.LOW,
                NO_SERVICE_ATTRIBUTES_PENALTY,
            )
        )
    return [f for f in found if f is not None]


def diagnose_profile(
    profile: ConnectedProfile, holidays: Iterable[Holiday], today: date
) -> Diagnosis:
    facts = profile.facts
    score, label, findings = score_and_rank(_collect(profile, holidays, today))
    return Diagnosis(
        place_id=facts.place_id or profile.place_id,
        name=facts.title or profile.name,
        address=facts.address,
        category=facts.category,
        score=score,
        label=label,
        findings=findings,
        locked_checks=(),
        maps_url=facts.maps_url,
    )
