"""The billing domain (pure): seat prices, tax treatment, tax ids, invoice lines, seat
state, blocks, the end of the beta and the DMARC check of a leave by email.

docs/admin-facturacion.md, step 1 of section 7, with the decisions of the owner of
section 9.0 (card only, the block has no time limit, a seat without a profile is not
renewed, Spain and the EU from the start).
"""

import itertools
import random
from datetime import UTC, date, datetime, timedelta

import pytest

from app.domain import billing, plans
from app.domain.billing import (
    BetaProfile,
    BillingEvent,
    BillingInputError,
    BlockAction,
    BlockReason,
    EnforceAction,
    PriceKey,
    RenewalAction,
    Seat,
    SeatStatus,
    StripeSnapshot,
    TaxIdKind,
    TaxTreatment,
    Territory,
)
from app.domain.plans import ActiveLocation, Plan

NOW = datetime(2027, 3, 14, 9, 0, tzinfo=UTC)
DAY = timedelta(days=1)


def seat(seat_id: str, plan: Plan, days_ago: int = 0, **kwargs) -> Seat:
    return Seat(seat_id, plan, NOW - days_ago * DAY, **kwargs)


# --- price keys ----------------------------------------------------------------------


def test_the_four_price_keys_are_the_lookup_keys_of_stripe_and_the_prices_of_plans():
    assert {key.value for key in PriceKey} == {
        "pro_first_yearly",
        "pro_additional_yearly",
        "whatsapp_first_yearly",
        "whatsapp_additional_yearly",
    }
    assert billing.price_cents(PriceKey.PRO_FIRST) == plans.first_price(Plan.PRO) == 3500
    assert billing.price_cents(PriceKey.PRO_ADDITIONAL) == plans.additional_price(Plan.PRO) == 3325
    assert billing.price_cents(PriceKey.WHATSAPP_FIRST) == 5900
    assert billing.price_cents(PriceKey.WHATSAPP_ADDITIONAL) == 5605
    assert billing.price_plan(PriceKey.WHATSAPP_ADDITIONAL) is Plan.PRO_WHATSAPP
    assert billing.is_additional(PriceKey.PRO_ADDITIONAL)
    assert not billing.is_additional(PriceKey.PRO_FIRST)


# --- the 5 %: seats that come and go -------------------------------------------------


def test_the_first_paid_seat_of_the_account_pays_in_full():
    assert billing.seat_price(Plan.PRO, "seat_new", []) is PriceKey.PRO_FIRST
    assert billing.seat_price(Plan.PRO_WHATSAPP, "seat_new", []) is PriceKey.WHATSAPP_FIRST


def test_a_second_pro_pays_the_additional_price():
    in_force = [seat("seat_a", Plan.PRO, 100)]
    # A seat being bought is not in force yet: it counts as the newest one.
    assert billing.seat_price(Plan.PRO, "seat_new", in_force) is PriceKey.PRO_ADDITIONAL


def test_a_whatsapp_bought_after_a_pro_pays_in_full_and_the_pro_drops_at_its_renewal():
    pro = seat("seat_pro", Plan.PRO, 200)
    assert billing.seat_price(Plan.PRO_WHATSAPP, "seat_wa", [pro]) is PriceKey.WHATSAPP_FIRST
    wa = seat("seat_wa", Plan.PRO_WHATSAPP, 0)
    # At its next renewal the Pro is no longer the dearest one.
    assert billing.seat_price(Plan.PRO, "seat_pro", [pro, wa]) is PriceKey.PRO_ADDITIONAL
    assert billing.seat_price(Plan.PRO_WHATSAPP, "seat_wa", [pro, wa]) is PriceKey.WHATSAPP_FIRST


def test_when_the_full_price_seat_leaves_the_next_one_in_the_order_pays_in_full():
    a, b, c = seat("seat_a", Plan.PRO, 300), seat("seat_b", Plan.PRO, 200), seat("seat_c", Plan.PRO)
    assert billing.seat_price(Plan.PRO, "seat_b", [a, b, c]) is PriceKey.PRO_ADDITIONAL
    # seat_a is cancelled: at its renewal seat_b is the oldest one left.
    assert billing.seat_price(Plan.PRO, "seat_b", [b, c]) is PriceKey.PRO_FIRST
    assert billing.seat_price(Plan.PRO, "seat_c", [b, c]) is PriceKey.PRO_ADDITIONAL


