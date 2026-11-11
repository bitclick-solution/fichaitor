"""Reads the calendar data files and validates their shape at start-up.

Files, all under `data/calendar/`:

- `<year>-festivos.json`: national and regional holidays of one year, from
  the BOE resolution (see `boe_import`).
- `<year>-locales-<region>.json`: optional preloaded local holidays of one
  region, one entry per municipality (INE code).
- `fechas-comerciales.json`: commercial dates and seasons, by rule.
- `sectores.json`: Google category to internal sector.

Any problem raises `CalendarDataError` naming the file and the field, so a
bad edit stops the service at start-up instead of producing wrong notices.
"""

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from app.domain.calendar import (
    NATIONAL,
    Calendar,
    CommercialEvent,
    DateRule,
    Holiday,
    KeyDateKind,
    RuleKind,
    Sector,
    SectorMap,
)

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "calendar"
COMMERCIAL_FILE = "fechas-comerciales.json"
SECTORS_FILE = "sectores.json"

REGION_CODES = frozenset(
    {
        "AN",
        "AR",
        "AS",
        "IB",
        "CN",
        "CB",
        "CM",
        "CL",
        "CT",
        "EX",
        "GA",
        "MD",
        "MC",
        "NC",
        "PV",
        "RI",
        "VC",
        "CE",
        "ML",
    }
)

_HOLIDAYS_NAME = re.compile(r"^(\d{4})-festivos\.json$")
_LOCALS_NAME = re.compile(r"^(\d{4})-locales-([A-Z]{2})\.json$")
_INE_CODE = re.compile(r"^\d{5}$")
_EVENT_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_EVENT_KINDS = (KeyDateKind.COMMERCIAL, KeyDateKind.SEASON)


class CalendarDataError(ValueError):
    """A calendar data file is missing or malformed."""


@dataclass(frozen=True)
class CalendarData:
    calendar: Calendar
    sector_map: SectorMap
    holiday_years: tuple[int, ...]
    unverified: tuple[tuple[int, str], ...]  # (year, region) pending verification


class _File:
    """Access to one file's content with errors that name the file and field."""

    def __init__(self, path: Path) -> None:
        self.name = path.name
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise CalendarDataError(f"{self.name}: no existe el archivo ({path})") from None
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CalendarDataError(f"{self.name}: no es JSON válido ({exc})") from exc
        if not isinstance(self.data, dict):
            raise CalendarDataError(f"{self.name}: se esperaba un objeto JSON en la raíz")

    def fail(self, where: str, problem: str) -> CalendarDataError:
        return CalendarDataError(f"{self.name}: {where}: {problem}")

    def get(self, obj: Any, key: str, kind: type, where: str) -> Any:
        if not isinstance(obj, dict):
            raise self.fail(where, "se esperaba un objeto")
        if key not in obj:
            raise self.fail(where, f"falta el campo '{key}'")
        value = obj[key]
        # bool is an int in Python; a true/false where a number goes is a mistake
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise self.fail(f"{where}.{key}", f"se esperaba {_KIND_NAMES[kind]}")
        return value

    def text(self, obj: Any, key: str, where: str) -> str:
        value = self.get(obj, key, str, where)
        if not value.strip():
            raise self.fail(f"{where}.{key}", "no puede estar vacío")
        return value

    def day(self, obj: Any, key: str, where: str) -> date:
        value = self.get(obj, key, str, where)
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise self.fail(
                f"{where}.{key}", f"'{value}' no es una fecha válida (AAAA-MM-DD)"
            ) from None


_KIND_NAMES = {
    str: "un texto",
    int: "un número entero",
    bool: "true o false",
    list: "una lista",
    dict: "un objeto",
}


