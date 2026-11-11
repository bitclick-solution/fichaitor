import json
import shutil
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.adapters.calendar.boe_import import (
    REGION_CODES,
    REGION_NAMES,
    BoeImportError,
    parse_boe_xml,
    restrict,
)
from app.adapters.calendar.loader import (
    DEFAULT_DATA_DIR,
    CalendarDataError,
    load_calendar,
)
from app.adapters.calendar.loader import REGION_CODES as LOADER_REGION_CODES
from app.domain.calendar import (
    NATIONAL,
    Business,
    Sector,
    due_proposals,
    easter_sunday,
    sector_for,
)

HOLIDAY_FILES = sorted(DEFAULT_DATA_DIR.glob("*-festivos.json"))


@pytest.fixture(scope="module")
def real():
    return load_calendar()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A private copy of the real data, to break one file per test."""
    target = tmp_path / "calendar"
    shutil.copytree(DEFAULT_DATA_DIR, target)
    return target


def edit(path: Path, change) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def texts(business: Business, today: date, real) -> list[str]:
    return [p.text for p in due_proposals(business, today, real.calendar)]


# --- the data files themselves ---------------------------------------------


def test_there_are_holiday_files_for_2026_and_2027():
    assert [p.name for p in HOLIDAY_FILES] == ["2026-festivos.json", "2027-festivos.json"]


@pytest.mark.parametrize("path", HOLIDAY_FILES, ids=lambda p: p.name)
def test_every_date_is_a_valid_date_of_its_year(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    assert str(data["year"]) == path.name[:4]
    for entry in [*data["holidays"], *data["subregional"]]:
        assert date.fromisoformat(entry["date"]).year == data["year"], entry


@pytest.mark.parametrize("path", HOLIDAY_FILES, ids=lambda p: p.name)
def test_no_duplicates_per_scope(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    counts = Counter((h["date"], h["scope"]) for h in data["holidays"])
    assert [key for key, n in counts.items() if n > 1] == []
    national = {h["date"] for h in data["holidays"] if h["scope"] == NATIONAL}
    regional = {h["date"] for h in data["holidays"] if h["scope"] != NATIONAL}
    assert national & regional == set()


@pytest.mark.parametrize("path", HOLIDAY_FILES, ids=lambda p: p.name)
def test_holiday_files_name_their_official_source(path: Path):
    source = json.loads(path.read_text(encoding="utf-8"))["source"]
    assert source["reference"].startswith("BOE-A-")
    assert source["url"].startswith("https://www.boe.es/")
    assert date.fromisoformat(source["consulted"])
    assert source["cross_checked_with"]


def test_2027_comes_from_the_resolution_of_8_october_2026():
    data = json.loads((DEFAULT_DATA_DIR / "2027-festivos.json").read_text(encoding="utf-8"))
    assert data["source"]["reference"] == "BOE-A-2026-21254"
    assert data["source"]["published"] == "2026-10-13"
    assert set(data["regions"]) == set(REGION_CODES) == LOADER_REGION_CODES
    assert len(data["regions"]) == 19


def test_2027_every_region_has_its_twelve_holidays(real):
    # Twelve national and regional holidays each; the two local ones are not
    # here. Canarias lists eleven because the twelfth is a different day on
    # each island (note 1 of the resolution, kept under "subregional").
    for region in sorted(REGION_CODES):
        days = {
            h.date
            for h in real.calendar.holidays
            if h.date.year == 2027 and h.scope in (NATIONAL, region)
        }
        assert len(days) == (11 if region == "CN" else 12), region


def test_2027_national_holidays_are_the_nine_common_ones(real):
    national = [
        h.date for h in real.calendar.holidays if h.scope == NATIONAL and h.date.year == 2027
    ]
    assert national == [
        date(2027, 1, 1),
        date(2027, 1, 6),
        date(2027, 3, 26),
        date(2027, 5, 1),
        date(2027, 10, 12),
        date(2027, 11, 1),
        date(2027, 12, 6),
        date(2027, 12, 8),
        date(2027, 12, 25),
    ]


def test_2027_good_friday_in_the_data_matches_the_easter_computation(real):
    by_name = {h.name: h.date for h in real.calendar.holidays if h.date.year == 2027}
    easter = easter_sunday(2027)
    assert by_name["Viernes Santo"] == easter - timedelta(days=2)
    assert by_name["Jueves Santo"] == easter - timedelta(days=3)
    assert by_name["Lunes de Pascua"] == easter + timedelta(days=1)


def test_2027_substitutable_flag_follows_the_boe_code():
    data = json.loads((DEFAULT_DATA_DIR / "2027-festivos.json").read_text(encoding="utf-8"))
    for h in data["holidays"]:
        assert h["substitutable"] == (h["boe_code"] == "**"), h
    flags = {(h["name"], h["scope"]): h["substitutable"] for h in data["holidays"]}
    assert flags[("Año Nuevo", "national")] is False
    assert flags[("Epifanía del Señor", "national")] is True
    assert flags[("San José", "MD")] is True
    assert flags[("Día de Andalucía", "AN")] is False


def test_2026_only_covers_what_is_left_of_the_year(real):
    days = sorted({h.date for h in real.calendar.holidays if h.date.year == 2026})
    assert days == [date(2026, 12, 7), date(2026, 12, 8), date(2026, 12, 25), date(2026, 12, 26)]


def test_everything_loaded_is_verified(real):
    assert real.unverified == ()
    assert real.holiday_years == (2026, 2027)
    assert all(h.verified for h in real.calendar.holidays)


def test_commercial_dates_cover_the_required_ones(real):
    by_id = {e.id: e for e in real.calendar.events}
    assert set(by_id) == {
        "black_friday",
        "cyber_monday",
        "winter_sales",
        "summer_sales",
        "valentines_day",
        "fathers_day",
        "mothers_day",
        "holy_week",
        "christmas",
        "new_years_eve",
        "back_to_school",
        "summer_hours",
    }
    assert by_id["black_friday"].rule.resolve(2024) == date(2024, 11, 29)
    assert by_id["black_friday"].rule.resolve(2027) == date(2027, 11, 26)
    assert by_id["cyber_monday"].rule.resolve(2027) == date(2027, 11, 29)
    assert by_id["mothers_day"].rule.resolve(2027) == date(2027, 5, 2)
    assert by_id["holy_week"].rule.resolve(2027) == date(2027, 3, 21)
    assert by_id["fathers_day"].rule.resolve(2027) == date(2027, 3, 19)
    assert by_id["valentines_day"].rule.resolve(2027) == date(2027, 2, 14)


def test_commercial_lead_days_follow_the_type(real):
    for event in real.calendar.events:
        assert event.lead_days == (14 if event.kind == "commercial" else 21), event.id


def test_commercial_rules_resolve_for_the_next_decade(real):
    for event in real.calendar.events:
        for year in range(2026, 2037):
            assert event.rule.resolve(year).year in (year, year + 1)


def test_sector_map_classifies_common_google_categories(real):
    cases = {
        "restaurant": Sector.HOSPITALITY,
        "pizza_restaurant": Sector.HOSPITALITY,
        "cafe": Sector.HOSPITALITY,
        "clothing_store": Sector.RETAIL,
        "gift_shop": Sector.RETAIL,
        "hair_salon": Sector.BEAUTY_HEALTH,
        "barber_shop": Sector.BEAUTY_HEALTH,
        "dentist": Sector.BEAUTY_HEALTH,
        "lawyer": Sector.PROFESSIONAL_SERVICES,
        "accounting": Sector.PROFESSIONAL_SERVICES,
        "car_repair": Sector.OTHER,
        "planetarium": Sector.OTHER,
    }
    for google_type, sector in cases.items():
        assert sector_for(google_type, (), real.sector_map) is sector, google_type


# --- the rules on the real data --------------------------------------------


def test_real_data_tuesday_8_december_2026(real):
    catalan = Business(region="CT", sector=Sector.HOSPITALITY)
    assert texts(catalan, date(2026, 12, 2), real) == []
    assert "El martes 8 de diciembre es festivo. ¿Abrís?" in texts(catalan, date(2026, 12, 3), real)


def test_real_data_monday_moved_from_sunday_follows_the_resolution(real):
    # 6 December 2026 is a Sunday: Andalucía rests on Monday 7, Cataluña does not.
    andalusian = Business(region="AN", sector=Sector.PROFESSIONAL_SERVICES)
    assert texts(andalusian, date(2026, 12, 2), real) == [
        "El lunes 7 de diciembre y el martes 8 de diciembre son festivos. ¿Abrís?"
    ]
    # 15 August 2027 is a Sunday: Andalucía moves it to Monday 16, Cataluña
    # does not, and Extremadura replaces it with 11 October.
    assert texts(andalusian, date(2027, 8, 11), real) == [
        "El lunes 16 de agosto es festivo. ¿Abrís?"
    ]
    catalan = Business(region="CT", sector=Sector.PROFESSIONAL_SERVICES)
    assert texts(catalan, date(2027, 8, 11), real) == []
    extremaduran = Business(region="EX", sector=Sector.PROFESSIONAL_SERVICES)
    assert texts(extremaduran, date(2027, 8, 11), real) == []
    assert texts(extremaduran, date(2027, 10, 6), real) == [
        "El lunes 11 de octubre y el martes 12 de octubre son festivos. ¿Abrís?"
    ]
    assert not [h for h in real.calendar.holidays if h.date == date(2027, 8, 15)]


def test_real_data_black_friday_by_sector(real):
    day = date(2027, 11, 12)
    shop = Business(region="AN", sector=sector_for("clothing_store", (), real.sector_map))
    notary = Business(region="AN", sector=sector_for("notary_public", (), real.sector_map))
    restaurant = Business(region="AN", sector=sector_for("restaurant", (), real.sector_map))
    (proposal,) = due_proposals(shop, day, real.calendar)
    assert "Black Friday (viernes 26 de noviembre)" in proposal.text
    assert "Cyber Monday (lunes 29 de noviembre)" in proposal.text
    assert texts(notary, day, real) == []
    assert texts(restaurant, day, real) == []


def test_real_data_fathers_day_and_san_jose_are_one_proposal_in_madrid(real):
    shop = Business(region="MD", sector=Sector.RETAIL)
    proposals = due_proposals(shop, date(2027, 3, 5), real.calendar)
    assert len(proposals) == 1
    assert {"fathers_day:2027-03-19", "holiday:2027-03-19"} <= set(proposals[0].keys)


# --- malformed files -------------------------------------------------------


def test_missing_folder_fails_clearly(tmp_path: Path):
    with pytest.raises(CalendarDataError, match="no existe la carpeta"):
        load_calendar(tmp_path / "nope")


def test_file_that_is_not_json_fails_naming_the_file(data_dir: Path):
    (data_dir / "2027-festivos.json").write_text("{ esto no es json", encoding="utf-8")
    with pytest.raises(CalendarDataError, match=r"2027-festivos\.json: no es JSON válido"):
        load_calendar(data_dir)


def test_impossible_date_fails_naming_the_entry(data_dir: Path):
    edit(data_dir / "2027-festivos.json", lambda d: d["holidays"][3].update(date="2027-02-30"))
    with pytest.raises(CalendarDataError) as error:
        load_calendar(data_dir)
    assert "2027-festivos.json" in str(error.value)
    assert "holidays[3].date" in str(error.value)
    assert "'2027-02-30' no es una fecha válida" in str(error.value)


def test_date_of_another_year_fails(data_dir: Path):
    edit(data_dir / "2027-festivos.json", lambda d: d["holidays"][0].update(date="2028-01-01"))
    with pytest.raises(CalendarDataError, match="fuera de la cobertura"):
        load_calendar(data_dir)


def test_duplicated_holiday_in_a_scope_fails(data_dir: Path):
    edit(data_dir / "2027-festivos.json", lambda d: d["holidays"].append(dict(d["holidays"][0])))
    with pytest.raises(CalendarDataError, match="fiesta duplicada: 2027-01-01 en national"):
        load_calendar(data_dir)


def test_unknown_scope_fails(data_dir: Path):
    edit(data_dir / "2027-festivos.json", lambda d: d["holidays"][0].update(scope="XX"))
    with pytest.raises(CalendarDataError, match="'XX' no es 'national' ni un código"):
        load_calendar(data_dir)


def test_missing_field_fails(data_dir: Path):
    edit(data_dir / "2027-festivos.json", lambda d: d["holidays"][0].pop("substitutable"))
    with pytest.raises(CalendarDataError, match=r"holidays\[0\]: falta el campo 'substitutable'"):
        load_calendar(data_dir)


def test_wrong_type_fails(data_dir: Path):
    edit(data_dir / "2027-festivos.json", lambda d: d["holidays"][0].update(substitutable="no"))
    with pytest.raises(CalendarDataError, match="se esperaba true o false"):
        load_calendar(data_dir)


def test_missing_region_fails(data_dir: Path):
    edit(data_dir / "2027-festivos.json", lambda d: d["regions"].pop("ML"))
    with pytest.raises(CalendarDataError, match="19 territorios"):
        load_calendar(data_dir)


def test_file_without_source_fails(data_dir: Path):
    edit(data_dir / "2027-festivos.json", lambda d: d.pop("source"))
    with pytest.raises(CalendarDataError, match="falta el campo 'source'"):
        load_calendar(data_dir)


def test_year_that_does_not_match_the_file_name_fails(data_dir: Path):
    edit(data_dir / "2027-festivos.json", lambda d: d.update(year=2028))
    with pytest.raises(CalendarDataError, match="no coincide con el nombre del archivo"):
        load_calendar(data_dir)


def test_unrecognised_file_name_fails(data_dir: Path):
    (data_dir / "festivos-2028.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CalendarDataError, match="nombre de archivo no reconocido"):
        load_calendar(data_dir)


def test_missing_commercial_file_fails(data_dir: Path):
    (data_dir / "fechas-comerciales.json").unlink()
    with pytest.raises(CalendarDataError, match=r"fechas-comerciales\.json: no existe"):
        load_calendar(data_dir)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda e: e["rule"].update(kind="lunar"), "no es una regla conocida"),
        (lambda e: e["rule"].update(kind="fixed", month=2, day=30), "no da una fecha válida"),
        (lambda e: e["rule"].update(kind="fixed", month=13, day=1), "entre 1 y 12"),
        (lambda e: e["rule"].update(kind="nth_weekday", month=5, weekday=6, n=5), "entre 1 y 4"),
        (lambda e: e.update(sectors=["retail", "notarias"]), "sector desconocido"),
        (lambda e: e.update(sectors=[]), "al menos un sector"),
        (lambda e: e.update(type="holiday"), "debe ser 'commercial' o 'season'"),
        (lambda e: e.update(lead_days=0), "entre 1 y 60"),
        (lambda e: e.update(lead_days=True), "un número entero"),
        (lambda e: e.update(question="Se acerca algo."), "marcador {date}"),
        (lambda e: e.update(question="Se acerca {evento} ({date})."), "solo admite"),
        (lambda e: e.update(id="Black Friday"), "no es un identificador válido"),
        (lambda e: e.pop("question"), "falta el campo 'question'"),
    ],
)
def test_malformed_commercial_event_fails(data_dir: Path, change, message: str):
    edit(data_dir / "fechas-comerciales.json", lambda d: change(d["events"][0]))
    with pytest.raises(CalendarDataError) as error:
        load_calendar(data_dir)
    assert "fechas-comerciales.json" in str(error.value)
    assert message in str(error.value)


def test_duplicated_commercial_id_fails(data_dir: Path):
    edit(data_dir / "fechas-comerciales.json", lambda d: d["events"].append(dict(d["events"][0])))
    with pytest.raises(CalendarDataError, match="identificador duplicado"):
        load_calendar(data_dir)


def test_unknown_sector_in_the_sector_map_fails(data_dir: Path):
    edit(data_dir / "sectores.json", lambda d: d["google_types"].update(restaurant="comida"))
    with pytest.raises(CalendarDataError, match=r"sectores\.json: google_types\.restaurant"):
        load_calendar(data_dir)


def test_unverified_region_is_reported_and_its_holidays_are_not_proposed(data_dir: Path):
    edit(data_dir / "2027-festivos.json", lambda d: d["regions"]["AN"].update(verified=False))
    loaded = load_calendar(data_dir)
    assert loaded.unverified == ((2027, "AN"),)
    andalusian = Business(region="AN", sector=Sector.PROFESSIONAL_SERVICES)
    # Día de Andalucía (regional, unverified) is held back...
    assert due_proposals(andalusian, date(2027, 2, 24), loaded.calendar) == ()
    # ...and the national holidays keep working.
    assert len(due_proposals(andalusian, date(2027, 10, 7), loaded.calendar)) == 1


# --- preloaded local holidays (format only; no real file is shipped) -------


def _locals_file(holidays: list[dict]) -> dict:
    return {
        "year": 2027,
        "region": "AN",
        "source": {
            "title": "Resolución de fiestas locales de Andalucía para 2027 (ejemplo de prueba)",
            "url": "https://www.juntadeandalucia.es/",
            "consulted": "2026-11-11",
        },
        "holidays": holidays,
    }


def test_preloaded_local_holidays_file_is_loaded_per_municipality(data_dir: Path):
    content = _locals_file(
        [{"date": "2027-06-03", "name": "Fiesta local de prueba", "municipality": "18087"}]
    )
    (data_dir / "2027-locales-AN.json").write_text(json.dumps(content), encoding="utf-8")
    loaded = load_calendar(data_dir)
    in_town = Business(region="AN", sector=Sector.PROFESSIONAL_SERVICES, municipality_code="18087")
    elsewhere = Business(
        region="AN", sector=Sector.PROFESSIONAL_SERVICES, municipality_code="41091"
    )
    assert [p.text for p in due_proposals(in_town, date(2027, 5, 29), loaded.calendar)] == [
        "El jueves 3 de junio es fiesta local. ¿Abrís?"
    ]
    assert due_proposals(elsewhere, date(2027, 5, 29), loaded.calendar) == ()


def test_preloaded_local_holidays_need_an_ine_code(data_dir: Path):
    content = _locals_file([{"date": "2027-06-03", "name": "Fiesta", "municipality": "Granada"}])
    (data_dir / "2027-locales-AN.json").write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(CalendarDataError, match="código INE de 5 cifras"):
        load_calendar(data_dir)


# --- importing the BOE resolution ------------------------------------------


def _boe_xml(rows: list[tuple[str, list[str]]], headers: list[str] | None = None) -> str:
    headers = headers or [REGION_NAMES[code] for code in REGION_CODES]
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = ""
    for first, marks in rows:
        cells = "".join(f"<td>{m}</td>" for m in marks)
        body += f"<tr><td>{first}</td>{cells}</tr>"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<documento>
  <metadatos>
    <identificador>BOE-A-2099-1</identificador>
    <titulo>Resolución con la relación de fiestas laborales para el año 2028.</titulo>
    <fecha_publicacion>20271015</fecha_publicacion>
  </metadatos>
  <texto>
    <table>
      <thead><tr><th>Fecha de las fiestas</th><th>Comunidades</th></tr><tr>{head}</tr></thead>
      <tbody>{body}</tbody>
    </table>
    <p>1. En la Comunidad Autónoma de Canarias, el decreto dispone otras fiestas por isla.</p>
  </texto>
</documento>"""


