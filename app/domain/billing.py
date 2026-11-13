"""Billing rules of Fichaitor: what a seat costs, which tax applies, what goes on the
invoice, when an account is blocked and what the end of the beta does. No I/O.

docs/admin-facturacion.md (step 1 of section 7), with the decisions of the owner of
2026-11-13 (sections 1.1 and 9.0), which prevail over the rest of that document:

- A seat is a paid year of a plan, charged whole the day it is bought and renewed on its
  anniversary (one Stripe subscription per seat). Nothing charged is given back.
- The 5 % is `plans.price_lines` applied, when a seat is charged, to the seats in force:
  the dearest plan pays in full and, if two tie, the oldest seat. Here it is reused,
  never written again.
- Card only at the launch (Apple Pay and Google Pay are cards to Stripe). No SEPA, so no
  payment "in progress" grants a provisional right. A 3DS left undone on a renewal is a
  non-payment.
- A non-payment blocks the whole account at once, with no time limit: there is no
  `block_timeout` and no seat is lost while it lasts. Only a payment (or the owner, by
  hand) unblocks it.
- A seat without a profile at its renewal is not charged: it is cancelled.
- Sold in Spain (peninsula, Baleares, Canarias, Ceuta and Melilla) and in the EU from
  the start, always B2B with a NIF or an EU VAT number. The VAT number is only checked
  for its form here; VIES is another step.
- Sold to the whole world (owner, 2026-11-13, decision 9.0, 12): outside the EU only to
  businesses, with a tax id of their country whose form is not checked (3 to 32 letters,
  digits and a few separators). Not subject to Spanish VAT (art. 69.Uno.1.º of Ley
  37/1992). Countries under sanctions or that Stripe Spain does not serve are refused.
  Always in euros.
- The beta window (owner, 2026-11-13): the beta is free until `launch_at`. The accounts
  of the beta keep their beta seats for BETA_WINDOW_DAYS more (`beta_ends_at`) to choose
  a plan; a seat one of them buys in that window is first charged when it ends, with no
  days added. Nobody else has a trial: a seat of an account created from the launch is
  charged the day it is bought.

Points marked "(gestoría)" in the document are still to be confirmed by the tax advisers.
"""

import hashlib
import math
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from email.utils import getaddresses
from enum import StrEnum

from app.domain.plans import (
    FREE_PER_ACCOUNT,
    PAID_PLANS,
    ActiveLocation,
    Plan,
    additional_price,
    first_price,
    price_lines,
)

# Card only (decision 9.0, 5 and 6). Apple Pay and Google Pay arrive as `card`.
PAYMENT_METHOD_TYPES = ("card",)
# The right of a paid seat lasts until the end of its year plus this: the delay of a
# webhook, not a grace period (section 4.2).
ENTITLEMENT_MARGIN = timedelta(days=2)
RENEWAL_NOTICE_DAYS = 30  # the price is frozen and announced (clause 7 of the terms)
UNASSIGNED_CANCEL_DAYS = 2  # a seat still without a profile this close to renewal ends
BETA_WINDOW_DAYS = 30  # after launch_at, for the accounts of the beta to choose a plan
BETA_WINDOW_NOTICE_DAYS = 7  # the email `trial_ending`, before the first charge
# Checkout refuses a `trial_end` less than 48 hours ahead (docs.stripe.com, API reference
# of Checkout Sessions, `subscription_data.trial_end`, read on 2026-11-13). One more hour
# covers the 31 minutes a Checkout stays open.
MIN_TRIAL_AHEAD = timedelta(hours=49)


# --- price of a seat -----------------------------------------------------------------


class PriceKey(StrEnum):
    """The `lookup_key` of each yearly price in Stripe (section 3.1)."""

    PRO_FIRST = "pro_first_yearly"
    PRO_ADDITIONAL = "pro_additional_yearly"
    WHATSAPP_FIRST = "whatsapp_first_yearly"
    WHATSAPP_ADDITIONAL = "whatsapp_additional_yearly"


_PRICE_KEYS: Mapping[tuple[Plan, bool], PriceKey] = {
    (Plan.PRO, False): PriceKey.PRO_FIRST,
    (Plan.PRO, True): PriceKey.PRO_ADDITIONAL,
    (Plan.PRO_WHATSAPP, False): PriceKey.WHATSAPP_FIRST,
    (Plan.PRO_WHATSAPP, True): PriceKey.WHATSAPP_ADDITIONAL,
}
_KEY_PARTS = {key: parts for parts, key in _PRICE_KEYS.items()}


def price_plan(key: PriceKey) -> Plan:
    return _KEY_PARTS[key][0]


def is_additional(key: PriceKey) -> bool:
    return _KEY_PARTS[key][1]


def price_cents(key: PriceKey) -> int:
    """Cents a year, without VAT: the same figures as `plans`."""
    plan, additional = _KEY_PARTS[key]
    return additional_price(plan) if additional else first_price(plan)


@dataclass(frozen=True)
class Seat:
    """A row of billing_seat, with what the rules here need. `created_at` is the age
    that settles a tie in the 5 %; `location_id` None is a seat without a profile."""

    seat_id: str
    plan: Plan
    created_at: datetime
    current_period_end: datetime | None = None
    cancel_at_period_end: bool = False
    location_id: str | None = None


