"""Special hours of one day: what the owner chose for a key date, how the action keeps
it and how it becomes Google's `specialHours`. No I/O.

The owner chooses one of three things for a day (docs/fase1/guion-conversacional.md,
flow 3): closed, the usual hours, or other hours in one or two periods. The choice is
checked here, written as a canonical text (`SpecialHoursChange.to_text`) that the action
keeps unchanged, and read back by the executor, which turns it into Google's periods and
merges them with the special hours the profile already has: the other dates are never
touched.

Google's shapes (Business Information v1, `locations`):
- `specialHours.specialHourPeriods[]`: `startDate` (Date), `openTime` (TimeOfDay),
  `endDate` (Date, optional, at most one day after `startDate`), `closeTime` and
  `closed`.
- `regularHours.periods[]`: `openDay` ("MONDAY"), `openTime`, `closeDay`, `closeTime`.
https://developers.google.com/my-business/reference/businessinformation/rest/v1/accounts.locations
"""

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

KIND_CLOSED = "closed"
KIND_REGULAR = "regular"
KIND_PERIODS = "periods"
KINDS = (KIND_CLOSED, KIND_REGULAR, KIND_PERIODS)

MAX_PERIODS = 2  # "otro horario": one or two periods
# Attempts of the job set_special_hours. With the waits of docs/worker.md (30 s, 60 s,
# 120 s, 240 s, 480 s) it keeps trying for some 15 minutes; the guion (1.8) tries quietly
# for 10 minutes before telling the owner.
JOB_MAX_ATTEMPTS = 6
TEXT_MAX_BYTES = 4096  # action.text
WEEKDAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")
# Every special hour period Google may hold; a longer list is not trusted whole.
MAX_GOOGLE_PERIODS = 500

_CLOCK = re.compile(r"(\d{1,2}):(\d{2})")


class InvalidSpecialHours(ValueError):
    """The choice cannot be sent to Google as it is. The message is in Spanish and
    safe to show: it never quotes what was received."""


class NoRegularHours(ValueError):
    """'Horario normal' was chosen and the profile has no regular hours in Google."""


def parse_clock(value: object, *, closing: bool = False) -> int:
    """'9:00' or '09:00' to minutes. 24:00 is only valid as a closing time."""
    match = _CLOCK.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise InvalidSpecialHours("Las horas van con el formato 10:00.")
    hours, minutes = int(match.group(1)), int(match.group(2))
    if minutes > 59 or hours > 24 or (hours == 24 and (minutes or not closing)):
        raise InvalidSpecialHours("Esa hora no existe.")
    return hours * 60 + minutes


def format_clock(minutes: int) -> str:
    """Same form as the conversation's: '9:00', '17:30'."""
    return f"{minutes // 60}:{minutes % 60:02d}"


@dataclass(frozen=True)
class SpecialHoursChange:
    """What the owner confirmed for one day. `periods` only with KIND_PERIODS."""

    day: date
    kind: str
    periods: tuple[tuple[str, str], ...] = ()

    def to_text(self) -> str:
        """The canonical text the action keeps: the same choice is always the same text."""
        return json.dumps(
            {
                "date": self.day.isoformat(),
                "kind": self.kind,
                "periods": [list(p) for p in self.periods],
            },
            separators=(",", ":"),
            sort_keys=True,
        )


def checked(day: date, kind: str, periods: Iterable[Sequence[str]] = ()) -> SpecialHoursChange:
    """A change, or InvalidSpecialHours. Periods in order, not overlapping, each one
    opening before it closes, one or two of them. Never guesses: a period that goes past
    midnight ("de 20 a 2") is refused; the panel or Google take those."""
    if kind not in KINDS:
        raise InvalidSpecialHours("Elige cerrado, horario normal u otro horario.")
    found = [tuple(p) for p in periods]
    if kind != KIND_PERIODS:
        if found:
            raise InvalidSpecialHours("Con cerrado u horario normal no van horas.")
        return SpecialHoursChange(day, kind)
    if not 1 <= len(found) <= MAX_PERIODS:
        raise InvalidSpecialHours("Indica una o dos franjas de horario.")
    clean: list[tuple[int, int]] = []
    for period in found:
        if len(period) != 2:
            raise InvalidSpecialHours("Cada franja lleva una hora de apertura y una de cierre.")
        start = parse_clock(period[0])
        end = parse_clock(period[1], closing=True)
        if start >= end:
            raise InvalidSpecialHours(
                "Cada franja tiene que abrir antes de cerrar, el mismo día. Si cierras "
                "después de medianoche, ponlo en Google."
            )
        if clean and start <= clean[-1][1]:
            raise InvalidSpecialHours("Las franjas van en orden y no se pueden solapar.")
        clean.append((start, end))
    return SpecialHoursChange(
        day, KIND_PERIODS, tuple((format_clock(s), format_clock(e)) for s, e in clean)
    )


