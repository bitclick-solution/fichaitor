"""Plans, their limits and the yearly price of a mix of active profiles (pure).

docs/multinegocio.md, sections 3.2, 11, 12 ("Precios en la respuesta") and 15.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.domain import plans
from app.domain.plans import ActiveLocation, Entitlement, Plan

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
DAY = timedelta(days=1)


def loc(location_id: str, plan: Plan, days_ago: int = 0) -> ActiveLocation:
    return ActiveLocation(location_id, plan, NOW - days_ago * DAY)


def beta(quantity: int = plans.BETA_PRO_QUANTITY) -> Entitlement:
    return Entitlement(Plan.PRO, quantity, starts_at=NOW - 30 * DAY, ends_at=None)


# --- prices and the discount ---------------------------------------------------------


def test_prices_are_in_cents_with_the_five_percent_of_the_owner():
    assert plans.VOLUME_DISCOUNT_PERCENT == 5
    assert plans.first_price(Plan.PRO) == 3500
    assert plans.additional_price(Plan.PRO) == 3325
    assert plans.first_price(Plan.PRO_WHATSAPP) == 5900
    assert plans.additional_price(Plan.PRO_WHATSAPP) == 5605
    assert plans.first_price(Plan.GRATIS) == plans.additional_price(Plan.GRATIS) == 0


@pytest.mark.parametrize(
    ("active", "total"),
    [
        ([], 0),
        ([loc("a", Plan.GRATIS)], 0),
        # Free does not count as the first one: the Pro pays in full.
        ([loc("a", Plan.GRATIS), loc("b", Plan.PRO)], 3500),
        ([loc("a", Plan.PRO), loc("b", Plan.PRO)], 3500 + 3325),
        ([loc("a", Plan.PRO), loc("b", Plan.PRO), loc("c", Plan.PRO)], 3500 + 2 * 3325),
        # The dearest plan pays in full, whatever its age.
        ([loc("a", Plan.PRO, 10), loc("b", Plan.PRO_WHATSAPP, 1)], 5900 + 3325),
        (
            [loc("g", Plan.GRATIS), loc("a", Plan.PRO, 5), loc("b", Plan.PRO_WHATSAPP, 1)],
            5900 + 3325,
        ),
        (
            [loc("a", Plan.PRO_WHATSAPP, 3), loc("b", Plan.PRO_WHATSAPP, 1), loc("c", Plan.PRO)],
            5900 + 5605 + 3325,
        ),
    ],
)
def test_yearly_price_of_a_mix(active, total):
    assert plans.yearly_price(active) == total


def test_the_full_price_goes_to_the_dearest_and_then_the_oldest():
    lines = plans.price_lines(
        [loc("new", Plan.PRO, 1), loc("old", Plan.PRO, 9), loc("free", Plan.GRATIS, 20)]
    )
    assert lines == {"old": 3500, "new": 3325, "free": 0}
    lines = plans.price_lines([loc("pro", Plan.PRO, 9), loc("wa", Plan.PRO_WHATSAPP, 1)])
    assert lines == {"wa": 5900, "pro": 3325}


def test_an_equal_age_is_settled_by_the_id_so_the_answer_never_changes():
    a, b = loc("loc_a", Plan.PRO), loc("loc_b", Plan.PRO)
    assert plans.price_lines([b, a]) == plans.price_lines([a, b]) == {"loc_a": 3500, "loc_b": 3325}


# --- the pricing block of the API ------------------------------------------------------


def test_pricing_block_of_an_account_with_its_free_profile():
    block = plans.pricing([loc("loc_3f2a", Plan.GRATIS)], NOW)
    assert block == {
        "currency": "EUR",
        "period": "year",
        "charging": False,
        "plans": {
            "pro": {"first": 3500, "additional": 3325},
            "pro_whatsapp": {"first": 5900, "additional": 5605},
        },
        "total": 0,
        # The free one does not count as first: the first paid one is at full price.
        "if_activate": {"pro": 3500, "pro_whatsapp": 5900},
        "if_deactivate": {"loc_3f2a": 0},
    }


def test_pricing_block_with_a_mix():
    active = [loc("g", Plan.GRATIS, 30), loc("p", Plan.PRO, 20), loc("w", Plan.PRO_WHATSAPP, 1)]
    block = plans.pricing(active, NOW)
    assert block["total"] == 5900 + 3325
    assert block["if_activate"] == {
        "pro": 5900 + 3325 + 3325,
        "pro_whatsapp": 5900 + 3325 + 5605,
    }
    assert block["if_deactivate"] == {"g": 5900 + 3325, "p": 5900, "w": 3500}


def test_pricing_block_with_nothing_active():
    block = plans.pricing([], NOW)
    assert block["total"] == 0
    assert block["if_activate"] == {"pro": 3500, "pro_whatsapp": 5900}
    assert block["if_deactivate"] == {}


def test_charging_starts_only_at_the_launch():
    launch = NOW + DAY
    assert plans.pricing([], NOW, launch_at=None)["charging"] is False
    assert plans.pricing([], NOW, launch_at=launch)["charging"] is False
    assert plans.pricing([], launch, launch_at=launch)["charging"] is True
    assert plans.LAUNCH_AT is None  # beta: nothing is charged yet


# --- limits --------------------------------------------------------------------------


def test_one_free_profile_per_account():
    assert plans.activation_allowed({}, [], Plan.GRATIS, NOW)
    assert not plans.activation_allowed({Plan.GRATIS: 1}, [], Plan.GRATIS, NOW)


def test_paid_plans_need_an_entitlement_in_force():
    assert not plans.activation_allowed({}, [], Plan.PRO, NOW)
    assert plans.activation_allowed({}, [beta()], Plan.PRO, NOW)
    assert plans.activation_allowed({Plan.PRO: 2}, [beta()], Plan.PRO, NOW)
    assert not plans.activation_allowed({Plan.PRO: 3}, [beta()], Plan.PRO, NOW)
    # The beta is Pro: it gives no WhatsApp.
    assert not plans.activation_allowed({}, [beta()], Plan.PRO_WHATSAPP, NOW)


def test_entitlements_add_up_and_only_count_while_in_force():
    manual = Entitlement(Plan.PRO, 2, starts_at=NOW - DAY, ends_at=None)
    assert plans.allowance([beta(), manual], NOW)[Plan.PRO] == 5
    ended = Entitlement(Plan.PRO, 2, starts_at=NOW - 9 * DAY, ends_at=NOW)
    future = Entitlement(Plan.PRO_WHATSAPP, 1, starts_at=NOW + DAY, ends_at=None)
    assert plans.allowance([ended, future], NOW) == {Plan.PRO: 0, Plan.PRO_WHATSAPP: 0}


def test_default_plan_once_charging_is_free_while_free_is_left_then_the_first_with_room():
    launched = NOW - DAY
    assert plans.default_plan({}, [beta()], NOW, launched) is Plan.GRATIS
    assert plans.default_plan({Plan.GRATIS: 1}, [beta()], NOW, launched) is Plan.PRO
    whatsapp = Entitlement(Plan.PRO_WHATSAPP, 1, starts_at=NOW - DAY, ends_at=None)
    assert plans.default_plan({Plan.GRATIS: 1, Plan.PRO: 3}, [beta(), whatsapp], NOW, launched) is (
        Plan.PRO_WHATSAPP
    )
    assert plans.default_plan({Plan.GRATIS: 1, Plan.PRO: 3}, [beta()], NOW, launched) is None


def test_default_plan_in_the_beta_is_pro_while_the_beta_has_room_then_free():
    # Paywall (owner, 2026-11-13): while nothing is charged (launch_at empty or ahead).
    for launch_at in (None, NOW + DAY):
        assert plans.default_plan({}, [beta()], NOW, launch_at) is Plan.PRO
        assert plans.default_plan({Plan.PRO: 2}, [beta()], NOW, launch_at) is Plan.PRO
        assert plans.default_plan({Plan.PRO: 3}, [beta()], NOW, launch_at) is Plan.GRATIS
        assert plans.default_plan({Plan.PRO: 3, Plan.GRATIS: 1}, [beta()], NOW, launch_at) is None
        assert plans.default_plan_order({}, [beta()], NOW, launch_at) == [Plan.PRO, Plan.GRATIS]
    # Without any beta room left (an expired entitlement), the free plan.
    expired = Entitlement(Plan.PRO, 3, starts_at=NOW - 2 * DAY, ends_at=NOW - DAY)
    assert plans.default_plan({}, [expired], NOW) is Plan.GRATIS


def test_usage_for_the_panel():
    usage = plans.usage({Plan.GRATIS: 1, Plan.PRO: 1}, [beta()], NOW)
    assert usage == {
        "active": 2,
        "free_available": False,
        "available": {"pro": 2, "pro_whatsapp": 0},
    }
    assert plans.usage({}, [], NOW) == {
        "active": 0,
        "free_available": True,
        "available": {"pro": 0, "pro_whatsapp": 0},
    }


def test_features_whatsapp_only_in_pro_whatsapp_and_key_dates_only_paid():
    assert plans.FEATURES[Plan.PRO_WHATSAPP].whatsapp
    assert not plans.FEATURES[Plan.PRO].whatsapp
    assert not plans.FEATURES[Plan.GRATIS].whatsapp
    assert plans.FEATURES[Plan.PRO].key_dates and not plans.FEATURES[Plan.GRATIS].key_dates


def test_parse_plan():
    assert plans.parse_plan("pro_whatsapp") is Plan.PRO_WHATSAPP
    assert plans.parse_plan("platino") is None
    assert plans.parse_plan(None) is None
