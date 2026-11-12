"""Rules of the conversation: pure functions that receive the clock. No I/O.

What the owner's answer means (by button and by simple text), one active proposal
per conversation and who goes first, how often the agent may write, when it must
keep quiet, and when a proposal is reminded and expires. The delays are the ones of
`docs/04-ux-conversacional.md`, section 3.

No language model takes part: buttons and exact words. `FreeTextRouter` in
`app/application/conversation_service.py` is where a model based router plugs in.
"""

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import StrEnum

from app.domain.proposals import (
    Proposal,
    ProposalEvent,
    ProposalState,
    ProposalType,
    jumps_queue,
)

# --- who is talking ----------------------------------------------------------------


CHANNELS = ("telegram", "whatsapp", "email")
_ADDRESS = {
    "telegram": re.compile(r"-?\d{1,20}"),  # the chat id
    "whatsapp": re.compile(r"\+[1-9]\d{7,14}"),  # the phone, in E.164
    "email": re.compile(r"[^@\s]{1,64}@[^@\s]{1,189}"),
}


class InvalidAddress(ValueError):
    pass


def normalize_address(channel: str, address: str) -> str:
    """The one way an address is written, so that the same chat is always the same
    row. Raises InvalidAddress."""
    if channel not in CHANNELS:
        raise InvalidAddress("canal desconocido")
    value = address.strip()
    if channel == "whatsapp":
        value = "+" + re.sub(r"[\s\-()+]", "", value)
    elif channel == "email":
        value = value.lower()
    if not _ADDRESS[channel].fullmatch(value):
        raise InvalidAddress("dirección no válida")
    return value


@dataclass(frozen=True)
class ChannelIdentity:
    """An address (a Telegram chat, a WhatsApp phone, an email) linked with a code
    to an account. Only a linked address is attended and can act. An account may
    have several (the owner and the manager), each linked with its own code; an
    address belongs to one account at a time."""

    id: str
    account_id: str
    channel: str
    address: str
    verified_at: datetime
    last_inbound_at: datetime | None = None
    unlinked_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.unlinked_at is None


_CHANNEL_NAMES = {"telegram": "Telegram", "whatsapp": "WhatsApp", "email": "Correo"}
MASK = "••••"


def masked_address(channel: str, address: str) -> str:
    """How the panel shows a linked address: enough to recognise it, not to use it.
    "Telegram de ••••7342", "WhatsApp de ••••2233", "Correo de m••••@bar.es"."""
    name = _CHANNEL_NAMES.get(channel, channel)
    if channel == "email" and "@" in address:
        local, domain = address.split("@", 1)
        return f"{name} de {local[:1]}{MASK}@{domain}"
    digits = re.sub(r"\D", "", address)
    return f"{name} de {MASK}{digits[-4:]}"


class LinkOutcome(StrEnum):
    NEW = "new"  # the address was free
    SAME = "same"  # already linked to that account: nothing to do, the code is kept
    MOVED = "moved"  # it was another account's: unlinked from it, with a notice


def plan_link(current: ChannelIdentity | None, account_id: str) -> LinkOutcome:
    """What linking an address to an account does, given whom it is linked to now."""
    if current is None or not current.active:
        return LinkOutcome.NEW
    return LinkOutcome.SAME if current.account_id == account_id else LinkOutcome.MOVED


def may_act(identity: ChannelIdentity | None, proposal_account_id: str) -> bool:
    """Whether that sender can move that proposal. Knowing the id of a proposal is
    never enough: the address must be linked, now, to the account that owns it."""
    return identity is not None and identity.active and identity.account_id == proposal_account_id


@dataclass(frozen=True)
class Conversation:
    """The thread between the agent and an account, whichever linked addresses take
    part in it: one open proposal at a time."""

    id: str
    account_id: str
    active_proposal_id: str | None = None
    # Messages in a row that were not understood, with no proposal open.
    unknown_streak: int = 0


@dataclass(frozen=True)
class AccountInfo:
    business_name: str = ""
    timezone: str = "Europe/Madrid"


# --- time zones ----------------------------------------------------------------------