def test_boe_import_reads_the_annex_table():
    everywhere, blank = ["*"] * 19, [" "] * 19
    only_andalusia = ["***"] + [" "] * 18
    madrid_and_valencia = [" "] * 19
    madrid_and_valencia[REGION_CODES.index("MD")] = "**"
    madrid_and_valencia[REGION_CODES.index("VC")] = "**"
    data = parse_boe_xml(
        _boe_xml(
            [
                ("Enero", blank),
                ("1 Año Nuevo.", everywhere),
                ("Febrero", blank),
                ("28 Día de Andalucía.", only_andalusia),
                ("Marzo", blank),
                ("20 San José.", madrid_and_valencia),
            ]
        )
    )
    assert data["year"] == 2028
    assert data["source"]["reference"] == "BOE-A-2099-1"
    assert data["source"]["published"] == "2027-10-15"
    assert [(h["date"], h["name"], h["scope"], h["substitutable"]) for h in data["holidays"]] == [
        ("2028-01-01", "Año Nuevo", "national", False),
        ("2028-02-28", "Día de Andalucía", "AN", False),
        ("2028-03-20", "San José", "MD", True),
        ("2028-03-20", "San José", "VC", True),
    ]
    assert len(data["notes"]) == 1
    # nothing is trusted until a person has checked it against a second source
    assert not any(region["verified"] for region in data["regions"].values())
    partial = restrict(data, date(2028, 2, 1), date(2028, 12, 31))
    assert [h["date"] for h in partial["holidays"]] == ["2028-02-28", "2028-03-20", "2028-03-20"]
    assert partial["coverage"] == {"from": "2028-02-01", "to": "2028-12-31"}


def test_boe_import_fails_if_the_columns_change_order():
    headers = [REGION_NAMES[code] for code in REGION_CODES]
    headers[0], headers[1] = headers[1], headers[0]
    with pytest.raises(BoeImportError, match="el orden de la tabla ha cambiado"):
        parse_boe_xml(_boe_xml([("Enero", [" "] * 19), ("1 Año Nuevo.", ["*"] * 19)], headers))


def test_boe_import_fails_on_an_unknown_code_or_an_impossible_date():
    with pytest.raises(BoeImportError, match="códigos no reconocidos"):
        parse_boe_xml(_boe_xml([("Enero", [" "] * 19), ("1 Año Nuevo.", ["x"] * 19)]))
    with pytest.raises(BoeImportError, match="fecha imposible"):
        parse_boe_xml(_boe_xml([("Febrero", [" "] * 19), ("30 Algo.", ["*"] * 19)]))
    with pytest.raises(BoeImportError, match="no se encuentra la tabla"):
        parse_boe_xml(
            "<documento><metadatos><identificador>BOE-A-1</identificador>"
            "<titulo>fiestas laborales para el año 2028</titulo>"
            "<fecha_publicacion>20271015</fecha_publicacion></metadatos>"
            "<texto/></documento>"
        )
