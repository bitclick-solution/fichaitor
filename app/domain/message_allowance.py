"""The monthly bag of WhatsApp messages of an account. No I/O.

docs/fase0/precios.md and decision 3 of docs/multinegocio.md: 80 messages of ours a
month per Pro+ (`pro_whatsapp`) profile, added up in one bag per account, because the
chat is one. No wallet: once it is used up, nothing more goes out by WhatsApp until the
1st, and the notices go by the next linked channel (Telegram) or stay in the panel.

- Only our messages count. What the owner sends never does (Meta never charges it).
- A message costs the units of its Meta category: a service message (inside the window)
  or a utility template, 1; a marketing template, 4 (0.0585 / 0.0166 = 3.5, rounded up).
  Today there is no marketing template.
- The answer to an unknown number, and to a code that is wrong, is paid by Fichaitor:
  it is not of any account (guion, flujo 13).
- Counted in `usage_counter` (db/schema.sql), scope `whatsapp_month:<account>`, window of
  the calendar month in Madrid time. A counter only goes up, one by one (B11), and the
  conditional upsert never goes over the limit: a message is counted before it is sent,
  and one that then fails stays counted (it never goes back).
"""

from datetime import UTC, date, datetime, timedelta, tzinfo

from app.domain.channel_messages import OutboundMessage

MESSAGES_PER_PROFILE = 80
SCOPE_PREFIX = "whatsapp_month"
# Channels whose messages are paid per message, and so spend the bag.
METERED_CHANNELS = frozenset({"whatsapp"})
UNITS_BY_CATEGORY = {"service": 1, "utility": 1, "authentication": 1, "marketing": 4}


def scope(account_id: str) -> str:
    return f"{SCOPE_PREFIX}:{account_id}"


def monthly_limit(pro_whatsapp_profiles: int) -> int:
    return MESSAGES_PER_PROFILE * max(pro_whatsapp_profiles, 0)


def _first_of_month(day: date) -> date:
    return day.replace(day=1)


def month_window(now: datetime, zone: tzinfo) -> tuple[datetime, datetime]:
    """Start and end, in UTC, of the calendar month `now` falls in, in that zone."""
    first = _first_of_month(now.astimezone(zone).date())
    following = _first_of_month(first + timedelta(days=32))
    start = datetime(first.year, first.month, 1, tzinfo=zone).astimezone(UTC)
    end = datetime(following.year, following.month, 1, tzinfo=zone).astimezone(UTC)
    return start, end


def month_label(now: datetime, zone: tzinfo) -> str:
    """ "2026-11": the month of the bag, as GET /me names it."""
    return now.astimezone(zone).strftime("%Y-%m")


def units(message: OutboundMessage) -> int:
    """What sending that message may cost, at most: the template it may become counts if
    it is dearer than the session message."""
    template = message.template_fallback
    cost = UNITS_BY_CATEGORY["service"]
    if template is not None:
        cost = max(cost, UNITS_BY_CATEGORY.get(template.category, UNITS_BY_CATEGORY["marketing"]))
    return cost
