"""Create in Stripe what Fichaitor charges with (docs/admin-facturacion.md, 2.6 and 3.1).

Idempotent: it looks each thing up first and only creates what is missing.

- Two products, «Fichaitor Pro» and «Fichaitor Pro WhatsApp» (Pro+ on screen), found by
  their metadata `fichaitor_plan`.
- Four yearly prices in euros, without VAT (`tax_behavior = exclusive`), found by their
  `lookup_key` (`app.domain.billing.PriceKey`): the code never keeps a `price_…`. Their
  amounts are those of `app.domain.plans` (35,00 and 33,25; 59,00 and 56,05). A price of
  the right key with another amount is reported and left alone: Stripe prices do not
  change, and moving a `lookup_key` is a decision of the owner.
- Four Tax Rates, found by their metadata `fichaitor_tax`: iva_21 (21 %), no_sujeto
  (0 %, Canarias, Ceuta and Melilla), isp_ue (0 %, reverse charge in the EU) and
  extracomunitario (0 %, a business outside the EU: not subject by the place of supply).
  No Stripe Tax. Run again on a catalogue made before, it only adds the missing one.

Test mode only, unless asked otherwise on purpose: a live key (`sk_live_`, `rk_live_`)
needs `--live` and FICHAITOR_STRIPE_LIVEMODE=true. The key is read from
FICHAITOR_STRIPE_SECRET_KEY and never printed.

    FICHAITOR_STRIPE_SECRET_KEY=rk_test_... uv run python scripts/stripe_bootstrap.py
    uv run python scripts/stripe_bootstrap.py --dry-run     # what it would create
"""

import argparse
import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.domain.billing import (  # noqa: E402
    PRODUCT_NAME,
    TAX_PERCENT,
    PriceKey,
    TaxTreatment,
    price_cents,
    price_plan,
)
from app.domain.plans import CURRENCY  # noqa: E402

PRODUCT_METADATA = "fichaitor_plan"
TAX_METADATA = "fichaitor_tax"  # the same as app/adapters/stripe/real.py
TAX_NAMES: Mapping[TaxTreatment, tuple[str, str]] = {
    TaxTreatment.IVA_21: ("IVA", "IVA 21 %"),
    TaxTreatment.NO_SUJETO: ("No sujeto", "Operación no sujeta a IVA (Canarias, Ceuta y Melilla)"),
    TaxTreatment.ISP_UE: ("ISP UE", "Inversión del sujeto pasivo (UE)"),
    TaxTreatment.EXTRACOMUNITARIO: (
        "No sujeto (extracomunitario)",
        "Operación no sujeta al IVA español por reglas de localización (art. 69 Ley 37/1992)",
    ),
}


@dataclass(frozen=True)
class Step:
    """One thing to create, or a problem to report."""

    kind: str  # product, price, tax_rate or conflict
    key: str
    params: Mapping[str, Any]


class StripeCatalog(Protocol):
    """What the script needs of Stripe. `StripeClientCatalog` is the real one."""

    def products(self) -> list[dict]: ...
    def prices(self, lookup_keys: list[str]) -> list[dict]: ...
    def tax_rates(self) -> list[dict]: ...
    def create_product(self, params: Mapping[str, Any], idempotency_key: str) -> dict: ...
    def create_price(self, params: Mapping[str, Any], idempotency_key: str) -> dict: ...
    def create_tax_rate(self, params: Mapping[str, Any], idempotency_key: str) -> dict: ...


def plan_products(existing: Iterable[dict]) -> list[Step]:
    found = {
        (p.get("metadata") or {}).get(PRODUCT_METADATA) for p in existing if p.get("active", True)
    }
    steps = []
    for plan, name in PRODUCT_NAME.items():
        if plan.value not in found:
            steps.append(
                Step(
                    "product",
                    plan.value,
                    {"name": name, "metadata": {PRODUCT_METADATA: plan.value}},
                )
            )
    return steps


def plan_prices(existing: Iterable[dict], product_ids: Mapping[str, str]) -> list[Step]:
    """`product_ids`: plan value -> prod_… (the ones just created are filled in later)."""
    found = {p.get("lookup_key"): p for p in existing if p.get("active", True)}
    steps = []
    for key in PriceKey:
        amount = price_cents(key)
        current = found.get(key.value)
        if current is not None:
            recurring = current.get("recurring") or {}
            if (
                current.get("unit_amount") != amount
                or str(current.get("currency", "")).lower() != CURRENCY.lower()
                or recurring.get("interval") != "year"
            ):
                steps.append(Step("conflict", key.value, {"expected": amount}))
            continue
        plan = price_plan(key).value
        steps.append(
            Step(
                "price",
                key.value,
                {
                    "product": product_ids.get(plan, f"<{plan}>"),
                    "lookup_key": key.value,
                    "unit_amount": amount,
                    "currency": CURRENCY.lower(),
                    "recurring": {"interval": "year", "interval_count": 1},
                    "tax_behavior": "exclusive",
                    "nickname": key.value,
                },
            )
        )
    return steps