def seat_price(plan: Plan, seat_id: str, in_force: Iterable[Seat]) -> PriceKey:
    """The price of seat `seat_id` of `plan` when it is charged (purchase or renewal).

    `in_force` is the paid seats of the account at that moment (`seats_counting_at`).
    The seat charged may be among them; if it is not, it is a new one and counts as the
    newest. The answer is exactly that of `plans.price_lines` over those seats."""
    if plan not in PAID_PLANS:
        raise ValueError(f"plan without a price: {plan}")
    seats = {s.seat_id: s for s in in_force if s.plan in PAID_PLANS}
    if seat_id in seats:
        seats[seat_id] = replace(seats[seat_id], plan=plan)
    else:
        newest = max((s.created_at for s in seats.values()), default=None)
        created = newest + timedelta(microseconds=1) if newest else datetime.min
        seats[seat_id] = Seat(seat_id, plan, created)
    lines = price_lines(ActiveLocation(s.seat_id, s.plan, s.created_at) for s in seats.values())
    additional = lines[seat_id] != first_price(plan)
    return _PRICE_KEYS[(plan, additional)]


def new_seat_prices(in_force: Iterable[Seat]) -> dict[str, int]:
    """`billing.new_seat_price` of GET /me: what a new seat of each plan costs today, in
    cents, without VAT, for a whole year."""
    seats = list(in_force)
    return {plan.value: price_cents(seat_price(plan, "\x00new", seats)) for plan in PAID_PLANS}


def seats_counting_at(seats: Iterable[Seat], at: datetime) -> list[Seat]:
    """The seats that count for the 5 % of a charge at `at` (for a renewal, its date;
    the price is frozen at the notice, RENEWAL_NOTICE_DAYS before):

    - a seat whose paid year still covers `at`, even if it will not renew;
    - a seat that renews before or at `at`: not cancelled and with a profile (a seat
      without one is cancelled at its renewal, decision 9.0, 4).
    """
    counting = []
    for seat in seats:
        if seat.plan not in PAID_PLANS:
            continue
        end = seat.current_period_end
        if (end is not None and end > at) or renewal_action(seat) is RenewalAction.RENEW:
            counting.append(seat)
    return counting


class RenewalAction(StrEnum):
    RENEW = "renew"
    CANCEL_UNASSIGNED = "cancel_unassigned"  # decision 9.0, 4: not charged
    END = "end"  # cancelled by the customer: it ends at the end of its year


def renewal_action(seat: Seat) -> RenewalAction:
    """What happens to a seat at its renewal. The notice at T-30 warns of a seat without
    a profile; at T-UNASSIGNED_CANCEL_DAYS it is cancelled if it still has none."""
    if seat.cancel_at_period_end:
        return RenewalAction.END
    if seat.location_id is None:
        return RenewalAction.CANCEL_UNASSIGNED
    return RenewalAction.RENEW


# --- tax -----------------------------------------------------------------------------


class TaxTreatment(StrEnum):
    """`billing_customer.tax_treatment`, also the key of the Stripe Tax Rate."""

    IVA_21 = "iva_21"
    NO_SUJETO = "no_sujeto"  # Canarias, Ceuta and Melilla
    ISP_UE = "isp_ue"  # a business of another EU country: reverse charge
    # A business outside the EU: not subject to Spanish VAT because the service is
    # located where the customer is (art. 69.Uno.1.º of Ley 37/1992).
    EXTRACOMUNITARIO = "extracomunitario"


class Territory(StrEnum):
    PENINSULA_BALEARES = "peninsula_baleares"
    CANARIAS = "canarias"
    CEUTA = "ceuta"
    MELILLA = "melilla"
    UE = "ue"
    FUERA_UE = "fuera_ue"  # any other country Fichaitor sells to


class TaxIdKind(StrEnum):
    NIF = "nif"  # DNI: 8 digits and a letter
    NIE = "nie"  # X, Y or Z, 7 digits and a letter
    CIF = "cif"  # an organisation: a letter, 7 digits and a control
    EU_VAT = "eu_vat"
    # The tax id of a business outside the EU (EIN, RFC, UID, ABN...), as typed and
    # normalised: its form is not checked by country.
    FOREIGN = "foreign"


