"""The jobs of the messaging channels (docs/canales.md, docs/worker.md): their kinds,
their payloads and when the planner enqueues them. No I/O.

All of them are run by the conversation process (`python -m app.worker.conversation`),
never by the worker that writes to Google. Two are enqueued from outside it:
`channel_inbound` by the API's webhook, and `propose_review` and
`channel_action_result` by the worker.
"""

import hashlib
import re
from datetime import datetime, time, timedelta, tzinfo

CHANNEL_INBOUND = "channel_inbound"  # {"channel": "telegram", "update": {...}}
CONVERSATION_TICK = "conversation_tick"  # {}
PROPOSE_REVIEW = "propose_review"  # {"review_id": "rev_..."}
CHANNEL_ACTION_RESULT = "channel_action_result"  # {"action_id", "ok", "error"}
PLAN_KEY_DATES = "plan_key_dates"  # {}: once a day, the key dates of every profile

KINDS = (CHANNEL_INBOUND, CONVERSATION_TICK, PROPOSE_REVIEW, CHANNEL_ACTION_RESULT, PLAN_KEY_DATES)

INBOUND_MAX_ATTEMPTS = 1  # an event is recorded as seen first: a retry would do nothing
TICK_MAX_ATTEMPTS = 1
PROPOSE_MAX_ATTEMPTS = 3
RESULT_MAX_ATTEMPTS = 3
PLAN_MAX_ATTEMPTS = 3

REVIEW_ID = re.compile(r"rev_[0-9a-f]{8,32}")

# Only reviews this recent are proposed by the channel; older ones stay in the panel.
PROPOSE_REVIEWS_NEWER_THAN = timedelta(days=7)

# What the executor reports, as the conversation understands it.
RESULT_TARGET_GONE = "target_gone"
RESULT_RECONNECT = "reconnect"
RESULT_FAILED = "failed"
RESULT_TOO_LATE = "too_late"  # special hours: the day passed before they could be sent
RESULT_NO_REGULAR_HOURS = "no_regular_hours"  # "horario normal" without regular hours
RESULT_BILLING_BLOCKED = "billing_blocked"  # the account is blocked for non-payment


def inbound_key(channel: str, update_id: int) -> str:
    """Telegram delivers an update again if the webhook failed: one job per update."""
    return f"{channel}:{update_id}"


def propose_key(review_id: str) -> str:
    return f"{PROPOSE_REVIEW}:{review_id}"


def result_key(action_id: str) -> str:
    return f"{CHANNEL_ACTION_RESULT}:{action_id}"


def result_error(error_code: str | None) -> str | None:
    """The error code of an action (app/domain/actions.py) as the conversation reads it."""
    if error_code is None:
        return None
    if error_code == "review_gone":
        return RESULT_TARGET_GONE
    if error_code == "reconnect":
        return RESULT_RECONNECT
    if error_code == "date_passed":
        return RESULT_TOO_LATE
    if error_code == "no_regular_hours":
        return RESULT_NO_REGULAR_HOURS
    if error_code == "billing_blocked":
        return RESULT_BILLING_BLOCKED
    return RESULT_FAILED


# --- the planner -------------------------------------------------------------------

TICK_EVERY = timedelta(minutes=5)
# Profiles with a linked address are read every 30 minutes by day and every 3 hours by
# night (local time). 30 minutes is the proposal of docs/canales.md (step 6), still to
# be checked against the quota of the reviews API (docs/fase0/matriz-api.md).
SYNC_DAY_EVERY = timedelta(minutes=30)
SYNC_NIGHT_EVERY = timedelta(hours=3)
SYNC_DAY_START = time(8, 30)
SYNC_DAY_END = time(21, 0)


def _slot(now: datetime, every: timedelta) -> int:
    seconds = int(every.total_seconds())
    return int(now.timestamp()) // seconds


def tick_key(now: datetime) -> str:
    """One tick per five minute slot, whatever number of processes try to enqueue it."""
    return f"{CONVERSATION_TICK}:{_slot(now, TICK_EVERY)}"


def sync_slot(now: datetime, zone: tzinfo) -> str:
    """The slot of the periodic sync `now` falls in: of 30 minutes by day, of 3 hours by
    night. Enqueued once per profile and slot."""
    local = now.astimezone(zone).time()
    if SYNC_DAY_START <= local < SYNC_DAY_END:
        return f"d{_slot(now, SYNC_DAY_EVERY)}"
    return f"n{_slot(now, SYNC_NIGHT_EVERY)}"


# The key dates are planned once a day, at this local time: the questions then wait
# for nothing but the weekly cap (the quiet hours end at 9:00).
PLAN_KEY_DATES_AT = time(9, 5)


def plan_key_dates_key(day: object) -> str:
    """One planning per local day, whatever number of processes try to enqueue it."""
    return f"{PLAN_KEY_DATES}:{day}"


def plan_key_dates_run_at(now: datetime, zone: tzinfo) -> datetime:
    """When today's planning runs: at PLAN_KEY_DATES_AT local time, or now if later."""
    local = now.astimezone(zone)
    at = datetime.combine(local.date(), PLAN_KEY_DATES_AT, tzinfo=zone)
    return max(at, now)


def sync_key(google_sub: str, location: str, slot: str) -> str:
    """`sync:<google_sub>:<location>:<slot>`; hashed if it would not fit in the column."""
    key = f"sync:{google_sub}:{location}:{slot}"
    if len(key) <= 200:
        return key
    digest = hashlib.sha256(f"{google_sub}\n{location}".encode()).hexdigest()
    return f"sync:{digest}:{slot}"
