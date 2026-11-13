"""Caps on paid calls that hold for all clients together, or for one account, over a
window of time: the screenshots and the public diagnoses of a day, the drafts of an
account. No I/O.

They are counted in fixed windows (a day is a UTC calendar day): a counter per scope
and window, which a database can increment in one atomic statement. The limits per
client IP stay in process memory (app/application/rate_limit.py): they do not protect
the bill, the caps do.
"""

import re
from datetime import UTC, date, datetime, timedelta

from app.domain.conversation import DEFAULT_TIMEZONE, local_midnight, timezone_for

DAY_SECONDS = 86400

# What a counter is named after: a fixed word, and one of our ids (of an account, or of a
# profile: "loc_...") when it is per account or per profile. Never an IP address, a
# search or anything typed by a visitor. The same expression as usage_counter.scope.
SCOPE = re.compile(r"[a-z_]{1,40}(:[0-9a-z_-]{1,64})?")

SCREENSHOT_DAY = "screenshot_day"
DIAGNOSIS_DAY = "diagnosis_day"


def draft_scope(location_id: str) -> str:
    """The drafts of replies of one profile (docs/multinegocio.md, 5.4): each business
    of an account has its own cap."""
    return f"review_draft:{location_id}"


def ai_replies_scope(location_id: str) -> str:
    """The replies drafted by the model for one profile in a month: the cap of the free
    plan (app/domain/plans.py, `monthly_ai_replies`)."""
    return f"ai_replies:{location_id}"


# The month of the cap of the free plan is the calendar month in Spain: it starts again
# on the 1st at 00:00 in Madrid, as the owner reads it.
MONTH_ZONE = timezone_for(DEFAULT_TIMEZONE)  # without the system's zone data


def month_window(now: datetime) -> tuple[datetime, datetime]:
    """Start and end (UTC) of the calendar month, in Madrid, that `now` falls in."""
    local = now.astimezone(MONTH_ZONE)
    year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
    return (
        local_midnight(date(local.year, local.month, 1), MONTH_ZONE),
        local_midnight(date(year, month, 1), MONTH_ZONE),
    )


def window(now: datetime, seconds: int) -> tuple[datetime, datetime]:
    """Start and end of the fixed window of that length that `now` falls in."""
    seconds = max(1, int(seconds))
    start = datetime.fromtimestamp((int(now.timestamp()) // seconds) * seconds, UTC)
    return start, start + timedelta(seconds=seconds)