def plan_tax_rates(existing: Iterable[dict]) -> list[Step]:
    found = {(t.get("metadata") or {}).get(TAX_METADATA) for t in existing if t.get("active", True)}
    steps = []
    for treatment in TaxTreatment:
        if treatment.value in found:
            continue
        name, description = TAX_NAMES[treatment]
        steps.append(
            Step(
                "tax_rate",
                treatment.value,
                {
                    "display_name": name,
                    "description": description,
                    "percentage": TAX_PERCENT[treatment],
                    "inclusive": False,
                    "country": "ES",
                    "metadata": {TAX_METADATA: treatment.value},
                },
            )
        )
    return steps


def bootstrap(catalog: StripeCatalog, *, dry_run: bool = False) -> list[Step]:
    """Create what is missing. Returns every step, created or (dry run) to create."""
    done: list[Step] = []
    products = catalog.products()
    product_ids = {
        (p.get("metadata") or {}).get(PRODUCT_METADATA): p["id"]
        for p in products
        if p.get("active", True)
    }
    for step in plan_products(products):
        done.append(step)
        if not dry_run:
            created = catalog.create_product(step.params, f"fichaitor:product:{step.key}")
            product_ids[step.key] = created["id"]
    for step in plan_prices(catalog.prices([k.value for k in PriceKey]), product_ids):
        done.append(step)
        if step.kind == "price" and not dry_run:
            catalog.create_price(step.params, f"fichaitor:price:{step.key}")
    for step in plan_tax_rates(catalog.tax_rates()):
        done.append(step)
        if not dry_run:
            catalog.create_tax_rate(step.params, f"fichaitor:tax_rate:{step.key}")
    return done


class StripeClientCatalog:
    def __init__(self, api_key: str) -> None:
        import stripe

        from app.adapters.stripe.real import STRIPE_API_VERSION

        self._client = stripe.StripeClient(api_key, stripe_version=STRIPE_API_VERSION)

    def products(self) -> list[dict]:
        return [p.to_dict() for p in self._client.v1.products.list(params={"limit": 100}).data]

    def prices(self, lookup_keys: list[str]) -> list[dict]:
        found = self._client.v1.prices.list(
            params={"lookup_keys": lookup_keys, "active": True, "limit": 100}
        )
        return [p.to_dict() for p in found.data]

    def tax_rates(self) -> list[dict]:
        found = self._client.v1.tax_rates.list(params={"active": True, "limit": 100})
        return [t.to_dict() for t in found.data]

    def create_product(self, params: Mapping[str, Any], idempotency_key: str) -> dict:
        return self._client.v1.products.create(
            params=dict(params),  # type: ignore[arg-type]
            options={"idempotency_key": idempotency_key},
        ).to_dict()

    def create_price(self, params: Mapping[str, Any], idempotency_key: str) -> dict:
        return self._client.v1.prices.create(
            params=dict(params),  # type: ignore[arg-type]
            options={"idempotency_key": idempotency_key},
        ).to_dict()

    def create_tax_rate(self, params: Mapping[str, Any], idempotency_key: str) -> dict:
        return self._client.v1.tax_rates.create(
            params=dict(params),  # type: ignore[arg-type]
            options={"idempotency_key": idempotency_key},
        ).to_dict()


def check_key(api_key: str, live: bool, livemode_env: str) -> str | None:
    """Why this key may not be used, or None."""
    if not api_key.startswith(("sk_test_", "rk_test_", "sk_live_", "rk_live_")):
        return "FICHAITOR_STRIPE_SECRET_KEY no es una clave de Stripe"
    is_live = "_live_" in api_key
    if is_live and not (live and livemode_env.lower() == "true"):
        return "clave live: hace falta --live y FICHAITOR_STRIPE_LIVEMODE=true"
    if live and not is_live:
        return "--live con una clave de prueba"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="solo dice qué crearía")
    parser.add_argument("--live", action="store_true", help="modo real (solo el propietario)")
    args = parser.parse_args(argv)
    api_key = os.environ.get("FICHAITOR_STRIPE_SECRET_KEY", "")
    problem = check_key(api_key, args.live, os.environ.get("FICHAITOR_STRIPE_LIVEMODE", ""))
    if problem:
        print(f"No se hace nada: {problem}.", file=sys.stderr)
        return 2
    steps = bootstrap(StripeClientCatalog(api_key), dry_run=args.dry_run)
    conflicts = [s for s in steps if s.kind == "conflict"]
    for step in steps:
        verb = "conflicto" if step.kind == "conflict" else ("crearía" if args.dry_run else "creado")
        print(f"{verb}: {step.kind} {step.key}")
    if not steps:
        print("Nada que crear: el catálogo ya está completo.")
    return 1 if conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