def test_a_tie_in_plan_goes_to_the_oldest_seat_and_then_to_the_id():
    old, new = seat("seat_z", Plan.PRO, 9), seat("seat_a", Plan.PRO, 1)
    assert billing.seat_price(Plan.PRO, "seat_z", [new, old]) is PriceKey.PRO_FIRST
    assert billing.seat_price(Plan.PRO, "seat_a", [new, old]) is PriceKey.PRO_ADDITIONAL
    same_a, same_b = seat("seat_a", Plan.PRO, 5), seat("seat_b", Plan.PRO, 5)
    for order in ([same_a, same_b], [same_b, same_a]):
        assert billing.seat_price(Plan.PRO, "seat_a", order) is PriceKey.PRO_FIRST
        assert billing.seat_price(Plan.PRO, "seat_b", order) is PriceKey.PRO_ADDITIONAL


def test_seat_price_gives_exactly_what_plans_price_lines_gives():
    rng = random.Random(20271314)
    for _ in range(300):
        seats = [
            seat(f"seat_{i}", rng.choice(plans.PAID_PLANS), rng.randint(0, 5))
            for i in range(rng.randint(1, 6))
        ]
        lines = plans.price_lines(ActiveLocation(s.seat_id, s.plan, s.created_at) for s in seats)
        for s in seats:
            key = billing.seat_price(s.plan, s.seat_id, seats)
            assert billing.price_cents(key) == lines[s.seat_id]


def test_free_has_no_seat_price():
    with pytest.raises(ValueError):
        billing.seat_price(Plan.GRATIS, "seat_x", [])


def test_new_seat_price_for_get_me():
    assert billing.new_seat_prices([]) == {"pro": 3500, "pro_whatsapp": 5900}
    assert billing.new_seat_prices([seat("seat_a", Plan.PRO, 3)]) == {
        "pro": 3325,
        "pro_whatsapp": 5900,
    }
    assert billing.new_seat_prices([seat("seat_a", Plan.PRO_WHATSAPP, 3)]) == {
        "pro": 3325,
        "pro_whatsapp": 5605,
    }


def test_seats_counting_at_a_renewal():
    renewal = NOW + 30 * DAY
    paid_after = seat("paid", Plan.PRO, 100, current_period_end=renewal + 50 * DAY)
    leaving_after = seat(
        "leaving",
        Plan.PRO,
        90,
        current_period_end=renewal + 10 * DAY,
        cancel_at_period_end=True,
        location_id="loc_b",
    )
    leaving_before = seat(
        "gone",
        Plan.PRO_WHATSAPP,
        80,
        current_period_end=renewal - DAY,
        cancel_at_period_end=True,
        location_id="loc_c",
    )
    renews_before = seat(
        "renews", Plan.PRO, 70, current_period_end=renewal - DAY, location_id="loc_d"
    )
    unassigned_before = seat("empty", Plan.PRO_WHATSAPP, 60, current_period_end=renewal - DAY)
    charged = seat("charged", Plan.PRO, 50, current_period_end=renewal, location_id="loc_e")
    counting = billing.seats_counting_at(
        [paid_after, leaving_after, leaving_before, renews_before, unassigned_before, charged],
        renewal,
    )
    assert [s.seat_id for s in counting] == ["paid", "leaving", "renews", "charged"]


# --- decision 4: a seat without a profile is not renewed ------------------------------


def test_renewal_action():
    assert billing.renewal_action(seat("s", Plan.PRO, location_id="loc_a")) is RenewalAction.RENEW
    assert billing.renewal_action(seat("s", Plan.PRO)) is RenewalAction.CANCEL_UNASSIGNED
    cancelling = seat("s", Plan.PRO, location_id="loc_a", cancel_at_period_end=True)
    assert billing.renewal_action(cancelling) is RenewalAction.END
    assert billing.renewal_action(seat("s", Plan.PRO, cancel_at_period_end=True)) is (
        RenewalAction.END
    )


# --- tax treatment -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("postal_code", "territory", "treatment"),
    [
        ("18001", Territory.PENINSULA_BALEARES, TaxTreatment.IVA_21),
        ("07001", Territory.PENINSULA_BALEARES, TaxTreatment.IVA_21),
        ("28013", Territory.PENINSULA_BALEARES, TaxTreatment.IVA_21),
        ("35001", Territory.CANARIAS, TaxTreatment.NO_SUJETO),
        ("38001", Territory.CANARIAS, TaxTreatment.NO_SUJETO),
        ("51001", Territory.CEUTA, TaxTreatment.NO_SUJETO),
        ("52001", Territory.MELILLA, TaxTreatment.NO_SUJETO),
        (" 35 500 ", Territory.CANARIAS, TaxTreatment.NO_SUJETO),
        # Provinces next to the special ones stay under the 21 %.
        ("34001", Territory.PENINSULA_BALEARES, TaxTreatment.IVA_21),
        ("36001", Territory.PENINSULA_BALEARES, TaxTreatment.IVA_21),
        ("37001", Territory.PENINSULA_BALEARES, TaxTreatment.IVA_21),
        ("39001", Territory.PENINSULA_BALEARES, TaxTreatment.IVA_21),
        ("50001", Territory.PENINSULA_BALEARES, TaxTreatment.IVA_21),
    ],
)
def test_spanish_postal_codes(postal_code, territory, treatment):
    profile = billing.tax_treatment("ES", postal_code, "12345678Z")
    assert profile.territory is territory
    assert profile.treatment is treatment
    assert profile.tax_id == "12345678Z"
    assert profile.tax_id_kind is TaxIdKind.NIF
    assert profile.stripe_tax_id_type == "es_cif"


