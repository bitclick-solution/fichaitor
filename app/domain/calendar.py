"""Key dates calendar: pure rules, no I/O.

Decides which holidays, commercial dates and seasons are worth proposing to a
business today, and words the question. The data (official holidays, the
table of commercial dates, the sector map) is loaded by
`app.adapters.calendar`; here it only arrives as plain values.

Holidays are taken exactly as the yearly BOE resolution lists them. When a
national holiday falls on a Sunday, the resolution already says which regions
move it to the Monday and which replace it with another day, so nothing here
shifts dates on its own.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum

NATIONAL = "national"
HOLIDAY_EVENT_ID = "holiday"
HOLIDAY_LEAD_DAYS = 5
COMMERCIAL_LEAD_DAYS = 14
SEASON_LEAD_DAYS = 21
COMMERCIAL_EXPIRY_DAYS = 2  # a commercial date stops being proposed 2 days before
GROUP_GAP_DAYS = 3  # key dates this close to each other go in one proposal
MAX_LOCAL_HOLIDAYS_PER_YEAR = 2
DISMISSALS_TO_MUTE = 2  # consecutive years

WEEKDAYS_ES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
MONTHS_ES = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)


class Sector(StrEnum):
    HOSPITALITY = "hospitality"
    RETAIL = "retail"
    BEAUTY_HEALTH = "beauty_health"
    PROFESSIONAL_SERVICES = "professional_services"
    OTHER = "other"


class KeyDateKind(StrEnum):
    HOLIDAY = "holiday"
    COMMERCIAL = "commercial"
    SEASON = "season"


class ProposalKind(StrEnum):
    HOLIDAY = "holiday"
    COMMERCIAL = "commercial"
    SEASON = "season"
    COMBINED = "combined"


class RuleKind(StrEnum):
    FIXED = "fixed"  # same month and day every year
    NTH_WEEKDAY = "nth_weekday"  # nth weekday of a month
    EASTER = "easter"  # Easter Sunday


# --- date arithmetic -------------------------------------------------------


def easter_sunday(year: int) -> date:
    """Gregorian Easter Sunday (anonymous algorithm of Meeus/Jones/Butcher)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The nth (1-based) given weekday (0 is Monday) of a month."""
    first = date(year, month, 1)
    day = first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    if n < 1 or day.month != month:
        raise ValueError(f"no hay {n} días de la semana {weekday} en {month}/{year}")
    return day


def black_friday(year: int) -> date:
    """The day after the fourth Thursday of November (US Thanksgiving).

    It is usually the fourth Friday of November, but not when November starts
    on a Friday: in 2019 and 2024 it was the 29th, the fifth Friday.
    """
    return nth_weekday(year, 11, 3, 4) + timedelta(days=1)


def format_date_es(day: date) -> str:
    """'martes 8 de diciembre', independent of the system locale."""
    return f"{WEEKDAYS_ES[day.weekday()]} {day.day} de {MONTHS_ES[day.month - 1]}"


# --- data ------------------------------------------------------------------


@dataclass(frozen=True)
class DateRule:
    kind: RuleKind
    month: int = 0
    day: int = 0
    weekday: int = 0  # 0 is Monday
    n: int = 0
    offset_days: int = 0

    def resolve(self, year: int) -> date:
        if self.kind is RuleKind.FIXED:
            base = date(year, self.month, self.day)
        elif self.kind is RuleKind.NTH_WEEKDAY:
            base = nth_weekday(year, self.month, self.weekday, self.n)
        else:
            base = easter_sunday(year)
        return base + timedelta(days=self.offset_days)


@dataclass(frozen=True)
class Holiday:
    """An official holiday, as published.

    `scope` is "national" or the ISO 3166-2:ES code of the region ("AN").
    `municipality` (INE code) narrows a preloaded local holiday to one town.
    `substitutable` is what the BOE marks with **: a national holiday the
    region could have replaced and did not. `verified` is False when the
    region's list has not been checked against the official text; those
    holidays are never proposed.
    """

    date: date
    name: str
    scope: str
    substitutable: bool = False
    municipality: str | None = None
    verified: bool = True


@dataclass(frozen=True)
class CommercialEvent:
    """A commercial date or a season, with the rule that gives its date."""

    id: str
    name: str
    kind: KeyDateKind  # COMMERCIAL or SEASON
    rule: DateRule
    lead_days: int
    sectors: frozenset[Sector]
    question: str  # with a {date} placeholder
    duration_days: int = 1


@dataclass(frozen=True)
class Calendar:
    holidays: tuple[Holiday, ...] = ()
    events: tuple[CommercialEvent, ...] = ()


@dataclass(frozen=True)
class SectorMap:
    """Google category (Places type) to internal sector."""

    types: Mapping[str, Sector] = field(default_factory=dict)
    suffixes: tuple[tuple[str, Sector], ...] = ()


@dataclass(frozen=True)
class LocalHoliday:
    """A local holiday as the owner declares it at sign-up (two per year).

    There is no national source for these, so the owner's answer is the data.
    """

    date: date
    name: str = ""


@dataclass(frozen=True)
class Dismissal:
    """The owner said no to one occurrence of a key date.

    `event_id` is the commercial event id ("black_friday") or "holiday";
    `date` is the date of that occurrence.
    """

    event_id: str
    date: date


@dataclass(frozen=True)
class Business:
    region: str  # ISO 3166-2:ES code, e.g. "AN"
    sector: Sector
    municipality_code: str | None = None  # INE code
    local_holidays: tuple[LocalHoliday, ...] = ()
    dismissed: tuple[Dismissal, ...] = ()
    accepted_events: frozenset[str] = frozenset()  # events the owner used before


@dataclass(frozen=True)
class KeyDate:
    """One occurrence of a holiday, commercial date or season."""

    event_id: str
    kind: KeyDateKind
    date: date
    end: date
    name: str
    lead_days: int
    question: str = ""
    local: bool = False

    @property
    def key(self) -> str:
        return f"{self.event_id}:{self.date.isoformat()}"

    @property
    def notice_date(self) -> date:
        return self.date - timedelta(days=self.lead_days)

    @property
    def expires_on(self) -> date:
        """First day on which it is too late to propose it."""
        if self.kind is KeyDateKind.COMMERCIAL:
            return self.date - timedelta(days=COMMERCIAL_EXPIRY_DAYS)
        return self.date


@dataclass(frozen=True)
class Proposal:
    """What the agent should ask today: one key date or several grouped."""

    kind: ProposalKind
    date: date  # earliest key date in the group
    items: tuple[KeyDate, ...]
    text: str

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.items)

    @property
    def expires_on(self) -> date:
        return max(item.expires_on for item in self.items)


# --- sector ----------------------------------------------------------------


def sector_for(primary_type: str, types: Iterable[str], sector_map: SectorMap) -> Sector:
    """Internal sector of a profile, from its Google category.

    The primary category decides; the secondary ones are only a fallback.
    """
    for google_type in (primary_type, *types):
        if not google_type:
            continue
        if google_type in sector_map.types:
            return sector_map.types[google_type]
        for suffix, sector in sector_map.suffixes:
            if google_type.endswith(suffix):
                return sector
    return Sector.OTHER


# --- local holidays --------------------------------------------------------


def check_local_holidays(holidays: Iterable[LocalHoliday]) -> None:
    """Reject a declaration with repeated dates or more than two per year."""
    per_year: dict[int, int] = {}
    seen: set[date] = set()
    for holiday in holidays:
        if holiday.date in seen:
            raise ValueError(f"fiesta local repetida: {format_date_es(holiday.date)}")
        seen.add(holiday.date)
        per_year[holiday.date.year] = per_year.get(holiday.date.year, 0) + 1
    for year, count in sorted(per_year.items()):
        if count > MAX_LOCAL_HOLIDAYS_PER_YEAR:
            raise ValueError(
                f"hay {count} fiestas locales en {year}; "
                f"son {MAX_LOCAL_HOLIDAYS_PER_YEAR} como máximo"
            )


# --- which key dates apply to a business -----------------------------------


def _holiday_applies(holiday: Holiday, business: Business) -> bool:
    if not holiday.verified:
        return False
    if holiday.scope != NATIONAL and holiday.scope != business.region:
        return False
    return holiday.municipality is None or holiday.municipality == business.municipality_code


def _holidays(business: Business, calendar: Calendar) -> list[KeyDate]:
    by_date: dict[date, KeyDate] = {}
    for holiday in calendar.holidays:
        if _holiday_applies(holiday, business):
            by_date.setdefault(
                holiday.date,
                KeyDate(
                    event_id=HOLIDAY_EVENT_ID,
                    kind=KeyDateKind.HOLIDAY,
                    date=holiday.date,
                    end=holiday.date,
                    name=holiday.name,
                    lead_days=HOLIDAY_LEAD_DAYS,
                    local=holiday.municipality is not None,
                ),
            )
    for declared in business.local_holidays:
        by_date.setdefault(
            declared.date,
            KeyDate(
                event_id=HOLIDAY_EVENT_ID,
                kind=KeyDateKind.HOLIDAY,
                date=declared.date,
                end=declared.date,
                name=declared.name or "Fiesta local",
                lead_days=HOLIDAY_LEAD_DAYS,
                local=True,
            ),
        )
    return list(by_date.values())


def is_muted(event_id: str, year: int, dismissed: Iterable[Dismissal]) -> bool:
    """True when the event was dismissed two years in a row before `year`.

    Once that happens it stays muted: the owner has to ask for it again (which
    clears the dismissals or adds the event to `accepted_events`).
    """
    years = {d.date.year for d in dismissed if d.event_id == event_id and d.date.year < year}
    return any(all(y + step in years for step in range(DISMISSALS_TO_MUTE)) for y in years)


def _events(business: Business, calendar: Calendar, years: Iterable[int]) -> list[KeyDate]:
    out: list[KeyDate] = []
    for event in calendar.events:
        accepted = event.id in business.accepted_events
        if business.sector not in event.sectors and not accepted:
            continue
        for year in years:
            if not accepted and is_muted(event.id, year, business.dismissed):
                continue
            start = event.rule.resolve(year)
            out.append(
                KeyDate(
                    event_id=event.id,
                    kind=event.kind,
                    date=start,
                    end=start + timedelta(days=event.duration_days - 1),
                    name=event.name,
                    lead_days=event.lead_days,
                    question=event.question,
                )
            )
    return out


def key_dates(business: Business, calendar: Calendar, years: Iterable[int]) -> tuple[KeyDate, ...]:
    """Every key date that concerns a business in the given years, by date.

    Excludes what the owner dismissed and what the learning rule has muted.
    """
    wanted = set(years)
    dismissed = {(d.event_id, d.date) for d in business.dismissed}
    found = [k for k in _holidays(business, calendar) if k.date.year in wanted]
    found += _events(business, calendar, sorted(wanted))
    kept = [k for k in found if (k.event_id, k.date) not in dismissed]
    return tuple(sorted(kept, key=lambda k: (k.date, k.kind is not KeyDateKind.HOLIDAY, k.key)))


# --- what to propose today -------------------------------------------------


def _gap_days(a: KeyDate, b: KeyDate) -> int:
    return max(0, (b.date - a.end).days, (a.date - b.end).days)


def _join_es(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} y {parts[-1]}"


def _holiday_word(items: list[KeyDate]) -> str:
    if all(item.local for item in items):
        return "fiesta local" if len(items) == 1 else "fiestas locales"
    return "festivo" if len(items) == 1 else "festivos"


def _text(items: tuple[KeyDate, ...]) -> str:
    holidays = [i for i in items if i.kind is KeyDateKind.HOLIDAY]
    others = [i for i in items if i.kind is not KeyDateKind.HOLIDAY]

    if not others:
        days = _join_es([f"el {format_date_es(h.date)}" for h in holidays])
        verb = "es" if len(holidays) == 1 else "son"
        sentence = f"{days} {verb} {_holiday_word(holidays)}. ¿Abrís?"
        return sentence[0].upper() + sentence[1:]

    if len(others) == 1 and not holidays:
        return others[0].question.format(date=format_date_es(others[0].date))

    parts = []
    for item in others:
        since = "desde el " if item.end > item.date else ""
        parts.append(f"{item.name} ({since}{format_date_es(item.date)})")
    parts += [f"{_holiday_word([h])} el {format_date_es(h.date)}" for h in holidays]
    if any(i.kind is KeyDateKind.COMMERCIAL for i in others):
        closing = "¿Tienes alguna oferta especial o cambia tu horario?"
    else:
        closing = "¿Cambia tu horario esos días?"
    return f"Se acercan varias fechas: {_join_es(parts)}. {closing}"


def _proposal(items: list[KeyDate]) -> Proposal:
    ordered = tuple(items)
    kinds = {item.kind for item in ordered}
    kind = ProposalKind(kinds.pop().value) if len(kinds) == 1 else ProposalKind.COMBINED
    return Proposal(kind=kind, date=ordered[0].date, items=ordered, text=_text(ordered))


def due_proposals(
    business: Business,
    today: date,
    calendar: Calendar,
    already_proposed: frozenset[str] = frozenset(),
) -> tuple[Proposal, ...]:
    """Key dates the agent should propose today, grouped.

    A key date is due from its notice date (date minus lead days) until it
    expires: a holiday or a season on the day itself, a commercial date two
    days before. `already_proposed` holds the `KeyDate.key` of what already
    has a proposal, so the same date is not asked twice.

    Dates close to each other go in a single proposal. A date that is not due
    yet but falls next to one that is (the holiday on Father's Day) is brought
    forward into the same proposal instead of producing a second message.
    """
    candidates = [
        k
        for k in key_dates(business, calendar, (today.year, today.year + 1))
        if k.key not in already_proposed and today < k.expires_on
    ]
    due = [k for k in candidates if k.notice_date <= today]
    if not due:
        return ()
    upcoming = [k for k in candidates if k.notice_date > today]
    pulled = [k for k in upcoming if any(_gap_days(k, d) <= GROUP_GAP_DAYS for d in due)]

    groups: list[list[KeyDate]] = []
    group_end = date.min
    for item in sorted(due + pulled, key=lambda k: (k.date, k.end)):
        if groups and (item.date - group_end).days <= GROUP_GAP_DAYS:
            groups[-1].append(item)
            group_end = max(group_end, item.end)
        else:
            groups.append([item])
            group_end = item.end
    return tuple(_proposal(group) for group in groups)