def from_entries(entries: object) -> SpecialHoursChange:
    """The payload the conversation builds (`{"date", "closed"}`, `{"date", "regular"}`
    or one `{"date", "open", "close"}` per period), checked."""
    if not isinstance(entries, list) or not entries:
        raise InvalidSpecialHours("No hay ningún horario que enviar.")
    days = set()
    for entry in entries:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("date"), str):
            raise InvalidSpecialHours("Falta el día.")
        days.add(entry["date"])
    if len(days) != 1:
        raise InvalidSpecialHours("Un cambio de horario es de un solo día.")
    try:
        day = date.fromisoformat(days.pop())
    except ValueError:
        raise InvalidSpecialHours("Ese día no existe.") from None
    first = entries[0]
    if first.get("closed") is True and len(entries) == 1 and set(first) == {"date", "closed"}:
        return checked(day, KIND_CLOSED)
    if first.get("regular") is True and len(entries) == 1 and set(first) == {"date", "regular"}:
        return checked(day, KIND_REGULAR)
    if any(set(e) != {"date", "open", "close"} for e in entries):
        raise InvalidSpecialHours("El horario no tiene la forma esperada.")
    return checked(day, KIND_PERIODS, [(e["open"], e["close"]) for e in entries])


def from_text(text: str) -> SpecialHoursChange:
    """The change an action keeps. Strict: anything but the canonical text is refused."""
    try:
        raw = json.loads(text)
    except (TypeError, ValueError):
        raise InvalidSpecialHours("El cambio guardado no se puede leer.") from None
    if not isinstance(raw, dict) or set(raw) != {"date", "kind", "periods"}:
        raise InvalidSpecialHours("El cambio guardado no se puede leer.")
    try:
        day = date.fromisoformat(raw["date"])
    except (TypeError, ValueError):
        raise InvalidSpecialHours("El cambio guardado no se puede leer.") from None
    periods = raw["periods"]
    if not isinstance(periods, list) or not all(isinstance(p, list) for p in periods):
        raise InvalidSpecialHours("El cambio guardado no se puede leer.")
    change = checked(day, raw["kind"], periods)
    if change.to_text() != text:
        raise InvalidSpecialHours("El cambio guardado no se puede leer.")
    return change


def describe(change: SpecialHoursChange) -> str:
    """Spanish, for the panel and the summaries: 'cerrado', 'horario normal',
    'de 10:00 a 14:00 y de 17:00 a 20:00'."""
    if change.kind == KIND_CLOSED:
        return "cerrado"
    if change.kind == KIND_REGULAR:
        return "horario normal"
    return " y ".join(f"de {start} a {end}" for start, end in change.periods)


# --- Google's shapes ---------------------------------------------------------------


def google_date(day: date) -> dict[str, int]:
    return {"year": day.year, "month": day.month, "day": day.day}


