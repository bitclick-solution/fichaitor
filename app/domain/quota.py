"""Caps on paid calls that hold for all clients together, or for one account, over a
window of time: the screenshots and the public diagnoses of a day, the drafts of an
account. No I/O.

They are counted in fixed windows (a day is a UTC calendar day): a counter per scope
and window, which a database can increment in one atomic statement. The limits per
client IP stay in process memory (app/application/rate_limit.py): they do not protect
the bill, the caps do.
"""

import re
from datetime import UTC, datetime, timedelta

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


def window(now: datetime, seconds: int) -> tuple[datetime, datetime]:
    """Start and end of the fixed window of that length that `now` falls in."""
    seconds = max(1, int(seconds))
    start = datetime.fromtimestamp((int(now.timestamp()) // seconds) * seconds, UTC)
    return start, start + timedelta(seconds=seconds)