@pytest.mark.parametrize("postal_code", ["", "1800", "180011", "00123", "53001", "99999", "18O01"])
def test_invalid_spanish_postal_codes(postal_code):
    with pytest.raises(BillingInputError) as error:
        billing.tax_treatment("ES", postal_code, "12345678Z")
    assert error.value.code == "postal_code_invalid"


def test_the_rates_notes_and_fiscal_positions_of_each_treatment():
    assert billing.TAX_PERCENT == {
        TaxTreatment.IVA_21: 21,
        TaxTreatment.NO_SUJETO: 0,
        TaxTreatment.ISP_UE: 0,
    }
    assert billing.INVOICE_NOTE[TaxTreatment.IVA_21] is None
    assert billing.INVOICE_NOTE[TaxTreatment.NO_SUJETO] == (
        "Operación no sujeta a IVA (arts. 3 y 69 de la Ley 37/1992)"
    )
    assert "Inversión del sujeto pasivo" in billing.INVOICE_NOTE[TaxTreatment.ISP_UE]
    assert billing.FISCAL_POSITION == {
        Territory.PENINSULA_BALEARES: "nacional",
        Territory.CANARIAS: "canarias",
        Territory.CEUTA: "extracomunitario",
        Territory.MELILLA: "extracomunitario",
        Territory.UE: "intracomunitario",
    }


def test_eu_customers_are_reverse_charged_with_their_vat_number():
    profile = billing.tax_treatment("fr", "75001", "fr 40 303 265 045")
    assert profile.territory is Territory.UE
    assert profile.treatment is TaxTreatment.ISP_UE
    assert profile.tax_id == "FR40303265045"
    assert profile.tax_id_kind is TaxIdKind.EU_VAT
    assert profile.stripe_tax_id_type == "eu_vat"
    # Greece: GR in the address, EL in the VAT number; the prefix may be left out.
    assert billing.tax_treatment("GR", "10558", "123456789").tax_id == "EL123456789"
    # The postal code abroad is not checked here.
    assert billing.tax_treatment("DE", "", "DE123456789").treatment is TaxTreatment.ISP_UE


def test_a_spanish_customer_needs_a_spanish_tax_id_and_an_eu_one_a_vat_number_of_its_country():
    with pytest.raises(BillingInputError) as error:
        billing.tax_treatment("ES", "18001", "FR40303265045")
    assert error.value.code == "tax_id_invalid"
    with pytest.raises(BillingInputError) as error:
        billing.tax_treatment("FR", "75001", "DE123456789")
    assert error.value.code == "tax_id_invalid"
    with pytest.raises(BillingInputError) as error:
        billing.tax_treatment("FR", "75001", "")
    assert error.value.code == "tax_id_invalid"


@pytest.mark.parametrize("country", ["US", "GB", "CH", "NO", "AD", "XI", "", "ESP", None])
def test_countries_out_of_spain_and_the_eu_are_not_sold_to(country):
    with pytest.raises(BillingInputError) as error:
        billing.tax_treatment(country, "12345", "12345678Z")
    assert error.value.code == "country_not_supported"


def test_spanish_vat_number_with_the_es_prefix_is_accepted():
    profile = billing.tax_treatment("ES", "18001", "ES-B12345674")
    assert profile.tax_id == "B12345674"
    assert profile.tax_id_kind is TaxIdKind.CIF