def _load_holidays(path: Path, year: int) -> tuple[list[Holiday], list[str]]:
    f = _File(path)
    if f.get(f.data, "year", int, "raíz") != year:
        raise f.fail("year", f"no coincide con el nombre del archivo ({year})")

    source = f.get(f.data, "source", dict, "raíz")
    for key in ("title", "reference", "url"):
        f.text(source, key, "source")
    f.day(source, "published", "source")
    f.day(source, "consulted", "source")

    coverage = f.get(f.data, "coverage", dict, "raíz")
    start, end = f.day(coverage, "from", "coverage"), f.day(coverage, "to", "coverage")
    if start > end or start.year != year or end.year != year:
        raise f.fail("coverage", f"debe ser un intervalo dentro de {year}")

    regions = f.get(f.data, "regions", dict, "raíz")
    if set(regions) != REGION_CODES:
        wrong = sorted(set(regions) ^ REGION_CODES)
        raise f.fail("regions", f"deben estar los 19 territorios; sobran o faltan: {wrong}")
    unverified = sorted(
        code for code in regions if not f.get(regions[code], "verified", bool, f"regions.{code}")
    )

    holidays: list[Holiday] = []
    seen: set[tuple[date, str]] = set()
    for i, raw in enumerate(f.get(f.data, "holidays", list, "raíz")):
        where = f"holidays[{i}]"
        day = f.day(raw, "date", where)
        scope = f.get(raw, "scope", str, where)
        if scope != NATIONAL and scope not in REGION_CODES:
            raise f.fail(f"{where}.scope", f"'{scope}' no es 'national' ni un código de comunidad")
        if not start <= day <= end:
            raise f.fail(f"{where}.date", f"{day.isoformat()} queda fuera de la cobertura")
        if (day, scope) in seen:
            raise f.fail(where, f"fiesta duplicada: {day.isoformat()} en {scope}")
        seen.add((day, scope))
        holidays.append(
            Holiday(
                date=day,
                name=f.text(raw, "name", where),
                scope=scope,
                substitutable=f.get(raw, "substitutable", bool, where),
                verified=scope not in unverified,
            )
        )
    for day, scope in seen:
        if scope != NATIONAL and (day, NATIONAL) in seen:
            raise f.fail("holidays", f"{day.isoformat()} está como nacional y también en {scope}")

    for i, raw in enumerate(f.data.get("subregional", [])):
        where = f"subregional[{i}]"
        if f.day(raw, "date", where).year != year:
            raise f.fail(f"{where}.date", f"no es una fecha de {year}")
        if f.get(raw, "scope", str, where) not in REGION_CODES:
            raise f.fail(f"{where}.scope", "no es un código de comunidad")
        f.text(raw, "name", where)
        f.text(raw, "area", where)
    return holidays, unverified


def _load_locals(path: Path, year: int, region: str) -> list[Holiday]:
    f = _File(path)
    if f.get(f.data, "year", int, "raíz") != year:
        raise f.fail("year", f"no coincide con el nombre del archivo ({year})")
    if f.get(f.data, "region", str, "raíz") != region:
        raise f.fail("region", f"no coincide con el nombre del archivo ({region})")
    if region not in REGION_CODES:
        raise f.fail("region", f"'{region}' no es un código de comunidad")
    source = f.get(f.data, "source", dict, "raíz")
    f.text(source, "title", "source")
    f.text(source, "url", "source")
    f.day(source, "consulted", "source")

    holidays: list[Holiday] = []
    per_town: dict[str, set[date]] = {}
    for i, raw in enumerate(f.get(f.data, "holidays", list, "raíz")):
        where = f"holidays[{i}]"
        day = f.day(raw, "date", where)
        town = f.get(raw, "municipality", str, where)
        if day.year != year:
            raise f.fail(f"{where}.date", f"{day.isoformat()} no es una fecha de {year}")
        if not _INE_CODE.match(town):
            raise f.fail(f"{where}.municipality", "debe ser el código INE de 5 cifras")
        if day in per_town.setdefault(town, set()):
            raise f.fail(where, f"fiesta duplicada: {day.isoformat()} en {town}")
        per_town[town].add(day)
        holidays.append(
            Holiday(date=day, name=f.text(raw, "name", where), scope=region, municipality=town)
        )
    return holidays


def _rule(f: _File, raw: Any, where: str) -> DateRule:
    kind_text = f.get(raw, "kind", str, where)
    try:
        kind = RuleKind(kind_text)
    except ValueError:
        options = ", ".join(k.value for k in RuleKind)
        raise f.fail(
            f"{where}.kind", f"'{kind_text}' no es una regla conocida ({options})"
        ) from None
    offset = f.get(raw, "offset_days", int, where) if "offset_days" in raw else 0
    if kind is RuleKind.EASTER:
        rule = DateRule(kind=kind, offset_days=offset)
    else:
        month = f.get(raw, "month", int, where)
        if not 1 <= month <= 12:
            raise f.fail(f"{where}.month", "debe estar entre 1 y 12")
        if kind is RuleKind.FIXED:
            rule = DateRule(
                kind=kind, month=month, day=f.get(raw, "day", int, where), offset_days=offset
            )
        else:
            weekday, n = f.get(raw, "weekday", int, where), f.get(raw, "n", int, where)
            if not 0 <= weekday <= 6:
                raise f.fail(f"{where}.weekday", "debe estar entre 0 (lunes) y 6 (domingo)")
            if not 1 <= n <= 4:
                raise f.fail(f"{where}.n", "debe estar entre 1 y 4")
            rule = DateRule(kind=kind, month=month, weekday=weekday, n=n, offset_days=offset)
    try:
        rule.resolve(2028)  # a leap year, so 29 February is accepted and 30 February is not
    except ValueError:
        raise f.fail(where, "la regla no da una fecha válida") from None
    return rule