TAX_PERCENT: Mapping[TaxTreatment, int] = {
    TaxTreatment.IVA_21: 21,
    TaxTreatment.NO_SUJETO: 0,
    TaxTreatment.ISP_UE: 0,
    TaxTreatment.EXTRACOMUNITARIO: 0,
}
# The legal mention at the foot of the invoice (sections 2.1 and 2.2). (gestoría)
INVOICE_NOTE: Mapping[TaxTreatment, str | None] = {
    TaxTreatment.IVA_21: None,
    TaxTreatment.NO_SUJETO: "Operación no sujeta a IVA (arts. 3 y 69 de la Ley 37/1992)",
    TaxTreatment.ISP_UE: (
        "Operación no sujeta a IVA en España (art. 69.Uno.1.º de la Ley 37/1992). "
        "Inversión del sujeto pasivo (art. 196 de la Directiva 2006/112/CE)"
    ),
    TaxTreatment.EXTRACOMUNITARIO: (
        "Operación no sujeta al IVA español por reglas de localización (art. 69 Ley 37/1992)"
    ),
}
# The fiscal position in Odoo, by the name the bridge maps to its record. Outside the EU
# it is the same «extracomunitario» as Ceuta and Melilla (the «Régimen Extracomunitario»
# of the Spanish chart); the tax of the line is what tells them apart.
FISCAL_POSITION: Mapping[Territory, str] = {
    Territory.PENINSULA_BALEARES: "nacional",
    Territory.CANARIAS: "canarias",
    Territory.CEUTA: "extracomunitario",
    Territory.MELILLA: "extracomunitario",
    Territory.UE: "intracomunitario",
    Territory.FUERA_UE: "extracomunitario",
}
_TREATMENT: Mapping[Territory, TaxTreatment] = {
    Territory.PENINSULA_BALEARES: TaxTreatment.IVA_21,
    Territory.CANARIAS: TaxTreatment.NO_SUJETO,
    Territory.CEUTA: TaxTreatment.NO_SUJETO,
    Territory.MELILLA: TaxTreatment.NO_SUJETO,
    Territory.UE: TaxTreatment.ISP_UE,
    Territory.FUERA_UE: TaxTreatment.EXTRACOMUNITARIO,
}
# First two digits of a Spanish postal code (the province) outside the 21 %.
_SPECIAL_PROVINCES = {
    "35": Territory.CANARIAS,
    "38": Territory.CANARIAS,
    "51": Territory.CEUTA,
    "52": Territory.MELILLA,
}

# VAT numbers of the other member states, with their prefix (the VIES formats). The
# address uses the ISO code; Greece is EL in a VAT number.
_EU_VAT_FORMATS: Mapping[str, tuple[str, str]] = {
    "AT": ("AT", r"U\d{8}"),
    "BE": ("BE", r"[01]\d{9}"),
    "BG": ("BG", r"\d{9,10}"),
    "CY": ("CY", r"\d{8}[A-Z]"),
    "CZ": ("CZ", r"\d{8,10}"),
    "DE": ("DE", r"\d{9}"),
    "DK": ("DK", r"\d{8}"),
    "EE": ("EE", r"\d{9}"),
    "FI": ("FI", r"\d{8}"),
    "FR": ("FR", r"[0-9A-HJ-NP-Z]{2}\d{9}"),
    "GR": ("EL", r"\d{9}"),
    "HR": ("HR", r"\d{11}"),
    "HU": ("HU", r"\d{8}"),
    "IE": ("IE", r"\d{7}[A-W][A-IW]?|\d[A-Z+*]\d{5}[A-W]"),
    "IT": ("IT", r"\d{11}"),
    "LT": ("LT", r"\d{9}|\d{12}"),
    "LU": ("LU", r"\d{8}"),
    "LV": ("LV", r"\d{11}"),
    "MT": ("MT", r"\d{8}"),
    "NL": ("NL", r"\d{9}B\d{2}"),
    "PL": ("PL", r"\d{10}"),
    "PT": ("PT", r"\d{9}"),
    "RO": ("RO", r"[1-9]\d{1,9}"),
    "SE": ("SE", r"\d{10}01"),
    "SI": ("SI", r"\d{8}"),
    "SK": ("SK", r"\d{10}"),
}
EU_COUNTRIES = frozenset(_EU_VAT_FORMATS)  # without Spain
# Monaco is not in the EU but is in its VAT territory, as part of France (art. 7.1 of
# Directive 2006/112/EC): its businesses have a French VAT number (FR...) and are
# reverse charged like a French one.
_VAT_AREA_ALIASES: Mapping[str, str] = {"MC": "FR"}
EU_VAT_COUNTRIES = EU_COUNTRIES | frozenset(_VAT_AREA_ALIASES)

# Every officially assigned ISO 3166-1 alpha-2 code (249), plus XK (Kosovo: user
# assigned, but the one Stripe, the EU and Odoo use). Canarias (IC), Ceuta and Melilla
# (EA) are not here on purpose: they are ES with their postal code, as always.
ISO_COUNTRIES = frozenset(
    """
    AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN
    BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ
    DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL
    GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM
    JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME
    MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP
    NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD
    SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO
    TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS XK YE YT ZA ZM ZW
    """.split()
)
# Not sold to: countries under comprehensive sanctions of the EU, Spain or the United
# States that Stripe does not serve (Cuba, Iran, North Korea, Syria) and those under EU
# sanctions that forbid selling business software to them (Russia, Belarus). The
# occupied regions of Ukraine (Crimea, Sevastopol, Donetsk, Luhansk, Zaporizhzhia and
# Kherson) have no ISO code of their own: they are UA, so an address there cannot be
# told apart here. Stripe screens the card and the payment (docs/admin-facturacion.md,
# 2.1).
BLOCKED_COUNTRIES = frozenset({"BY", "CU", "IR", "KP", "RU", "SY"})
# Outside the EU VAT area and not refused: not subject to Spanish VAT.
WORLD_COUNTRIES = ISO_COUNTRIES - BLOCKED_COUNTRIES - EU_VAT_COUNTRIES - {"ES"}