# --- Spanish tax ids -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "kind", "value"),
    [
        ("12345678Z", TaxIdKind.NIF, "12345678Z"),
        ("12345678-z", TaxIdKind.NIF, "12345678Z"),
        (" 12.345.678 Z ", TaxIdKind.NIF, "12345678Z"),
        ("00000000T", TaxIdKind.NIF, "00000000T"),
        ("X1234567L", TaxIdKind.NIE, "X1234567L"),
        ("Y1234567X", TaxIdKind.NIE, "Y1234567X"),
        ("Z1234567R", TaxIdKind.NIE, "Z1234567R"),
        ("A46103834", TaxIdKind.CIF, "A46103834"),  # control by digit
        ("B12345674", TaxIdKind.CIF, "B12345674"),
        ("P1234567D", TaxIdKind.CIF, "P1234567D"),  # public bodies: control by letter
        ("Q1234567D", TaxIdKind.CIF, "Q1234567D"),
        ("G1234567D", TaxIdKind.CIF, "G1234567D"),  # either digit or letter
        ("G12345674", TaxIdKind.CIF, "G12345674"),
        ("ESB12345674", TaxIdKind.CIF, "B12345674"),
    ],
)
def test_valid_spanish_tax_ids(raw, kind, value):
    tax_id = billing.validate_spanish_tax_id(raw)
    assert tax_id is not None
    assert (tax_id.kind, tax_id.value) == (kind, value)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "12345678A",  # wrong letter
        "1234567Z",  # too short
        "X1234567A",  # wrong NIE letter
        "W1234567L",  # not a NIE prefix
        "A46103835",  # wrong CIF digit
        "B1234567D",  # A, B, E, H: only a digit
        "P12345674",  # P, Q, R, S, W, N: only a letter
        "I12345674",  # not an organisation letter
        "B12345674X",
        "ESX",
        None,
        12345678,
    ],
)
def test_invalid_spanish_tax_ids(raw):
    assert billing.validate_spanish_tax_id(raw) is None


@pytest.mark.parametrize(
    ("country", "raw", "value"),
    [
        ("AT", "ATU12345678", "ATU12345678"),
        ("BE", "0123456789", "BE0123456789"),
        ("DE", "de 123 456 789", "DE123456789"),
        ("FR", "FRXX123456789", "FRXX123456789"),
        ("IE", "IE1234567WA", "IE1234567WA"),
        ("IT", "IT12345678901", "IT12345678901"),
        ("NL", "NL123456789B01", "NL123456789B01"),
        ("PT", "PT123456789", "PT123456789"),
        ("GR", "EL123456789", "EL123456789"),
        ("PL", "PL1234567890", "PL1234567890"),
        ("SE", "SE123456789001", "SE123456789001"),
    ],
)
def test_valid_eu_vat_numbers(country, raw, value):
    assert billing.validate_eu_vat_id(country, raw) == value


@pytest.mark.parametrize(
    ("country", "raw"),
    [
        ("DE", "DE12345678"),  # one digit short
        ("FR", "DE123456789"),  # another country
        ("NL", "NL123456789"),  # without B01
        ("AT", "AT12345678"),  # without the U
        ("GR", "GR123456789"),  # Greece is EL
        ("ES", "ESB12345674"),  # Spain is not reverse charged
        ("US", "US123456789"),
        ("DE", ""),
        ("DE", None),
    ],
)
def test_invalid_eu_vat_numbers(country, raw):
    assert billing.validate_eu_vat_id(country, raw) is None


# --- invoice lines -------------------------------------------------------------------


def test_invoice_lines_of_a_first_pro_seat_in_the_peninsula():
    draft = billing.invoice_lines(
        PriceKey.PRO_FIRST,
        "Bar La Parra",
        date(2027, 3, 14),
        date(2028, 3, 14),
        TaxTreatment.IVA_21,
        Territory.PENINSULA_BALEARES,
    )
    (line,) = draft.lines
    assert line.product == "pro"
    assert line.description == 'Fichaitor Pro, ficha "Bar La Parra", del 14/03/2027 al 13/03/2028'
    assert (line.quantity, line.price_unit_cents, line.tax_percent) == (1, 3500, 21)
    assert line.tax_rate == "iva_21"
    assert (draft.amount_untaxed, draft.amount_tax, draft.amount_total) == (3500, 735, 4235)
    assert draft.legal_note is None
    assert draft.fiscal_position == "nacional"


def test_invoice_lines_of_an_additional_whatsapp_seat_in_the_canaries():
    draft = billing.invoice_lines(
        PriceKey.WHATSAPP_ADDITIONAL,
        "Café\n  «El Puerto»\x00",
        date(2027, 1, 1),
        date(2028, 1, 1),
        TaxTreatment.NO_SUJETO,
        Territory.CANARIAS,
    )
    (line,) = draft.lines
    assert line.product == "pro_whatsapp"
    assert line.description == (
        "Fichaitor Pro+ (con avisos por WhatsApp), "
        'ficha "Café «El Puerto»", del 01/01/2027 al 31/12/2027 '
        "(segunda ficha y siguientes, 5 % de descuento)"
    )
    assert (draft.amount_untaxed, draft.amount_tax, draft.amount_total) == (5605, 0, 5605)
    assert draft.legal_note == "Operación no sujeta a IVA (arts. 3 y 69 de la Ley 37/1992)"
    assert draft.fiscal_position == "canarias"


@pytest.mark.parametrize("key", list(PriceKey))
def test_the_vat_of_every_price_rounds_to_the_cent(key):
    draft = billing.invoice_lines(
        key,
        "X",
        date(2027, 1, 1),
        date(2028, 1, 1),
        TaxTreatment.IVA_21,
        Territory.PENINSULA_BALEARES,
    )
    cents = billing.price_cents(key)
    assert draft.amount_tax == {3500: 735, 3325: 698, 5900: 1239, 5605: 1177}[cents]
    assert draft.amount_total == cents + draft.amount_tax


