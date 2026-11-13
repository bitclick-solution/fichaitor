"""Plans of a profile, how many an account may have active and what they cost. No I/O.

docs/multinegocio.md, sections 3.2 and 11, the block "Precios en la respuesta" of
section 12, and the decisions of the owner in section 15:

- Free: at most one active profile per account (and one Google profile is free in one
  account only; that rule needs every account, so the database imposes it).
- Pro and Pro WhatsApp: as many as the entitlements in force add up. During the beta
  every account has one of 3 Pro.
- Price per profile and year: Pro 35 EUR, Pro WhatsApp 59 EUR. From the second paid
  profile of the account, 5 % off. Free does not count as the first one. The profile of
  the dearest plan pays in full and, if two tie, the oldest one.

The database imposes the limits (trigger `location_active_guard` in db/schema.sql);
these functions say the same thing beforehand, to answer the web and to price. Nothing
here charges: the discount is worked out when charging, never stored in a profile.
`app/domain/billing.py` (`seat_price`) applies `price_lines` to the paid seats in force
when one is charged (docs/admin-facturacion.md, section 3.3): keep the order here.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class Plan(StrEnum):
    GRATIS = "gratis"
    PRO = "pro"
    PRO_WHATSAPP = "pro_whatsapp"


PAID_PLANS = (Plan.PRO, Plan.PRO_WHATSAPP)  # also the order in which a default is chosen
FREE_PER_ACCOUNT = 1
BETA_PRO_QUANTITY = 3  # decision 4: until the launch, enlargeable by hand

CURRENCY = "EUR"
PERIOD = "year"
# Cents a year, of the first paid profile of an account.
YEARLY_PRICE_CENTS: Mapping[Plan, int] = {Plan.GRATIS: 0, Plan.PRO: 3500, Plan.PRO_WHATSAPP: 5900}
VOLUME_DISCOUNT_PERCENT = 5  # decision 1, from the second paid profile
# None while there is no charging (beta): prices are shown for reference only.
LAUNCH_AT: datetime | None = None


@dataclass(frozen=True)
class Features:
    whatsapp: bool  # decision 3: WhatsApp only tells of pro_whatsapp profiles
    key_dates: bool
    monthly_ai_replies: int | None  # None = without a cap


# docs/fase0/precios.md. Enforced per profile (`location.plan`) at the moment of each
# action (decision of the owner, 2026-11-13):
# - Free: the panel, the summary, the diagnosis, the sentiment analysis and the notices of
#   reviews (panel and Telegram); replies written by the owner without a cap; 25 replies
#   drafted by the model per month and profile (`monthly_ai_replies`: every draft asked to
#   the model, in the panel and in the bot); no key dates (no proposals, no answer, no
#   dismiss).
# - Pro and Pro WhatsApp: without those limits.
FREE_MONTHLY_AI_REPLIES = 25
FEATURES: Mapping[Plan, Features] = {
    Plan.GRATIS: Features(
        whatsapp=False, key_dates=False, monthly_ai_replies=FREE_MONTHLY_AI_REPLIES
    ),
    Plan.PRO: Features(whatsapp=False, key_dates=True, monthly_ai_replies=None),
    Plan.PRO_WHATSAPP: Features(whatsapp=True, key_dates=True, monthly_ai_replies=None),
}

# What a plan may lack, as the API names it in a 402 (`feature`).
FEATURE_AI_REPLIES = "ai_replies"
FEATURE_KEY_DATES = "key_dates"


class PlanRequired(Exception):
    """The plan of that profile does not include `feature`, or its monthly cap of it is
    used up. The API answers 402 with `code: plan_required` and this `feature`."""

    def __init__(self, feature: str, limit: int | None = None) -> None:
        super().__init__(feature)
        self.feature = feature
        self.limit = limit


def features_of(plan: Plan) -> Features:
    return FEATURES[plan]


def check_key_dates(plan: Plan | None) -> None:
    """Raises PlanRequired unless that plan includes the key dates. An inactive profile
    (None) is not served at all: whoever asks answers that apart."""
    if plan is not None and not FEATURES[plan].key_dates:
        raise PlanRequired(FEATURE_KEY_DATES)


def parse_plan(value: object) -> Plan | None:
    """The plan a value names, or None. Never raises: the value may come from outside."""
    try:
        return Plan(value) if isinstance(value, str) else None
    except ValueError:
        return None


# --- entitlements and limits ---------------------------------------------------------


@dataclass(frozen=True)
class Entitlement:
    """A row of plan_entitlement: `quantity` profiles of `plan` from `starts_at` until
    `ends_at` (None = no end)."""

    plan: Plan
    quantity: int
    starts_at: datetime
    ends_at: datetime | None = None

    def in_force(self, now: datetime) -> bool:
        return self.starts_at <= now and (self.ends_at is None or now < self.ends_at)


def allowance(entitlements: Iterable[Entitlement], now: datetime) -> dict[Plan, int]:
    """How many profiles of each paid plan the account may have active now."""
    total = dict.fromkeys(PAID_PLANS, 0)
    for entitlement in entitlements:
        if entitlement.plan in total and entitlement.in_force(now):
            total[entitlement.plan] += entitlement.quantity
    return total


def _limit(plan: Plan, entitlements: Iterable[Entitlement], now: datetime) -> int:
    return FREE_PER_ACCOUNT if plan is Plan.GRATIS else allowance(entitlements, now)[plan]


def activation_allowed(
    active_by_plan: Mapping[Plan, int],
    entitlements: Iterable[Entitlement],
    plan: Plan,
    now: datetime,
) -> bool:
    """Whether one more profile fits in `plan`. To change the plan of a profile that is
    already active, leave that profile out of `active_by_plan`."""
    return active_by_plan.get(plan, 0) + 1 <= _limit(plan, entitlements, now)


def default_plan(
    active_by_plan: Mapping[Plan, int], entitlements: Iterable[Entitlement], now: datetime
) -> Plan | None:
    """The plan of an activation that names none: free while it is left, then the
    first paid plan with room. None if nothing fits."""
    granted = list(entitlements)
    for plan in (Plan.GRATIS, *PAID_PLANS):
        if activation_allowed(active_by_plan, granted, plan, now):
            return plan
    return None


def usage(
    active_by_plan: Mapping[Plan, int], entitlements: Iterable[Entitlement], now: datetime
) -> dict[str, Any]:
    """The `plans` object of GET /me: active profiles, whether the free one is left and
    the room left in each paid plan."""
    limits = allowance(entitlements, now)
    return {
        "active": sum(active_by_plan.values()),
        "free_available": active_by_plan.get(Plan.GRATIS, 0) < FREE_PER_ACCOUNT,
        "available": {
            plan.value: max(limits[plan] - active_by_plan.get(plan, 0), 0) for plan in PAID_PLANS
        },
    }


# --- price ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActiveLocation:
    location_id: str
    plan: Plan
    active_since: datetime


def first_price(plan: Plan) -> int:
    return YEARLY_PRICE_CENTS[plan]


def additional_price(plan: Plan) -> int:
    """The price from the second paid profile on, rounded to the cent."""
    cents = YEARLY_PRICE_CENTS[plan] * (100 - VOLUME_DISCOUNT_PERCENT)
    return (cents + 50) // 100


def price_lines(active: Iterable[ActiveLocation]) -> dict[str, int]:
    """What each active profile pays a year, in cents. The full price goes to the
    profile of the dearest plan and, if two tie, to the oldest (then the id, so that
    the answer never depends on the order of the list)."""
    lines: dict[str, int] = {}
    paid = sorted(
        (a for a in active if a.plan in PAID_PLANS),
        key=lambda a: (-YEARLY_PRICE_CENTS[a.plan], a.active_since, a.location_id),
    )
    for index, location in enumerate(paid):
        price = first_price if index == 0 else additional_price
        lines[location.location_id] = price(location.plan)
    for location in active:
        lines.setdefault(location.location_id, 0)
    return lines


def yearly_price(active: Iterable[ActiveLocation]) -> int:
    """What the account pays a year for its active profiles, in cents."""
    return sum(price_lines(active).values())


def charging(now: datetime, launch_at: datetime | None = LAUNCH_AT) -> bool:
    return launch_at is not None and now >= launch_at


def pricing(
    active: Sequence[ActiveLocation], now: datetime, launch_at: datetime | None = LAUNCH_AT
) -> dict[str, Any]:
    """The `pricing` block of GET /me and of the activation routes, in cents:
    `total` with the active profiles; `if_activate` and `if_deactivate`, the total after
    each possible change (a new profile is the newest one)."""
    current = list(active)
    return {
        "currency": CURRENCY,
        "period": PERIOD,
        "charging": charging(now, launch_at),
        "plans": {
            plan.value: {"first": first_price(plan), "additional": additional_price(plan)}
            for plan in PAID_PLANS
        },
        "total": yearly_price(current),
        "if_activate": {
            plan.value: yearly_price([*current, ActiveLocation("\x00new", plan, now)])
            for plan in PAID_PLANS
        },
        "if_deactivate": {
            location.location_id: yearly_price(
                [a for a in current if a.location_id != location.location_id]
            )
            for location in current
        },
    }
