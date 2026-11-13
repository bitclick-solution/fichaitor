"""Key dates of a profile, as Fichaitor asks about them: which days to ask about, where
the business is, and what the panel may do with a proposal. No I/O.

The calendar (`app/domain/calendar.py`) knows every holiday, commercial date and season.
Here only the days whose opening hours may change are asked about, one question per
profile and day (docs/calendario.md, "Preguntar por el horario"):

- every official holiday of the business (national, of its region and, when the data
  has them, of its town);
- Nochebuena and Nochevieja (the first day of the seasons `christmas` and
  `new_years_eve`): not holidays, and the days on which most businesses change hours.

Commercial dates (Black Friday, rebajas) ask for an offer, not for hours: they wait for
the posts (docs/fase1/guion-conversacional.md, flow 4).
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from app.domain.calendar import (
    HOLIDAY_LEAD_DAYS,
    Business,
    Calendar,
    KeyDate,
    KeyDateKind,
    Sector,
    key_dates,
)
from app.domain.proposals import ProposalEvent, ProposalState

E, S = ProposalEvent, ProposalState

# Season id -> the name of its first day, the one asked about.
HOURS_EVENTS = {"christmas": "Nochebuena", "new_years_eve": "Nochevieja"}
DEFAULT_LEAD_DAYS = HOLIDAY_LEAD_DAYS  # docs/04-ux-conversacional.md: "Festivo: 5 días antes"
MAX_LEAD_DAYS = 60

# --- where the business is -----------------------------------------------------------

# First two digits of a Spanish postal code: the province, and so the region
# (ISO 3166-2:ES, the codes of the holiday files). From the province codes of the INE.
_PROVINCE_REGION = {
    "01": "PV", "02": "CM", "03": "VC", "04": "AN", "05": "CL", "06": "EX", "07": "IB",
    "08": "CT", "09": "CL", "10": "EX", "11": "AN", "12": "VC", "13": "CM", "14": "AN",
    "15": "GA", "16": "CM", "17": "CT", "18": "AN", "19": "CM", "20": "PV", "21": "AN",
    "22": "AR", "23": "AN", "24": "CL", "25": "CT", "26": "RI", "27": "GA", "28": "MD",
    "29": "AN", "30": "MC", "31": "NC", "32": "GA", "33": "AS", "34": "CL", "35": "CN",
    "36": "GA", "37": "CL", "38": "CN", "39": "CB", "40": "CL", "41": "AN", "42": "CL",
    "43": "CT", "44": "AR", "45": "CM", "46": "VC", "47": "CL", "48": "PV", "49": "CL",
    "50": "AR", "51": "CE", "52": "ML",
}  # fmt: skip
_POSTAL_CODE = re.compile(r"(?<!\d)(\d{5})(?!\d)")


def region_for_postal_code(postal_code: str) -> str:
    """'18010' -> 'AN'. Empty when it is not a Spanish postal code."""
    code = (postal_code or "").strip()
    if not re.fullmatch(r"\d{5}", code):
        return ""
    return _PROVINCE_REGION.get(code[:2], "")


def postal_code_in(address: str) -> str:
    """The postal code of an address as the panel keeps it ('Calle Elvira 1, 18010
    Granada'): the last group of exactly five digits. Empty if there is none."""
    found = _POSTAL_CODE.findall(address or "")
    return found[-1] if found else ""


def region_of(region: str, address: str) -> str:
    """The region of a profile: the one the sync stored, or the one its address says.
    Empty when neither is known; then only national holidays are asked about."""
    return region or region_for_postal_code(postal_code_in(address))


# --- what to ask -------------------------------------------------------------------------


@dataclass(frozen=True)
class HoursQuestion:
    """One day to ask the owner about: are you open, and with what hours."""

    day: date
    kind: str  # "holiday" or "season" (the timing rules of docs/canales.md)
    name: str
    local: bool = False


def source_ref(location_id: str, day: date) -> str:
    """The key of a proposal: one per profile and day, whatever made the day special."""
    return f"{location_id}:{day.isoformat()}"


def hours_questions(
    region: str,
    today: date,
    calendar: Calendar,
    *,
    municipality: str | None = None,
    lead_days: int = DEFAULT_LEAD_DAYS,
    skip: Iterable[date] = (),
) -> tuple[HoursQuestion, ...]:
    """The days to ask about today, earliest first.

    A holiday is asked about from `lead_days` before it until the day before; Nochebuena
    and Nochevieja from the lead days of their season. A day that already has special
    hours in Google (`skip`) is not asked about. One question per day: a holiday wins.
    """
    business = Business(region=region, sector=Sector.OTHER, municipality_code=municipality)
    skipped = set(skip)
    by_day: dict[date, HoursQuestion] = {}
    for item in key_dates(business, calendar, (today.year, today.year + 1)):
        question = _question(item, lead_days)
        if question is None or question.day in skipped:
            continue
        lead = lead_days if item.kind is KeyDateKind.HOLIDAY else item.lead_days
        if not (today < question.day <= today + timedelta(days=lead)):
            continue
        current = by_day.get(question.day)
        if current is None or (current.kind != "holiday" and question.kind == "holiday"):
            by_day[question.day] = question
    return tuple(by_day[day] for day in sorted(by_day))


def _question(item: KeyDate, lead_days: int) -> HoursQuestion | None:
    if item.kind is KeyDateKind.HOLIDAY:
        return HoursQuestion(item.date, "holiday", item.name, item.local)
    name = HOURS_EVENTS.get(item.event_id)
    if name is None:
        return None
    first = replace(item, end=item.date)
    return HoursQuestion(first.date, "season", name)


# --- the panel -----------------------------------------------------------------------------

# The panel walks the same diagram as the chat: an answer from the panel is a reply, the
# data are complete and the owner confirms, all at once. A proposal the chat never sent
# (no linked chat, or waiting for the quiet hours) is "sent" to the panel first; one that
# expired while its day has not passed is reopened, as the chat does (flow 9).
_CONFIRM_PATHS: dict[ProposalState, tuple[ProposalEvent, ...]] = {
    S.SCHEDULED: (E.SEND, E.OWNER_REPLIED, E.DATA_COMPLETE, E.CONFIRM),
    S.SENT: (E.OWNER_REPLIED, E.DATA_COMPLETE, E.CONFIRM),
    S.REMINDED: (E.OWNER_REPLIED, E.DATA_COMPLETE, E.CONFIRM),
    S.COLLECTING: (E.DATA_COMPLETE, E.CONFIRM),
    S.TO_CONFIRM: (E.CONFIRM,),
    S.EXPIRED: (E.REOPEN, E.SEND, E.OWNER_REPLIED, E.DATA_COMPLETE, E.CONFIRM),
}
_DISMISS_PATHS: dict[ProposalState, tuple[ProposalEvent, ...]] = {
    S.SCHEDULED: (E.CANCEL,),
    S.SENT: (E.OWNER_REPLIED, E.DECLINE),
    S.REMINDED: (E.OWNER_REPLIED, E.DECLINE),
    S.COLLECTING: (E.DECLINE,),
    S.TO_CONFIRM: (E.DECLINE,),
}
ANSWERABLE_STATES = frozenset(_CONFIRM_PATHS)
OUTCOME_DISMISSED = "dismissed"  # proposal.outcome_reason of a dismissal from the panel

# How the panel names the state of a key date.
STATUS_PENDING = "pendiente"
STATUS_RUNNING = "en_curso"
STATUS_APPLIED = "aplicada"
STATUS_FAILED = "fallida"
STATUS_DISMISSED = "descartada"
STATUS_EXPIRED = "caducada"
_STATUS = {
    S.SCHEDULED: STATUS_PENDING,
    S.SENT: STATUS_PENDING,
    S.REMINDED: STATUS_PENDING,
    S.COLLECTING: STATUS_PENDING,
    S.TO_CONFIRM: STATUS_PENDING,
    S.EXECUTING: STATUS_RUNNING,
    S.APPLIED: STATUS_APPLIED,
    S.FAILED: STATUS_FAILED,
    S.DISMISSED: STATUS_DISMISSED,
    S.CANCELLED: STATUS_EXPIRED,
    S.EXPIRED: STATUS_EXPIRED,
}


class PanelRefused(ValueError):
    """The panel cannot do that with this proposal. `code` says why."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


REFUSED_CLOSED = "closed"  # answered, dismissed or running already
REFUSED_PAST = "past"  # its day has passed
REFUSED_PROFILE = "profile"  # its profile is no longer an active one


def panel_status(state: ProposalState) -> str:
    return _STATUS[state]


def answerable(state: ProposalState, day: date, today: date) -> bool:
    """The panel can still answer it: not resolved, and its day has not passed. On the
    day itself it can (a proposal expires at 00:00 of its day, but "hoy cierro" still
    makes sense)."""
    return state in ANSWERABLE_STATES and today <= day


def panel_confirm_path(state: ProposalState, day: date, today: date) -> tuple[ProposalEvent, ...]:
    if state not in _CONFIRM_PATHS:
        raise PanelRefused(REFUSED_CLOSED)
    if today > day:
        raise PanelRefused(REFUSED_PAST)
    return _CONFIRM_PATHS[state]


def panel_dismiss_path(state: ProposalState) -> tuple[ProposalEvent, ...]:
    if state not in _DISMISS_PATHS:
        raise PanelRefused(REFUSED_CLOSED)
    return _DISMISS_PATHS[state]


def panel_event_id(now: datetime, token: str) -> str:
    """`confirmed_event_id` of a confirmation made in the panel (no channel event)."""
    return f"panel:{int(now.timestamp())}:{token}"