def test_a_long_profile_name_is_cut_and_a_missing_one_is_not_shown():
    long = billing.invoice_lines(
        PriceKey.PRO_FIRST,
        "A" * 500,
        date(2027, 1, 1),
        date(2028, 1, 1),
        TaxTreatment.IVA_21,
        Territory.PENINSULA_BALEARES,
    )
    assert len(long.lines[0].description) < 200
    missing = billing.invoice_lines(
        PriceKey.PRO_FIRST,
        None,
        date(2027, 1, 1),
        date(2028, 1, 1),
        TaxTreatment.IVA_21,
        Territory.PENINSULA_BALEARES,
    )
    assert missing.lines[0].description == "Fichaitor Pro, del 01/01/2027 al 31/12/2027"


def test_invoice_lines_refuse_a_treatment_that_does_not_fit_the_territory():
    with pytest.raises(ValueError):
        billing.invoice_lines(
            PriceKey.PRO_FIRST,
            "X",
            date(2027, 1, 1),
            date(2028, 1, 1),
            TaxTreatment.IVA_21,
            Territory.CANARIAS,
        )
    with pytest.raises(ValueError):
        billing.invoice_lines(
            PriceKey.PRO_FIRST,
            "X",
            date(2028, 1, 1),
            date(2027, 1, 1),
            TaxTreatment.IVA_21,
            Territory.PENINSULA_BALEARES,
        )


# --- block decisions (decision 9.0, 5 and 6: card only, a pending 3DS is unpaid) ------


@pytest.mark.parametrize(
    ("event", "action", "reason"),
    [
        (
            BillingEvent("invoice.payment_failed", billing_reason="subscription_cycle"),
            BlockAction.OPEN,
            BlockReason.PAYMENT_FAILED,
        ),
        (
            BillingEvent("invoice.payment_action_required", billing_reason="subscription_cycle"),
            BlockAction.OPEN,
            BlockReason.ACTION_REQUIRED,
        ),
        (BillingEvent("charge.dispute.created"), BlockAction.OPEN, BlockReason.DISPUTE),
        # A dispute blocks also on the first payment: the service was given.
        (
            BillingEvent("charge.dispute.created", billing_reason="subscription_create"),
            BlockAction.OPEN,
            BlockReason.DISPUTE,
        ),
        # The first purchase fails with the customer in front: no debt, no block.
        (
            BillingEvent("invoice.payment_failed", billing_reason="subscription_create"),
            BlockAction.NONE,
            None,
        ),
        (
            BillingEvent("invoice.payment_action_required", billing_reason="subscription_create"),
            BlockAction.NONE,
            None,
        ),
        # A payment in progress does not block (and with card only it is brief).
        (BillingEvent("payment_intent.processing"), BlockAction.NONE, None),
        (BillingEvent("customer.subscription.updated"), BlockAction.NONE, None),
        (
            BillingEvent("invoice.paid", billing_reason="subscription_cycle"),
            BlockAction.CLOSE,
            None,
        ),
        (BillingEvent("charge.dispute.closed", dispute_status="won"), BlockAction.CLOSE, None),
        (BillingEvent("charge.dispute.closed", dispute_status="lost"), BlockAction.NONE, None),
    ],
)
def test_block_decision(event, action, reason):
    decision = billing.block_decision(event)
    assert decision.action is action
    assert decision.reason is reason
    if action is BlockAction.CLOSE:
        assert decision.unblocked_by == "payment"


def test_there_is_no_block_timeout():
    assert not hasattr(billing, "BLOCK_CANCEL_DAYS")
    assert "timeout" not in {reason.value for reason in BlockReason}
    assert billing.PAYMENT_METHOD_TYPES == ("card",)


# --- seat state from what Stripe says ------------------------------------------------

END = NOW + 365 * DAY


def snapshot(**kwargs) -> StripeSnapshot:
    values = {
        "subscription_status": "active",
        "current_period_end": END,
        "invoice_status": "paid",
        "invoice_billing_reason": "subscription_cycle",
        "payment_intent_status": "succeeded",
    }
    values.update(kwargs)
    return StripeSnapshot(**values)


def test_a_paid_seat_is_entitled_until_the_end_of_its_year_plus_the_technical_margin():
    state = billing.seat_state(snapshot())
    assert state.status is SeatStatus.ACTIVE
    assert state.entitled_until == END + 2 * DAY
    assert state.block is None