class CountryRegion(StrEnum):
    """How the form and the tax treat a country (GET /me/billing/countries)."""

    ES = "es"  # NIF, NIE or CIF; postal code of 5 digits, required
    UE = "ue"  # VAT number of that country; postal code required
    FUERA_UE = "fuera_ue"  # tax id of the business; postal code optional


def country_region(country: object) -> CountryRegion | None:
    """The region of an ISO code, or None if it is not sold to (unknown or refused)."""
    code = country.strip().upper() if isinstance(country, str) else ""
    if code == "ES":
        return CountryRegion.ES
    if code in EU_VAT_COUNTRIES:
        return CountryRegion.UE
    if code in WORLD_COUNTRIES:
        return CountryRegion.FUERA_UE
    return None


def countries_for_form() -> list[tuple[str, CountryRegion]]:
    """Every country sold to, by its code, with its region."""
    return [(code, region) for code in sorted(ISO_COUNTRIES) if (region := country_region(code))]


_DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"
_CIF_LETTERS = "JABCDEFGHI"
_CIF_DIGIT_ONLY = set("ABEH")
_CIF_LETTER_ONLY = set("KPQRSNW")
_CIF_FIRST = set("ABCDEFGHJNPQRSUVW")
_SEPARATORS = re.compile(r"[\s.\-_/]+")


class BillingInputError(ValueError):
    """Fiscal data that cannot be used. `code` is the one the API answers with 422."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SpanishTaxId:
    kind: TaxIdKind
    value: str  # upper case, without separators nor the ES prefix


@dataclass(frozen=True)
class TaxProfile:
    """What `billing_customer` keeps of the fiscal data of an account."""

    territory: Territory
    treatment: TaxTreatment
    tax_id: str
    tax_id_kind: TaxIdKind
    # `es_cif` (Stripe's type for every Spanish NIF), `eu_vat`, or None outside the EU:
    # that tax id is kept here and in Odoo only (section 2.7).
    stripe_tax_id_type: str | None
    postal_code: str | None = None  # normalised; None only outside the EU


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return _SEPARATORS.sub("", value).upper()


def validate_spanish_tax_id(value: object) -> SpanishTaxId | None:
    """A Spanish NIF (DNI), NIE or CIF with a right control character, or None. Spaces,
    dots and dashes are ignored, and so is an `ES` prefix (the Spanish VAT number).

    Not accepted: the K, L and M NIFs of people without a DNI, which a business buying
    Fichaitor does not use."""
    text = _clean(value)
    if text is None:
        return None
    if len(text) == 11 and text.startswith("ES"):
        text = text[2:]
    if len(text) != 9:
        return None
    if re.fullmatch(r"\d{8}[A-Z]", text):
        ok = _DNI_LETTERS[int(text[:8]) % 23] == text[8]
        return SpanishTaxId(TaxIdKind.NIF, text) if ok else None
    if re.fullmatch(r"[XYZ]\d{7}[A-Z]", text):
        number = int(str("XYZ".index(text[0])) + text[1:8])
        ok = _DNI_LETTERS[number % 23] == text[8]
        return SpanishTaxId(TaxIdKind.NIE, text) if ok else None
    if re.fullmatch(r"[A-Z]\d{7}[0-9A-J]", text) and text[0] in _CIF_FIRST:
        return SpanishTaxId(TaxIdKind.CIF, text) if _cif_control_ok(text) else None
    return None


def _cif_control_ok(text: str) -> bool:
    digits = [int(c) for c in text[1:8]]
    even = sum(digits[1::2])
    odd = sum(sum(divmod(2 * d, 10)) for d in digits[0::2])
    control = (10 - (even + odd) % 10) % 10
    first, given = text[0], text[8]
    as_digit, as_letter = given == str(control), given == _CIF_LETTERS[control]
    if first in _CIF_DIGIT_ONLY:
        return as_digit
    if first in _CIF_LETTER_ONLY:
        return as_letter
    return as_digit or as_letter


def validate_eu_vat_id(country: object, value: object) -> str | None:
    """The VAT number of a business of another member state, normalised with its prefix
    (`FR40303265045`, `EL123456789`), or None. Its form only: whether it exists and is
    active is asked to VIES elsewhere, before charging (decision 9.0, 9)."""
    if not isinstance(country, str):
        return None
    code = _VAT_AREA_ALIASES.get(country.upper(), country.upper())
    if code not in _EU_VAT_FORMATS:
        return None
    text = _clean(value)
    if not text:
        return None
    prefix, pattern = _EU_VAT_FORMATS[code]
    if text.startswith(prefix):
        body = text[2:]
    elif text[:2].isalpha():
        return None  # the prefix of another country (a French body is asked with FR)
    else:
        body = text
    return prefix + body if re.fullmatch(pattern, body) else None


FOREIGN_TAX_ID_MIN = 3
FOREIGN_TAX_ID_MAX = 32
# Letters, digits, "-", ".", "/" and a single space; Ñ and & as well, which the Mexican
# RFC may carry. At least one digit: every tax id has some, and "N/A" is not one.
_FOREIGN_TAX_ID = re.compile(r"[A-Z0-9Ñ&./ -]+")
# A postal code abroad: letters, digits, spaces and dashes, 2 to 12, starting and
# ending with a letter or a digit ("SW1A 1AA", "10001-1234", "KY1-1104").
_POSTAL_ABROAD = re.compile(r"[A-Z0-9][A-Z0-9 -]{0,10}[A-Z0-9]")


def validate_foreign_tax_id(value: object) -> str | None:
    """The tax id of a business outside the EU, normalised (upper case, spaces folded,
    separators kept as typed: `12-3456789`, `CHE-123.456.789 MWST`), or None. Only its
    characters and its length: no format by country, and nothing asked to any registry."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split()).upper()
    if not FOREIGN_TAX_ID_MIN <= len(text) <= FOREIGN_TAX_ID_MAX:
        return None
    if not _FOREIGN_TAX_ID.fullmatch(text) or not any(c.isdigit() for c in text):
        return None
    return text