class EuRuleTime(tzinfo):
    """A zone that follows the EU summer time rule (from the last Sunday of March to
    the last Sunday of October, at 01:00 UTC), with a fixed winter offset.

    Quiet hours are local time, and the standard library has no zone data on Windows
    without an extra package. Spain only needs this rule: peninsula and Balearics
    (+1) and the Canaries (+0).
    """

    def __init__(self, winter_offset_hours: int, name: str) -> None:
        self._winter = timedelta(hours=winter_offset_hours)
        self._name = name

    @staticmethod
    def _last_sunday(year: int, month: int) -> datetime:
        last = date(year, month, 31)
        last -= timedelta(days=(last.weekday() + 1) % 7)
        return datetime(year, month, last.day, 1, 0)  # 01:00 UTC

    def _is_summer_utc(self, naive_utc: datetime) -> bool:
        start = self._last_sunday(naive_utc.year, 3)
        end = self._last_sunday(naive_utc.year, 10)
        return start <= naive_utc < end

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return self._winter + self.dst(dt)

    def dst(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return timedelta(0)
        hour = timedelta(hours=1)
        wall = dt.replace(tzinfo=None)
        # Wall clock at which summer time starts, and at which it ends (the hour
        # before `end` happens twice: `fold` tells the second one, already winter).
        start = self._last_sunday(wall.year, 3) + self._winter
        end = self._last_sunday(wall.year, 10) + self._winter + hour
        if start <= wall < end - hour:
            return hour
        if end - hour <= wall < end:
            return timedelta(0) if dt.fold else hour
        return timedelta(0)

    def fromutc(self, dt: datetime) -> datetime:
        naive = dt.replace(tzinfo=None)
        summer = self._is_summer_utc(naive)
        local = naive + self._winter + (timedelta(hours=1) if summer else timedelta(0))
        end = self._last_sunday(naive.year, 10) + self._winter + timedelta(hours=1)
        repeated = not summer and end - timedelta(hours=1) <= local < end
        return local.replace(tzinfo=self, fold=1 if repeated else 0)

    def tzname(self, dt: datetime | None) -> str:
        return self._name


_EU_ZONES = {"Europe/Madrid": 1, "Africa/Ceuta": 1, "Atlantic/Canary": 0}
DEFAULT_TIMEZONE = "Europe/Madrid"


def timezone_for(name: str) -> tzinfo:
    """The zone of an account. Spanish zones never depend on the system's zone data;
    any other name does, and an unknown one falls back to the peninsula."""
    if name in _EU_ZONES:
        return EuRuleTime(_EU_ZONES[name], name)
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return EuRuleTime(_EU_ZONES[DEFAULT_TIMEZONE], DEFAULT_TIMEZONE)


def local_midnight(day: date, zone: tzinfo) -> datetime:
    """00:00 of that day in the zone, as an aware UTC datetime."""
    return datetime.combine(day, time(0), tzinfo=zone).astimezone(UTC)


# --- quiet hours -------------------------------------------------------------------


@dataclass(frozen=True)
class QuietHours:
    """Nothing is sent on the agent's initiative from `start` to `end` (local time),
    nor during the busy hours of the business."""

    start: time = time(21, 0)
    end: time = time(9, 0)
    busy: tuple[tuple[time, time], ...] = ()


def _within(moment: time, start: time, end: time) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end  # crosses midnight


def is_quiet(now: datetime, zone: tzinfo, quiet: QuietHours) -> bool:
    local = now.astimezone(zone).time()
    if _within(local, quiet.start, quiet.end):
        return True
    return any(_within(local, start, end) for start, end in quiet.busy)


def next_allowed(now: datetime, zone: tzinfo, quiet: QuietHours) -> datetime:
    """The first moment from `now` on at which the agent may write."""
    moment = now
    for _ in range(4 * 24 * 2):  # two days in steps of 15 minutes: always enough
        if not is_quiet(moment, zone, quiet):
            return moment
        local = moment.astimezone(zone)
        minutes = 15 - local.minute % 15
        moment = (moment + timedelta(minutes=minutes)).replace(second=0, microsecond=0)
    return moment


def may_send_proactively(now: datetime, zone: tzinfo, quiet: QuietHours) -> bool:
    """A proposal, a reminder or a proposal taken up again goes out only outside the
    quiet hours. Every type: a review of 1 or 2 stars jumps the queue, not the quiet
    hours ("Reseña: al momento (en horario)"). Answers to the owner are not held."""
    return not is_quiet(now, zone, quiet)


# --- how often ---------------------------------------------------------------------

DEFAULT_WEEKLY_CAP = 2  # proactive messages a week, reviews apart
CAP_WINDOW = timedelta(days=7)  # a rolling week, not Monday to Sunday


def under_weekly_cap(sent_in_window: int, cap: int = DEFAULT_WEEKLY_CAP) -> bool:
    return sent_in_window < cap


# --- reminder and expiry -----------------------------------------------------------

REVIEW_REMINDER_AFTER = timedelta(hours=24)
REVIEW_EXPIRES_AFTER = timedelta(days=7)

KIND_HOLIDAY = "holiday"
KIND_COMMERCIAL = "commercial"
KIND_SEASON = "season"
KEY_DATE_KINDS = (KIND_HOLIDAY, KIND_COMMERCIAL, KIND_SEASON)

# kind: (days before the date for the reminder, days before the date it expires)
_KEY_DATE_DELAYS = {KIND_HOLIDAY: (1, 0), KIND_COMMERCIAL: (7, 2), KIND_SEASON: (10, 0)}


def review_deadlines(sent_at: datetime) -> tuple[datetime, datetime]:
    """(reminder, expiry) of a review proposal, counted from when it was sent."""
    return sent_at + REVIEW_REMINDER_AFTER, sent_at + REVIEW_EXPIRES_AFTER


def key_date_deadlines(kind: str, day: date, zone: tzinfo) -> tuple[datetime, datetime]:
    """(reminder, expiry) of a key date. Both are the start of a local day: the
    reminder then waits for the quiet hours to end, like any other message."""
    remind_days, expire_days = _KEY_DATE_DELAYS[kind]
    return (
        local_midnight(day - timedelta(days=remind_days), zone),
        local_midnight(day - timedelta(days=expire_days), zone),
    )


def due_event(proposal: Proposal, now: datetime) -> ProposalEvent | None:
    """What time alone does to a proposal: cancel it before it was ever sent, expire
    it, or remind it. One reminder per proposal, and only if the owner has not
    answered at all."""
    if proposal.is_terminal or proposal.state is ProposalState.EXECUTING:
        return None
    expired = proposal.expires_at is not None and now >= proposal.expires_at
    if proposal.state is ProposalState.SCHEDULED:
        return ProposalEvent.CANCEL if expired else None
    if expired:
        return ProposalEvent.EXPIRE
    if (
        proposal.state is ProposalState.SENT
        and proposal.reminder_sent_at is None
        and proposal.remind_at is not None
        and now >= proposal.remind_at
    ):
        return ProposalEvent.REMIND
    return None


# --- one active proposal and the queue ---------------------------------------------

_PARKED_RANK = 90  # taken up again before anything new, except a review of 1 or 2 stars


def should_interrupt(active: Proposal | None, incoming: Proposal) -> bool:
    """A review of 1 or 2 stars takes the place of the open proposal, unless that one
    is another such review."""
    if active is None:
        return False
    return jumps_queue(incoming.type, incoming.priority) and not jumps_queue(
        active.type, active.priority
    )


def is_parked(proposal: Proposal) -> bool:
    return proposal.is_open and proposal.parked_at is not None


def is_waiting(proposal: Proposal, now: datetime) -> bool:
    if proposal.slots.get("in_panel"):
        return False  # the owner took it to the panel: the chat does not bring it back
    if is_parked(proposal):
        return True
    return proposal.state is ProposalState.SCHEDULED and proposal.scheduled_for <= now


def queue_order(proposals: Iterable[Proposal], now: datetime) -> list[Proposal]:
    """Who goes next: reviews of 1 or 2 stars, then what was interrupted (the latest
    first), then the rest by priority and age."""

    def rank(p: Proposal) -> tuple[int, float, str]:
        if is_parked(p) and not jumps_queue(p.type, p.priority):
            assert p.parked_at is not None
            return (-_PARKED_RANK, -p.parked_at.timestamp(), p.id)
        return (-p.priority, p.scheduled_for.timestamp(), p.id)

    return sorted((p for p in proposals if is_waiting(p, now)), key=rank)


# --- what a short text means ---------------------------------------------------------


class Reply(StrEnum):
    AFFIRMATIVE = "affirmative"
    NEGATIVE = "negative"
    COURTESY = "courtesy"  # a greeting or thanks: no answer
    OTHER = "other"


_AFFIRMATIVE = frozenset(
    {"si", "vale", "ok", "okey", "dale", "publica", "publicala", "envialo", "enviala"}
)
_NEGATIVE = frozenset({"no", "dejalo", "paso", "cancela", "cancelar"})
_COURTESY = frozenset(
    {"hola", "buenas", "buenos dias", "buenas tardes", "buenas noches", "gracias", "muchas gracias"}
)


def normalize(text: str) -> str:
    """Lower case, no accents, no punctuation, single spaces."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    plain = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return " ".join(re.sub(r"[^\w\s:.,\-–]", " ", plain).split()).strip(" .,-–")


def classify_reply(text: str) -> Reply:
    """Only the whole message counts: "no" is a no, "no sé si abriré" is not."""
    words = normalize(text)
    if words in _AFFIRMATIVE:
        return Reply.AFFIRMATIVE
    if words in _NEGATIVE:
        return Reply.NEGATIVE
    if words in _COURTESY:
        return Reply.COURTESY
    return Reply.OTHER


# --- hours written by the owner ------------------------------------------------------

MAX_CLARIFICATIONS = 2  # the third time, closed options
MAX_PERIODS = 3


class HoursKind(StrEnum):
    PERIODS = "periods"
    CLOSED = "closed"
    REGULAR = "regular"


@dataclass(frozen=True)
class HoursAnswer:
    kind: HoursKind
    periods: tuple[tuple[str, str], ...] = ()  # ("10:00", "14:00"), in order


_CLOSED_WORDS = frozenset({"cierro", "cerrado", "cerramos", "no abro", "no abrimos"})
_REGULAR_WORDS = frozenset(
    {"horario normal", "abro normal", "abrimos normal", "normal", "como siempre"}
)
_TIME = r"(\d{1,2})(?:[:.](\d{2})|\s?h)?"
_RANGE = re.compile(rf"(?<![\d:.]){_TIME}\s*(?:a|hasta|-|–)\s*(?:las?\s+)?{_TIME}(?![\d:.])")
# Words that may go around the hours without changing what they mean.
_FILLER = frozenset(
    {
        "abro",
        "abrimos",
        "abre",
        "de",
        "desde",
        "y",
        "las",
        "la",
        "el",
        "horario",
        "es",
        "hago",
        "mejor",
    }
)


def _clock(hour: str, minute: str | None) -> int | None:
    h, m = int(hour), int(minute or 0)
    if h > 24 or m > 59 or (h == 24 and m):
        return None
    return h * 60 + m


def format_clock(minutes: int) -> str:
    return f"{minutes // 60}:{minutes % 60:02d}"


def parse_hours(text: str) -> HoursAnswer | None:
    """The hours of one day, or None if the text is not exactly that.

    Understands "de 10 a 14", "10:00-14:00", "de 9 a 13 y de 17 a 20", "cierro" and
    "horario normal". Never guesses: an opening that is not before its closing ("de
    9 a 2"), overlapping periods or any word it does not know make the whole answer
    None, and the agent asks.
    """
    words = normalize(text)
    if words in _CLOSED_WORDS:
        return HoursAnswer(HoursKind.CLOSED)
    if words in _REGULAR_WORDS:
        return HoursAnswer(HoursKind.REGULAR)
    matches = list(_RANGE.finditer(words))
    if not matches or len(matches) > MAX_PERIODS:
        return None
    rest = _RANGE.sub(" ", words)
    if any(word.strip(".,") not in _FILLER for word in rest.split() if word.strip(".,")):
        return None
    periods: list[tuple[int, int]] = []
    for match in matches:
        start = _clock(match.group(1), match.group(2))
        end = _clock(match.group(3), match.group(4))
        if start is None or end is None or start >= end:
            return None
        if periods and start <= periods[-1][1]:
            return None  # out of order or overlapping
        periods.append((start, end))
    return HoursAnswer(
        HoursKind.PERIODS, tuple((format_clock(s), format_clock(e)) for s, e in periods)
    )


def hours_text(periods: Sequence[tuple[str, str]]) -> str:
    """`fmt.hours_range`: "de 10:00 a 14:00 y de 17:00 a 20:00"."""
    return " y ".join(f"de {start} a {end}" for start, end in periods)


def clarification_key(asked_before: int) -> str:
    """Which text asks for the hours again: two questions, then closed options."""
    if asked_before >= MAX_CLARIFICATIONS:
        return "hours.options"
    return "hours.clarify_1" if asked_before == 0 else "hours.clarify_2"


# --- what a proposal type is allowed to end in ---------------------------------------

ACTION_REPLY_REVIEW = "reply_review"
ACTION_SET_SPECIAL_HOURS = "set_special_hours"

ALLOWED_ACTIONS = {
    ProposalType.REVIEW_REPLY: ACTION_REPLY_REVIEW,
    ProposalType.KEY_DATE: ACTION_SET_SPECIAL_HOURS,
}