def test_a_failed_renewal_blocks_and_extends_nothing():
    state = billing.seat_state(
        snapshot(
            subscription_status="past_due",
            invoice_status="open",
            payment_intent_status="requires_payment_method",
        )
    )
    assert state.status is SeatStatus.PAYMENT_FAILED
    assert state.entitled_until is None
    assert state.block is BlockReason.PAYMENT_FAILED


def test_a_renewal_waiting_for_3ds_blocks():
    state = billing.seat_state(
        snapshot(
            subscription_status="past_due",
            invoice_status="open",
            payment_intent_status="requires_action",
        )
    )
    assert state.status is SeatStatus.ACTION_REQUIRED
    assert state.block is BlockReason.ACTION_REQUIRED
    assert state.entitled_until is None


def test_a_first_purchase_waiting_for_3ds_or_failed_is_pending_without_a_block():
    for intent in ("requires_action", "requires_payment_method"):
        state = billing.seat_state(
            snapshot(
                subscription_status="incomplete",
                invoice_status="open",
                invoice_billing_reason="subscription_create",
                payment_intent_status=intent,
            )
        )
        assert state.status is SeatStatus.PENDING
        assert state.block is None
        assert state.entitled_until is None


def test_a_payment_in_progress_neither_blocks_nor_grants_a_provisional_right():
    state = billing.seat_state(snapshot(invoice_status="open", payment_intent_status="processing"))
    assert state.status is SeatStatus.PENDING
    assert state.block is None
    assert state.entitled_until is None


def test_a_disputed_seat_blocks_but_keeps_its_right():
    state = billing.seat_state(snapshot(disputed=True))
    assert state.status is SeatStatus.DISPUTED
    assert state.block is BlockReason.DISPUTE
    assert state.entitled_until == END + 2 * DAY


def test_a_cancelled_seat_keeps_its_paid_year_unless_it_was_cancelled_for_not_paying():
    requested = billing.seat_state(
        snapshot(
            subscription_status="canceled",
            canceled_at=END,
            cancellation_reason="cancellation_requested",
        )
    )
    assert requested.status is SeatStatus.CANCELED
    assert requested.entitled_until == END
    assert requested.block is None
    unpaid = billing.seat_state(
        snapshot(
            subscription_status="canceled",
            canceled_at=NOW,
            cancellation_reason="payment_failed",
            invoice_status="void",
            payment_intent_status="canceled",
        )
    )
    assert unpaid.entitled_until == NOW
    expired = billing.seat_state(
        snapshot(
            subscription_status="incomplete_expired",
            invoice_status="void",
            invoice_billing_reason="subscription_create",
        )
    )
    assert expired.status is SeatStatus.CANCELED
    assert expired.entitled_until is None


# --- end of the beta -----------------------------------------------------------------


def beta_profile(location_id, plan, days_ago, gbp=None, has_seat=False):
    return BetaProfile(location_id, plan, NOW - days_ago * DAY, gbp or f"g_{location_id}", has_seat)


def test_enforce_after_beta_moves_the_oldest_unpaid_pro_to_free_and_deactivates_the_rest():
    profiles = [
        beta_profile("new", Plan.PRO, 1),
        beta_profile("old", Plan.PRO, 50),
        beta_profile("mid", Plan.PRO, 20),
        beta_profile("paid", Plan.PRO, 60, has_seat=True),
    ]
    assert billing.enforce_after_beta(profiles, gratis_elsewhere=set()) == {
        "old": EnforceAction.TO_GRATIS,
        "mid": EnforceAction.DEACTIVATE,
        "new": EnforceAction.DEACTIVATE,
        "paid": EnforceAction.KEEP,
    }


def test_enforce_after_beta_without_the_free_profile_left():
    profiles = [beta_profile("free", Plan.GRATIS, 90), beta_profile("pro", Plan.PRO, 10)]
    assert billing.enforce_after_beta(profiles, gratis_elsewhere=set()) == {
        "free": EnforceAction.KEEP,
        "pro": EnforceAction.DEACTIVATE,
    }


def test_enforce_after_beta_skips_a_google_profile_already_free_in_another_account():
    profiles = [
        beta_profile("old", Plan.PRO, 50, gbp="g_shared"),
        beta_profile("new", Plan.PRO, 1),
    ]
    assert billing.enforce_after_beta(profiles, gratis_elsewhere={"g_shared"}) == {
        "old": EnforceAction.DEACTIVATE,
        "new": EnforceAction.TO_GRATIS,
    }


def test_enforce_after_beta_ignores_the_order_of_the_list():
    profiles = [beta_profile(f"p{i}", Plan.PRO, 10 - i) for i in range(4)]
    results = {
        tuple(sorted(billing.enforce_after_beta(order, gratis_elsewhere=set()).items()))
        for order in itertools.permutations(profiles)
    }
    assert len(results) == 1