def _spanish_postal_code(postal_code: object) -> tuple[str, Territory]:
    code = _clean(postal_code) or ""
    if not re.fullmatch(r"\d{5}", code) or not 1 <= int(code[:2]) <= 52:
        raise BillingInputError("postal_code_invalid")
    return code, _SPECIAL_PROVINCES.get(code[:2], Territory.PENINSULA_BALEARES)


def _postal_code_abroad(postal_code: object, *, required: bool) -> str | None:
    """A postal code of another country, normalised, or None if it may be left out."""
    text = " ".join(postal_code.split()).upper() if isinstance(postal_code, str) else ""
    if not text:
        if required:
            raise BillingInputError("postal_code_invalid")
        return None
    if not _POSTAL_ABROAD.fullmatch(text):
        raise BillingInputError("postal_code_invalid")
    return text


def tax_treatment(country: object, postal_code: object, tax_id: object) -> TaxProfile:
    """The tax of a customer from the country and postal code of its address and its tax
    id (section 2.1 and decisions 9.0, 9 and 12). Raises BillingInputError with the code
    of the API: `country_not_supported`, `postal_code_invalid` or `tax_id_invalid`.

    - Spain, postal code 35 or 38 (Canarias), 51 (Ceuta) or 52 (Melilla): not subject to
      Spanish VAT. Any other Spanish province: 21 %. Always with a NIF, NIE or CIF.
    - Another member state (or Monaco): reverse charge, with a VAT number of that
      country. Postal code required, of any form.
    - Any other country but those refused: not subject to Spanish VAT by the place of
      supply, only to a business with its tax id. Postal code optional.
    """
    region = country_region(country)
    code = country.strip().upper() if isinstance(country, str) else ""
    if region is CountryRegion.ES:
        postal, territory = _spanish_postal_code(postal_code)
        spanish = validate_spanish_tax_id(tax_id)
        if spanish is None:
            raise BillingInputError("tax_id_invalid")
        return TaxProfile(
            territory, _TREATMENT[territory], spanish.value, spanish.kind, "es_cif", postal
        )
    if region is CountryRegion.UE:
        postal = _postal_code_abroad(postal_code, required=True)
        vat = validate_eu_vat_id(code, tax_id)
        if vat is None:
            raise BillingInputError("tax_id_invalid")
        return TaxProfile(
            Territory.UE, TaxTreatment.ISP_UE, vat, TaxIdKind.EU_VAT, "eu_vat", postal
        )
    if region is CountryRegion.FUERA_UE:
        postal = _postal_code_abroad(postal_code, required=False)
        foreign = validate_foreign_tax_id(tax_id)
        if foreign is None:
            raise BillingInputError("tax_id_invalid")
        return TaxProfile(
            Territory.FUERA_UE,
            TaxTreatment.EXTRACOMUNITARIO,
            foreign,
            TaxIdKind.FOREIGN,
            None,
            postal,
        )
    raise BillingInputError("country_not_supported")


# --- invoice lines -------------------------------------------------------------------

PRODUCT_NAME: Mapping[Plan, str] = {
    Plan.PRO: "Fichaitor Pro",
    Plan.PRO_WHATSAPP: "Fichaitor Pro+ (con avisos por WhatsApp)",
}
ADDITIONAL_NOTE = "(segunda ficha y siguientes, 5 % de descuento)"
MAX_TITLE_CHARS = 80
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class InvoiceLine:
    product: str  # the plan, which the bridge maps to its Odoo product
    description: str
    quantity: int
    price_unit_cents: int  # without VAT
    tax_rate: str  # the Stripe Tax Rate key, the same as the treatment
    tax_percent: int


@dataclass(frozen=True)
class InvoiceDraft:
    """What `fichaitor_bridge.issue` needs besides the customer: lines, the totals that
    must equal those of Stripe to the cent, the legal mention and the fiscal position."""

    lines: tuple[InvoiceLine, ...]
    amount_untaxed: int
    amount_tax: int
    amount_total: int
    legal_note: str | None
    fiscal_position: str