def google_time(minutes: int) -> dict[str, int]:
    return {"hours": minutes // 60, "minutes": minutes % 60}


def read_date(value: object) -> date | None:
    if not isinstance(value, Mapping):
        return None
    try:
        return date(int(value["year"]), int(value["month"]), int(value["day"]))
    except (KeyError, TypeError, ValueError):
        return None


def read_time(value: object) -> int | None:
    """Minutes of a TimeOfDay. Google leaves out the fields that are zero: {} is 0:00."""
    if not isinstance(value, Mapping):
        return None
    try:
        hours, minutes = int(value.get("hours", 0)), int(value.get("minutes", 0))
    except (TypeError, ValueError):
        return None
    if not (0 <= hours <= 24 and 0 <= minutes <= 59):
        return None
    return hours * 60 + minutes


def regular_periods_for(regular: Sequence[Mapping[str, Any]], day: date) -> list[dict[str, Any]]:
    """The regular hours of that weekday as special hour periods of that date.

    NoRegularHours when the profile has no regular hours at all. A weekday without
    regular periods is a day the business is normally closed: it stays closed. A period
    that closes the next day (a bar open until 2:00) ends on the next date, as Google
    allows; one that spans longer is not copied.
    """
    if not regular:
        raise NoRegularHours("la ficha no tiene horario normal en Google")
    weekday, next_day = WEEKDAYS[day.weekday()], WEEKDAYS[(day.weekday() + 1) % 7]
    out: list[dict[str, Any]] = []
    for period in regular:
        if not isinstance(period, Mapping) or period.get("openDay") != weekday:
            continue
        opens, closes = read_time(period.get("openTime")), read_time(period.get("closeTime"))
        close_day = period.get("closeDay", weekday)
        if opens is None or closes is None:
            continue
        if close_day == weekday:
            end = day
        elif close_day == next_day:
            end = day + timedelta(days=1)
        else:
            continue
        out.append(
            {
                "startDate": google_date(day),
                "openTime": google_time(opens),
                "endDate": google_date(end),
                "closeTime": google_time(closes),
                "closed": False,
            }
        )
    if not out:
        return [{"startDate": google_date(day), "closed": True}]
    return sorted(out, key=lambda p: read_time(p["openTime"]) or 0)


def weekly_hours(regular: Sequence[Mapping[str, Any]]) -> tuple[tuple[int, str, str], ...]:
    """Google's regular periods as (weekday, "HH:MM", "HH:MM"), Monday 0, in order. A
    period that closes the next day keeps its closing time ("20:00", "02:00"). What
    cannot be read is left out. For the panel's "Horario normal"."""
    out = []
    for period in regular:
        if not isinstance(period, Mapping) or period.get("openDay") not in WEEKDAYS:
            continue
        opens, closes = read_time(period.get("openTime")), read_time(period.get("closeTime"))
        if opens is None or closes is None:
            continue
        out.append((WEEKDAYS.index(period["openDay"]), _hhmm(opens), _hhmm(closes)))
    return tuple(sorted(out))


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def google_periods(
    change: SpecialHoursChange, regular: Sequence[Mapping[str, Any]] = ()
) -> list[dict[str, Any]]:
    """Google's special hour periods for the day of the change."""
    if change.kind == KIND_CLOSED:
        return [{"startDate": google_date(change.day), "closed": True}]
    if change.kind == KIND_REGULAR:
        return regular_periods_for(regular, change.day)
    return [
        {
            "startDate": google_date(change.day),
            "openTime": google_time(parse_clock(start)),
            "endDate": google_date(change.day),
            "closeTime": google_time(parse_clock(end, closing=True)),
            "closed": False,
        }
        for start, end in change.periods
    ]


def _normal(period: Mapping[str, Any]) -> tuple:
    """A period as Google may echo it (zero fields left out, `closed` false omitted)."""
    start = read_date(period.get("startDate"))
    end = read_date(period.get("endDate")) or start
    closed = period.get("closed") is True
    if closed:
        return (start, end, True, None, None)
    return (
        start,
        end,
        False,
        read_time(period.get("openTime")),
        read_time(period.get("closeTime")),
    )


def periods_of(periods: Iterable[Mapping[str, Any]], day: date) -> list[Mapping[str, Any]]:
    return [p for p in periods if isinstance(p, Mapping) and read_date(p.get("startDate")) == day]


def already_there(
    current: Sequence[Mapping[str, Any]], wanted: Sequence[Mapping[str, Any]]
) -> bool:
    """Whether the special hours of that day in Google are already exactly these: a
    retry whose earlier attempt arrived does not write again."""
    if not wanted:
        return False
    day = read_date(wanted[0].get("startDate"))
    if day is None:
        return False
    have = sorted((_normal(p) for p in periods_of(current, day)), key=repr)
    return have == sorted((_normal(p) for p in wanted), key=repr)


def merge(
    current: Sequence[Mapping[str, Any]], day: date, wanted: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The whole list to send: every period Google has, except those that start on that
    day, which are replaced by `wanted`. Nothing of another date is dropped or changed,
    not even a period this code cannot read."""
    kept = [
        dict(p)
        for p in current
        if not (isinstance(p, Mapping) and read_date(p.get("startDate")) == day)
    ]
    merged = kept + [dict(p) for p in wanted]

    def order(period: Mapping[str, Any]) -> tuple:
        start = read_date(period.get("startDate"))
        return (start or date.max, read_time(period.get("openTime")) or 0)

    return sorted(merged, key=order)