# --- DMARC of a leave asked by email -------------------------------------------------

AUTHSERV = "mx.fichaitor.es"


def message(*auth_results: str, sender: str = "Ana <ana@ejemplo.es>") -> str:
    headers = [f"Authentication-Results: {value}" for value in auth_results]
    return "\r\n".join(
        [
            *headers,
            "Received: from mail.ejemplo.es by mx.fichaitor.es",
            f"From: {sender}",
            "To: soporte@fichaitor.es",
            "Subject: Baja",
            "Message-ID: <abc123@ejemplo.es>",
            "",
            "Quiero darme de baja.",
            "Authentication-Results: mx.fichaitor.es; dmarc=pass header.from=ejemplo.es",
        ]
    )


PASS = f"{AUTHSERV}; spf=pass smtp.mailfrom=ejemplo.es; dkim=pass header.d=ejemplo.es; dmarc=pass (p=reject dis=none) header.from=ejemplo.es"  # noqa: E501


def test_dmarc_pass_aligned_with_the_from():
    check = billing.parse_dmarc(message(PASS), AUTHSERV)
    assert check.passed
    assert check.reason == "ok"
    assert check.from_address == "ana@ejemplo.es"
    assert check.from_domain == "ejemplo.es"
    assert len(check.message_id_sha256) == 64


def test_dmarc_only_the_first_header_counts_a_forged_pass_below_is_ignored():
    forged = f"{AUTHSERV}; dmarc=pass header.from=ejemplo.es"
    ours = f"{AUTHSERV}; spf=fail smtp.mailfrom=atacante.com; dmarc=fail header.from=ejemplo.es"
    check = billing.parse_dmarc(message(ours, forged), AUTHSERV)
    assert not check.passed
    assert check.reason == "dmarc_not_pass"


def test_dmarc_a_forged_fail_below_does_not_spoil_our_pass():
    forged = f"{AUTHSERV}; dmarc=fail header.from=ejemplo.es"
    assert billing.parse_dmarc(message(PASS, forged), AUTHSERV).passed


def test_dmarc_a_folded_header_and_any_case():
    folded = (
        "MX.Fichaitor.ES;\r\n\tspf=pass smtp.mailfrom=ejemplo.es;\r\n"
        "  DMARC=Pass (p=none) header.from=Ejemplo.ES"
    )
    raw = message(folded).replace("Authentication-Results:", "authentication-results:", 1)
    assert billing.parse_dmarc(raw, AUTHSERV).passed


@pytest.mark.parametrize(
    ("auth_results", "reason"),
    [
        ((), "no_authentication_results"),
        (("otro.servidor.com; dmarc=pass header.from=ejemplo.es",), "wrong_authserv_id"),
        (
            (f"{AUTHSERV}; spf=pass smtp.mailfrom=ejemplo.es; dkim=pass header.d=ejemplo.es",),
            "no_dmarc",
        ),  # noqa: E501
        ((f"{AUTHSERV}; dmarc=fail header.from=ejemplo.es",), "dmarc_not_pass"),
        ((f"{AUTHSERV}; dmarc=none header.from=ejemplo.es",), "dmarc_not_pass"),
        ((f"{AUTHSERV}; dmarc=bestguesspass header.from=ejemplo.es",), "dmarc_not_pass"),
        ((f"{AUTHSERV}; dmarc=temperror header.from=ejemplo.es",), "dmarc_not_pass"),
        ((f"{AUTHSERV}; dmarc=pass header.from=otro.es",), "domain_mismatch"),
        ((f"{AUTHSERV}; dmarc=pass",), "domain_mismatch"),
        (
            (f"{AUTHSERV}; dmarc=pass header.from=ejemplo.es; dmarc=fail header.from=ejemplo.es",),
            "ambiguous_dmarc",
        ),  # noqa: E501
        # The pass hidden in a comment is not a result.
        ((f"{AUTHSERV}; dmarc=fail (dmarc=pass) header.from=ejemplo.es",), "dmarc_not_pass"),
    ],
)
def test_dmarc_rejections(auth_results, reason):
    check = billing.parse_dmarc(message(*auth_results), AUTHSERV)
    assert not check.passed
    assert check.reason == reason


def test_dmarc_needs_one_from_address():
    assert billing.parse_dmarc(message(PASS, sender=""), AUTHSERV).reason == "no_from"
    two = "ana@ejemplo.es, eva@ejemplo.es"
    assert billing.parse_dmarc(message(PASS, sender=two), AUTHSERV).reason == "no_from"


def test_dmarc_of_an_empty_message():
    assert billing.parse_dmarc("", AUTHSERV).reason == "no_authentication_results"


# --- the beta window (owner, 2026-11-13) ---------------------------------------------
# Only the accounts of the beta (created before launch_at) keep their beta seats for
# BETA_WINDOW_DAYS after the launch. A seat they buy in that window is first charged
# when it ends, never later and with no extra days. Nobody else has a trial.