def _tax_cents(base_cents: int, percent: int) -> int:
    """Tax of a line, rounded per line and half up, as Stripe does."""
    return (base_cents * percent + 50) // 100


def _title(location_title: str | None) -> str | None:
    if not location_title:
        return None
    text = " ".join(_CONTROL.sub(" ", location_title).split()).replace('"', "'")
    if len(text) > MAX_TITLE_CHARS:
        text = text[: MAX_TITLE_CHARS - 1].rstrip() + "…"
    return text or None


def invoice_lines(
    price_key: PriceKey,
    location_title: str | None,
    period_start: date,
    period_end: date,
    treatment: TaxTreatment,
    territory: Territory,
) -> InvoiceDraft:
    """The invoice of the charge of a seat (section 2.2): one line, the plan, the profile
    by its name (decision 9.0, 8) and the year paid. `period_end` is the renewal date, in
    Europe/Madrid: the invoice shows the day before as the last one."""
    if treatment is not _TREATMENT[territory]:
        raise ValueError(f"{treatment} does not apply to {territory}")
    if period_end <= period_start:
        raise ValueError("the period ends before it starts")
    plan = price_plan(price_key)
    last_day = period_end - timedelta(days=1)
    parts = [PRODUCT_NAME[plan]]
    title = _title(location_title)
    if title:
        parts.append(f'ficha "{title}"')
    parts.append(f"del {period_start:%d/%m/%Y} al {last_day:%d/%m/%Y}")
    description = ", ".join(parts)
    if is_additional(price_key):
        description = f"{description} {ADDITIONAL_NOTE}"
    percent = TAX_PERCENT[treatment]
    base = price_cents(price_key)
    line = InvoiceLine(plan.value, description, 1, base, treatment.value, percent)
    tax = _tax_cents(base, percent)
    return InvoiceDraft(
        lines=(line,),
        amount_untaxed=base,
        amount_tax=tax,
        amount_total=base + tax,
        legal_note=INVOICE_NOTE[treatment],
        fiscal_position=FISCAL_POSITION[territory],
    )


# --- blocks --------------------------------------------------------------------------


class BlockReason(StrEnum):
    """`account_billing_block.reason`. There is no timeout (decision 9.0, 2 and 3)."""

    PAYMENT_FAILED = "payment_failed"
    ACTION_REQUIRED = "action_required"  # a 3DS left undone is a non-payment
    DISPUTE = "dispute"
    MANUAL_UNPAID = "manual_unpaid"  # Bizum seats, second phase


class BlockAction(StrEnum):
    OPEN = "open"
    CLOSE = "close"
    NONE = "none"


@dataclass(frozen=True)
class BillingEvent:
    """A Stripe event, with the facts read again from Stripe (the payload never
    decides, section 4.2). `billing_reason` is that of the invoice involved;
    `dispute_status` that of the dispute, for `charge.dispute.closed`."""

    type: str
    billing_reason: str | None = None
    dispute_status: str | None = None


@dataclass(frozen=True)
class BlockDecision:
    action: BlockAction
    reason: BlockReason | None = None
    unblocked_by: str | None = None  # `payment` when closing


_NO_BLOCK = BlockDecision(BlockAction.NONE)
_FIRST_PURCHASE = "subscription_create"
_RENEWAL_FAILURES = {
    "invoice.payment_failed": BlockReason.PAYMENT_FAILED,
    "invoice.payment_action_required": BlockReason.ACTION_REQUIRED,
}
# A dispute closed in our favour gives the money back: the debt is settled.
_DISPUTE_SETTLED = {"won", "warning_closed"}


def block_decision(event: BillingEvent) -> BlockDecision:
    """Whether an event opens or closes the block of the account (section 3.6, P3).

    - A failed charge or a 3DS left undone opens it at once, except on the first
      purchase of a seat: the customer is in front, nothing was given and nothing is
      owed, so the seat simply stays unpaid.
    - A dispute opens it always: the money was taken back after the service was given.
    - `invoice.paid` closes the block of that invoice; so does a dispute won.
    - A payment in progress (`payment_intent.processing`) never blocks.
    """
    if event.type in _RENEWAL_FAILURES:
        if event.billing_reason == _FIRST_PURCHASE:
            return _NO_BLOCK
        return BlockDecision(BlockAction.OPEN, _RENEWAL_FAILURES[event.type])
    if event.type == "charge.dispute.created":
        return BlockDecision(BlockAction.OPEN, BlockReason.DISPUTE)
    if event.type == "invoice.paid":
        return BlockDecision(BlockAction.CLOSE, unblocked_by="payment")
    if event.type == "charge.dispute.closed" and event.dispute_status in _DISPUTE_SETTLED:
        return BlockDecision(BlockAction.CLOSE, unblocked_by="payment")
    return _NO_BLOCK


# --- state of a seat from Stripe -----------------------------------------------------