def _load_events(path: Path) -> list[CommercialEvent]:
    f = _File(path)
    events: list[CommercialEvent] = []
    ids: set[str] = set()
    for i, raw in enumerate(f.get(f.data, "events", list, "raíz")):
        where = f"events[{i}]"
        event_id = f.text(raw, "id", where)
        if not _EVENT_ID.match(event_id) or event_id == "holiday":
            raise f.fail(f"{where}.id", f"'{event_id}' no es un identificador válido")
        if event_id in ids:
            raise f.fail(f"{where}.id", f"identificador duplicado: '{event_id}'")
        ids.add(event_id)
        where = f"events[{i}] ({event_id})"

        kind_text = f.get(raw, "type", str, where)
        if kind_text not in {k.value for k in _EVENT_KINDS}:
            raise f.fail(f"{where}.type", f"'{kind_text}' debe ser 'commercial' o 'season'")
        lead_days = f.get(raw, "lead_days", int, where)
        if not 1 <= lead_days <= 60:
            raise f.fail(f"{where}.lead_days", "debe estar entre 1 y 60")
        duration = f.get(raw, "duration_days", int, where) if "duration_days" in raw else 1
        if not 1 <= duration <= 31:
            raise f.fail(f"{where}.duration_days", "debe estar entre 1 y 31")

        sector_names = f.get(raw, "sectors", list, where)
        try:
            sectors = frozenset(Sector(name) for name in sector_names)
        except ValueError:
            raise f.fail(f"{where}.sectors", f"sector desconocido en {sector_names}") from None
        if not sectors:
            raise f.fail(f"{where}.sectors", "tiene que haber al menos un sector")

        question = f.text(raw, "question", where)
        try:
            question.format(date="lunes 1 de enero")
        except (KeyError, IndexError, ValueError):
            raise f.fail(f"{where}.question", "solo admite el marcador {date}") from None
        if "{date}" not in question:
            raise f.fail(f"{where}.question", "tiene que incluir el marcador {date}")

        events.append(
            CommercialEvent(
                id=event_id,
                name=f.text(raw, "name", where),
                kind=KeyDateKind(kind_text),
                rule=_rule(f, f.get(raw, "rule", dict, where), f"{where}.rule"),
                lead_days=lead_days,
                sectors=sectors,
                question=question,
                duration_days=duration,
            )
        )
    if not events:
        raise f.fail("events", "no hay ninguna fecha comercial")
    return events


def _load_sectors(path: Path) -> SectorMap:
    f = _File(path)

    def sector(value: Any, where: str) -> Sector:
        try:
            return Sector(value)
        except ValueError:
            options = ", ".join(s.value for s in Sector)
            raise f.fail(where, f"'{value}' no es un sector ({options})") from None

    labels = f.get(f.data, "sectors", dict, "raíz")
    if set(labels) != {s.value for s in Sector}:
        raise f.fail("sectors", "deben estar exactamente los sectores del dominio")
    types = {
        google_type: sector(value, f"google_types.{google_type}")
        for google_type, value in f.get(f.data, "google_types", dict, "raíz").items()
    }
    if not types:
        raise f.fail("google_types", "no hay ninguna categoría")
    suffixes = tuple(
        (suffix, sector(value, f"suffixes.{suffix}"))
        for suffix, value in f.get(f.data, "suffixes", dict, "raíz").items()
    )
    return SectorMap(types=types, suffixes=suffixes)


def load_calendar(data_dir: Path = DEFAULT_DATA_DIR) -> CalendarData:
    """Load and validate every calendar file. Call once at start-up."""
    if not data_dir.is_dir():
        raise CalendarDataError(f"no existe la carpeta de datos del calendario: {data_dir}")

    holidays: list[Holiday] = []
    years: list[int] = []
    unverified: list[tuple[int, str]] = []
    for path in sorted(data_dir.glob("*.json")):
        if match := _HOLIDAYS_NAME.match(path.name):
            year = int(match.group(1))
            found, pending = _load_holidays(path, year)
            holidays += found
            years.append(year)
            unverified += [(year, region) for region in pending]
        elif match := _LOCALS_NAME.match(path.name):
            holidays += _load_locals(path, int(match.group(1)), match.group(2))
        elif path.name not in (COMMERCIAL_FILE, SECTORS_FILE):
            raise CalendarDataError(f"{path.name}: nombre de archivo no reconocido en {data_dir}")
    if not years:
        raise CalendarDataError(f"no hay ningún archivo <año>-festivos.json en {data_dir}")

    return CalendarData(
        calendar=Calendar(
            holidays=tuple(sorted(holidays, key=lambda h: (h.date, h.scope, h.municipality or ""))),
            events=tuple(_load_events(data_dir / COMMERCIAL_FILE)),
        ),
        sector_map=_load_sectors(data_dir / SECTORS_FILE),
        holiday_years=tuple(years),
        unverified=tuple(unverified),
    )