LAUNCH = datetime(2027, 3, 1, 8, 0, tzinfo=UTC)
WINDOW_END = LAUNCH + 30 * DAY


def test_the_beta_ends_30_days_after_the_launch():
    assert billing.BETA_WINDOW_DAYS == 30
    assert billing.beta_ends_at(LAUNCH) == WINDOW_END
    assert billing.beta_ends_at(None) is None


def test_a_new_account_pays_its_seat_from_the_first_day():
    for now in (LAUNCH, LAUNCH + 3 * DAY, WINDOW_END + 400 * DAY):
        assert billing.first_charge_at(now, LAUNCH, beta_until=None) is None


def test_a_beta_account_is_first_charged_when_its_window_ends():
    assert billing.first_charge_at(LAUNCH + 5 * DAY, LAUNCH, beta_until=WINDOW_END) == (WINDOW_END)
    # A beta entitlement without an end (the backfill not run) ends with the window.
    assert billing.first_charge_at(LAUNCH + 5 * DAY, LAUNCH, beta_until=None, beta=True) == (
        WINDOW_END
    )
    # Never later than the window, whatever the entitlement says.
    later = WINDOW_END + 90 * DAY
    assert billing.first_charge_at(LAUNCH, LAUNCH, beta_until=later) == WINDOW_END


def test_after_the_window_a_beta_account_pays_from_the_first_day():
    assert billing.first_charge_at(WINDOW_END, LAUNCH, beta_until=WINDOW_END) is None
    assert billing.first_charge_at(WINDOW_END + DAY, LAUNCH, beta=True, beta_until=None) is None


def test_a_beta_window_that_ended_before_its_time_gives_no_free_days():
    # The owner ended the beta right sooner (a beta entitlement that already ended).
    gone = LAUNCH + 2 * DAY
    assert billing.first_charge_at(LAUNCH + 3 * DAY, LAUNCH, beta_until=gone) is None


def test_the_last_hours_of_the_window_are_stretched_to_what_checkout_accepts():
    # Checkout refuses a trial_end less than 48 hours ahead: the first charge waits a
    # little more instead of charging a year that overlaps the beta.
    now = WINDOW_END - 10 * timedelta(hours=1)
    assert billing.first_charge_at(now, LAUNCH, beta_until=WINDOW_END) == (
        now + billing.MIN_TRIAL_AHEAD
    )
    assert billing.MIN_TRIAL_AHEAD > timedelta(hours=48)


def test_nothing_is_charged_before_the_launch():
    with pytest.raises(ValueError):
        billing.first_charge_at(LAUNCH - DAY, LAUNCH, beta_until=None)
    with pytest.raises(ValueError):
        billing.first_charge_at(LAUNCH, None, beta_until=None)


def test_a_seat_waiting_for_its_first_charge_is_entitled_until_then():
    trial = snapshot(
        subscription_status="trialing",
        current_period_end=WINDOW_END,
        invoice_billing_reason="subscription_create",
    )
    state = billing.seat_state(trial)
    assert state.status is SeatStatus.ACTIVE
    assert state.entitled_until == WINDOW_END + 2 * DAY
    assert state.block is None


def test_a_first_charge_after_the_window_that_fails_or_waits_for_3ds_blocks_as_a_renewal():
    failed = billing.seat_state(
        snapshot(
            subscription_status="past_due",
            invoice_status="open",
            invoice_billing_reason="subscription_cycle",
            payment_intent_status="requires_payment_method",
        )
    )
    assert failed.block is BlockReason.PAYMENT_FAILED
    waiting = billing.seat_state(
        snapshot(
            subscription_status="past_due",
            invoice_status="open",
            invoice_billing_reason="subscription_cycle",
            payment_intent_status="requires_action",
        )
    )
    assert waiting.block is BlockReason.ACTION_REQUIRED
    event = BillingEvent("invoice.payment_action_required", "subscription_cycle")
    assert billing.block_decision(event).action is BlockAction.OPEN


def test_the_beta_window_days_left():
    assert billing.beta_window_days_left(LAUNCH + 5 * DAY, LAUNCH, beta_until=WINDOW_END) == 25
    assert (
        billing.beta_window_days_left(
            WINDOW_END - timedelta(hours=1), LAUNCH, beta_until=WINDOW_END
        )
        == 1
    )
    assert billing.beta_window_days_left(WINDOW_END, LAUNCH, beta_until=WINDOW_END) is None
    assert billing.beta_window_days_left(LAUNCH + DAY, LAUNCH, beta_until=None) is None
    assert billing.beta_window_days_left(LAUNCH - DAY, LAUNCH, beta_until=WINDOW_END) is None