class SeatStatus(StrEnum):
    ACTIVE = "active"
    PENDING = "pending"  # first payment not done yet, or a payment in progress
    PAYMENT_FAILED = "payment_failed"
    ACTION_REQUIRED = "action_required"
    DISPUTED = "disputed"
    CANCELED = "canceled"


@dataclass(frozen=True)
class StripeSnapshot:
    """A subscription of a seat and its latest invoice, as read from Stripe."""

    subscription_status: str  # active, past_due, unpaid, incomplete, canceled, ...
    current_period_end: datetime
    cancel_at_period_end: bool = False
    canceled_at: datetime | None = None
    cancellation_reason: str | None = None  # cancellation_details.reason
    invoice_status: str | None = None  # draft, open, paid, void, uncollectible
    invoice_billing_reason: str | None = None
    payment_intent_status: str | None = None
    disputed: bool = False


@dataclass(frozen=True)
class SeatState:
    """What `reconcile_seat` copies and decides (section 4.2). `entitled_until`: the end
    of the right of the seat, or None to grant nothing new. `block`: the reason to open
    a block, or None."""

    status: SeatStatus
    entitled_until: datetime | None
    block: BlockReason | None = None


_FAILED_INTENTS = {"requires_payment_method", "canceled"}
_ENDED = {"canceled", "incomplete_expired"}


def seat_state(snapshot: StripeSnapshot) -> SeatState:
    """The state of a seat from what Stripe says, whatever the order of the events."""
    paid = snapshot.invoice_status == "paid"
    paid_until = snapshot.current_period_end + ENTITLEMENT_MARGIN if paid else None
    first_purchase = snapshot.invoice_billing_reason == _FIRST_PURCHASE
    if snapshot.disputed:
        return SeatState(SeatStatus.DISPUTED, paid_until, BlockReason.DISPUTE)
    if snapshot.subscription_status in _ENDED:
        if snapshot.subscription_status == "incomplete_expired":
            until = None  # never paid
        elif snapshot.cancellation_reason in (None, "cancellation_requested"):
            until = snapshot.current_period_end  # the year paid is kept
        else:
            until = snapshot.canceled_at  # cancelled for not paying: at once
        return SeatState(SeatStatus.CANCELED, until)
    if paid:
        return SeatState(SeatStatus.ACTIVE, paid_until)
    intent = snapshot.payment_intent_status
    if intent == "requires_action":
        if first_purchase:
            return SeatState(SeatStatus.PENDING, None)
        return SeatState(SeatStatus.ACTION_REQUIRED, None, BlockReason.ACTION_REQUIRED)
    failed = (
        intent in _FAILED_INTENTS
        or snapshot.invoice_status == "uncollectible"
        or snapshot.subscription_status in ("past_due", "unpaid")
    )
    if failed and not first_purchase:
        return SeatState(SeatStatus.PAYMENT_FAILED, None, BlockReason.PAYMENT_FAILED)
    # First purchase not paid yet, or a payment in progress: card only, so no
    # provisional right (decision 9.0, 5 and 6).
    return SeatState(SeatStatus.PENDING, None)


# --- end of the beta -----------------------------------------------------------------


def beta_ends_at(launch_at: datetime | None) -> datetime | None:
    """The end of the beta window: BETA_WINDOW_DAYS after the launch. The database
    derives `billing_setting.beta_ends_at` the same way."""
    return launch_at + timedelta(days=BETA_WINDOW_DAYS) if launch_at is not None else None


def _window_until(
    now: datetime, launch_at: datetime, beta_until: datetime | None, beta: bool
) -> datetime | None:
    """The end of the beta window of an account at `now`, or None if it has none now."""
    window_end = beta_ends_at(launch_at)
    assert window_end is not None
    if beta_until is None and not beta:
        return None
    until = min(beta_until or window_end, window_end)
    return until if launch_at <= now < until else None


def first_charge_at(
    now: datetime,
    launch_at: datetime | None,
    *,
    beta_until: datetime | None,
    beta: bool = False,
) -> datetime | None:
    """When a seat bought at `now` is first charged: None for at once (every account
    created from the launch, and a beta account after its window), or the end of the
    beta window of a beta account (its Stripe `trial_end`).

    `beta_until`: the latest end of the beta entitlements of the account (None if it has
    none; `beta=True` for one without an end, which then ends with the window). Never
    later than the window; stretched to MIN_TRIAL_AHEAD in its last hours, which Checkout
    needs. ValueError before the launch: nothing is charged in the beta."""
    if launch_at is None or now < launch_at:
        raise ValueError("beta: nothing is charged before the launch")
    until = _window_until(now, launch_at, beta_until, beta)
    if until is None:
        return None
    return max(until, now + MIN_TRIAL_AHEAD)


def beta_window_days_left(
    now: datetime,
    launch_at: datetime | None,
    *,
    beta_until: datetime | None,
    beta: bool = False,
) -> int | None:
    """`pricing.trial_days` of GET /me: whole days (rounded up) a beta account still has
    before its first charge, or None (beta, an account created from the launch, or a
    window that has ended)."""
    if launch_at is None or now < launch_at:
        return None
    until = _window_until(now, launch_at, beta_until, beta)
    if until is None:
        return None
    return math.ceil((until - now) / timedelta(days=1))


