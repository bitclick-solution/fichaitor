"""Turn the yearly BOE resolution of labour holidays into our JSON data file.

The Dirección General de Trabajo publishes one resolution per year whose annex
is a table: one row per holiday, one column per region, each cell holding the
BOE code (*, ** or ***). This module parses the official XML of that
resolution (https://www.boe.es/diario_boe/xml.php?id=BOE-A-...) so nobody has
to copy the table by hand.

Usage (the XML is downloaded by a person, this tool does no network access):

    uv run python -m app.adapters.calendar.boe_import resolucion.xml \
        --out data/calendar/2028-festivos.json

Every region is written with "verified": false. A person flips it to true
after checking the result against a second source (see docs/calendario.md).
"""

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

# Column order of the annex table, as ISO 3166-2:ES codes. The header text is
# matched against REGION_HEADERS so a reordered table fails instead of
# silently assigning holidays to the wrong region.
REGION_CODES = (
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
)
REGION_NAMES = {
    "AN": "Andalucía",
    "AR": "Aragón",
    "AS": "Principado de Asturias",
    "IB": "Illes Balears",
    "CN": "Canarias",
    "CB": "Cantabria",
    "CM": "Castilla-La Mancha",
    "CL": "Castilla y León",
    "CT": "Cataluña",
    "EX": "Extremadura",
    "GA": "Galicia",
    "MD": "Comunidad de Madrid",
    "MC": "Región de Murcia",
    "NC": "Comunidad Foral de Navarra",
    "PV": "País Vasco",
    "RI": "La Rioja",
    "VC": "Comunitat Valenciana",
    "CE": "Ciudad de Ceuta",
    "ML": "Ciudad de Melilla",
}
REGION_HEADERS = {
    "AN": "andaluc",
    "AR": "arag",
    "AS": "asturias",
    "IB": "balears",
    "CN": "canarias",
    "CB": "cantabria",
    "CM": "mancha",
    "CL": "castilla y le",
    "CT": "catalu",
    "EX": "extremadura",
    "GA": "galicia",
    "MD": "madrid",
    "MC": "murcia",
    "NC": "navarra",
    "PV": "vasco",
    "RI": "rioja",
    "VC": "valenciana",
    "CE": "ceuta",
    "ML": "melilla",
}
MONTHS = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}
BOE_CODES = ("*", "**", "***")

_ROW = re.compile(r"^(\d{1,2})\s+(.+?)\.?$")
_YEAR = re.compile(r"para el año (\d{4})")


class BoeImportError(ValueError):
    """The XML does not look like the yearly resolution of labour holidays."""


def _text(node: ET.Element) -> str:
    return " ".join("".join(node.itertext()).split())


def parse_boe_xml(xml_text: str) -> dict:
    """Return the content of a `<year>-festivos.json` file, unverified."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise BoeImportError(f"el XML no se puede leer: {exc}") from exc

    reference = root.findtext("metadatos/identificador") or ""
    title = root.findtext("metadatos/titulo") or ""
    published = root.findtext("metadatos/fecha_publicacion") or ""
    year_match = _YEAR.search(title)
    if not reference or not year_match or len(published) != 8:
        raise BoeImportError("faltan el identificador, el título con el año o la fecha del BOE")
    year = int(year_match.group(1))

    table = root.find("texto/table")
    if table is None:
        raise BoeImportError("no se encuentra la tabla del anexo")

    header_rows = table.findall("thead/tr")
    if not header_rows:
        raise BoeImportError("la tabla del anexo no tiene cabecera")
    headers = [_text(th).lower() for th in header_rows[-1].findall("th")]
    if len(headers) != len(REGION_CODES):
        raise BoeImportError(
            f"la tabla tiene {len(headers)} columnas de territorio; se esperaban "
            f"{len(REGION_CODES)}"
        )
    for code, header in zip(REGION_CODES, headers, strict=True):
        if REGION_HEADERS[code] not in header:
            raise BoeImportError(
                f"la columna '{header}' no corresponde a {REGION_NAMES[code]}: "
                "el orden de la tabla ha cambiado"
            )

    holidays: list[dict] = []
    month: int | None = None
    for tr in table.findall("tbody/tr"):
        cells = [_text(td) for td in tr.findall("td")]
        if not cells or not cells[0]:
            continue
        first, marks = cells[0], cells[1:]
        if first.lower().rstrip(".") in MONTHS:
            month = MONTHS[first.lower().rstrip(".")]
            continue
        row = _ROW.match(first)
        if row is None or month is None or len(marks) != len(REGION_CODES):
            raise BoeImportError(f"fila del anexo que no se entiende: '{first}'")
        try:
            day = date(year, month, int(row.group(1)))
        except ValueError as exc:
            raise BoeImportError(f"fecha imposible en la fila '{first}'") from exc
        name = row.group(2).strip()
        marked = {
            code: mark for code, mark in zip(REGION_CODES, marks, strict=True) if mark.strip()
        }
        bad = sorted({mark for mark in marked.values() if mark not in BOE_CODES})
        if bad or not marked:
            raise BoeImportError(f"códigos no reconocidos en la fila '{first}': {bad}")
        everywhere = len(marked) == len(REGION_CODES) and len(set(marked.values())) == 1
        scopes = {"national": marks[0]} if everywhere else marked
        for scope, mark in scopes.items():
            holidays.append(
                {
                    "date": day.isoformat(),
                    "name": name,
                    "scope": scope,
                    "substitutable": mark == "**",
                    "boe_code": mark,
                }
            )

    if not holidays:
        raise BoeImportError("el anexo no contiene ninguna fiesta")

    notes = [
        _text(p)
        for p in root.findall("texto/p")
        if re.match(r"^\d+\. En la (Comunidad|Ciudad)", _text(p))
    ]
    order = {code: i for i, code in enumerate(("national", *REGION_CODES))}
    holidays.sort(key=lambda h: (h["date"], order[h["scope"]]))
    return {
        "year": year,
        "coverage": {"from": f"{year}-01-01", "to": f"{year}-12-31"},
        "source": {
            "title": title,
            "reference": reference,
            "published": f"{published[:4]}-{published[4:6]}-{published[6:]}",
            "url": f"https://www.boe.es/diario_boe/txt.php?id={reference}",
            "consulted": "",
            "cross_checked_with": [],
        },
        "regions": {code: {"name": REGION_NAMES[code], "verified": False} for code in REGION_CODES},
        "holidays": holidays,
        "subregional": [],
        "notes": notes,
    }


def restrict(data: dict, start: date, end: date) -> dict:
    """Keep only the holidays between two dates (for a year loaded halfway)."""
    kept = [h for h in data["holidays"] if start <= date.fromisoformat(h["date"]) <= end]
    return {
        **data,
        "coverage": {"from": start.isoformat(), "to": end.isoformat()},
        "holidays": kept,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("xml", type=Path, help="XML oficial de la resolución, ya descargado")
    parser.add_argument("--out", type=Path, required=True, help="archivo JSON de salida")
    parser.add_argument("--from", dest="start", type=date.fromisoformat, default=None)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, default=None)
    args = parser.parse_args(argv)

    data = parse_boe_xml(args.xml.read_text(encoding="utf-8"))
    if args.start or args.end:
        year = data["year"]
        data = restrict(data, args.start or date(year, 1, 1), args.end or date(year, 12, 31))
    args.out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{args.out}: {len(data['holidays'])} entradas de {data['year']}, sin verificar")
    return 0


if __name__ == "__main__":
    sys.exit(main())