class EnforceAction(StrEnum):
    KEEP = "keep"
    TO_GRATIS = "to_gratis"
    DEACTIVATE = "deactivate"


@dataclass(frozen=True)
class BetaProfile:
    """An active profile of the account at `beta_ends_at`. `has_seat`: a paid seat of
    its plan covers it."""

    location_id: str
    plan: Plan
    active_since: datetime
    gbp_location: str
    has_seat: bool = False


def enforce_after_beta(
    profiles: Iterable[BetaProfile], *, gratis_elsewhere: Collection[str]
) -> dict[str, EnforceAction]:
    """What `billing_enforce` does to each active profile at the end of the beta
    (section 4.6). Paid profiles without a seat: the oldest one moves to Gratis if the
    account has its free profile left and that Google profile is not Gratis in another
    account (`gratis_elsewhere`, by gbp_location); if it is, the next one tries. The
    rest are deactivated. There is no block: nothing was left unpaid."""
    active = list(profiles)
    actions = {p.location_id: EnforceAction.KEEP for p in active}
    free_left = FREE_PER_ACCOUNT - sum(1 for p in active if p.plan is Plan.GRATIS)
    unpaid = sorted(
        (p for p in active if p.plan in PAID_PLANS and not p.has_seat),
        key=lambda p: (p.active_since, p.location_id),
    )
    for profile in unpaid:
        if free_left > 0 and profile.gbp_location not in gratis_elsewhere:
            actions[profile.location_id] = EnforceAction.TO_GRATIS
            free_left -= 1
        else:
            actions[profile.location_id] = EnforceAction.DEACTIVATE
    return actions


# --- DMARC of a leave asked by email -------------------------------------------------


@dataclass(frozen=True)
class DmarcCheck:
    """`passed` only with `reason == "ok"`. The address is for the comparison with the
    account, which a database function makes without showing it; the evidence kept is
    the result, the domain and the hash of the Message-ID."""

    passed: bool
    reason: str
    from_address: str | None = None
    from_domain: str | None = None
    message_id_sha256: str | None = None


_COMMENT = re.compile(r"\([^()]*\)")


def _headers(raw: str) -> list[tuple[str, str]]:
    """The headers of a raw message, in order, unfolded. The body is left out."""
    head = re.split(r"\r?\n\r?\n", raw, maxsplit=1)[0]
    headers: list[tuple[str, str]] = []
    for line in re.split(r"\r?\n", head):
        if line[:1] in (" ", "\t") and headers:
            name, value = headers[-1]
            headers[-1] = (name, f"{value} {line.strip()}")
        elif ":" in line:
            name, value = line.split(":", 1)
            headers.append((name.strip().lower(), value.strip()))
    return headers


def _strip_comments(value: str) -> str:
    previous = None
    while previous != value:
        previous, value = value, _COMMENT.sub(" ", value)
    return value


def _fail(reason: str) -> DmarcCheck:
    return DmarcCheck(False, reason)


def parse_dmarc(raw_message: str, authserv_id: str) -> DmarcCheck:
    """Whether a message pasted whole proves that it comes from its `From:` (section 6.4).

    Only the first `Authentication-Results` counts: our mail server puts it on top, and
    any one below may be written by whoever sent the message. It must carry our
    `authserv_id` (FICHAITOR_ADMIN_AUTHSERV_ID), exactly one `dmarc=pass` and
    `header.from` equal to the domain of the only address of `From:`. `fail`, `none`,
    `bestguesspass` and the absence of a result are rejected; SPF or DKIM alone are not
    enough."""
    headers = _headers(raw_message or "")
    results = [value for name, value in headers if name == "authentication-results"]
    if not results:
        return _fail("no_authentication_results")
    resinfos = [part.strip() for part in _strip_comments(results[0]).split(";")]
    server = resinfos[0].split()
    if not server or server[0].lower() != authserv_id.strip().lower():
        return _fail("wrong_authserv_id")
    dmarc = [r for r in resinfos[1:] if r.lower().startswith("dmarc=")]
    if not dmarc:
        return _fail("no_dmarc")
    if len(dmarc) > 1:
        return _fail("ambiguous_dmarc")
    tokens = dmarc[0].split()
    if tokens[0].split("=", 1)[1].lower() != "pass":
        return _fail("dmarc_not_pass")
    header_from = next(
        (t.split("=", 1)[1].lower() for t in tokens[1:] if t.lower().startswith("header.from=")),
        None,
    )
    senders = [value for name, value in headers if name == "from"]
    addresses = [a for _, a in getaddresses(senders) if a]
    if len(senders) != 1 or len(addresses) != 1 or addresses[0].count("@") != 1:
        return _fail("no_from")
    address = addresses[0].strip().lower()
    domain = address.rsplit("@", 1)[1]
    if header_from != domain:
        return DmarcCheck(False, "domain_mismatch", address, domain)
    message_id = next((value for name, value in headers if name == "message-id"), "")
    digest = hashlib.sha256(message_id.strip().encode()).hexdigest()
    return DmarcCheck(True, "ok", address, domain, digest)
