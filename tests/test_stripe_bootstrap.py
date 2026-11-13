"""scripts/stripe_bootstrap.py: the catalogue Fichaitor charges with, created once and only
in test mode unless asked on purpose. Against an in-memory catalogue: never Stripe."""

import importlib.util
from pathlib import Path

import pytest

from app.adapters.stripe.real import TAX_RATE_METADATA

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "stripe_bootstrap.py"
spec = importlib.util.spec_from_file_location("stripe_bootstrap", SCRIPT)
bootstrap_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap_module)


class Catalog:
    def __init__(self) -> None:
        self.products_: list[dict] = []
        self.prices_: list[dict] = []
        self.rates: list[dict] = []
        self.keys: list[str] = []

    def products(self):
        return list(self.products_)

    def prices(self, lookup_keys):
        return [p for p in self.prices_ if p["lookup_key"] in lookup_keys]

    def tax_rates(self):
        return list(self.rates)

    def create_product(self, params, idempotency_key):
        self.keys.append(idempotency_key)
        created = {"id": f"prod_{len(self.products_)}", "active": True, **params}
        self.products_.append(created)
        return created

    def create_price(self, params, idempotency_key):
        self.keys.append(idempotency_key)
        created = {"id": f"price_{len(self.prices_)}", "active": True, **params}
        self.prices_.append(created)
        return created

    def create_tax_rate(self, params, idempotency_key):
        self.keys.append(idempotency_key)
        created = {"id": f"txr_{len(self.rates)}", "active": True, **params}
        self.rates.append(created)
        return created


def test_it_creates_the_four_yearly_prices_and_four_tax_rates_once():
    catalog = Catalog()
    steps = bootstrap_module.bootstrap(catalog)
    assert [s.kind for s in steps].count("product") == 2
    prices = {p["lookup_key"]: p for p in catalog.prices_}
    assert {k: p["unit_amount"] for k, p in prices.items()} == {
        "pro_first_yearly": 3500,
        "pro_additional_yearly": 3325,
        "whatsapp_first_yearly": 5900,
        "whatsapp_additional_yearly": 5605,
    }
    for price in prices.values():
        assert price["currency"] == "eur" and price["recurring"]["interval"] == "year"
        assert price["tax_behavior"] == "exclusive"
        assert price["product"].startswith("prod_")
    rates = {r["metadata"][TAX_RATE_METADATA]: r["percentage"] for r in catalog.rates}
    assert rates == {"iva_21": 21, "no_sujeto": 0, "isp_ue": 0, "extracomunitario": 0}
    assert all(not r["inclusive"] for r in catalog.rates)
    # Idempotent: a second run creates nothing.
    assert bootstrap_module.bootstrap(catalog) == []
    assert len(catalog.keys) == len(set(catalog.keys)) == 10


def test_a_price_with_another_amount_is_reported_not_changed():
    catalog = Catalog()
    bootstrap_module.bootstrap(catalog)
    catalog.prices_[0]["unit_amount"] = 3000
    steps = bootstrap_module.bootstrap(catalog)
    assert [(s.kind, s.key) for s in steps] == [("conflict", catalog.prices_[0]["lookup_key"])]


def test_the_dry_run_creates_nothing():
    catalog = Catalog()
    steps = bootstrap_module.bootstrap(catalog, dry_run=True)
    assert len(steps) == 10 and catalog.keys == []


def test_a_catalogue_made_before_only_gets_the_rate_outside_the_eu():
    catalog = Catalog()
    bootstrap_module.bootstrap(catalog)
    catalog.rates = [
        r for r in catalog.rates if r["metadata"][TAX_RATE_METADATA] != "extracomunitario"
    ]
    steps = bootstrap_module.bootstrap(catalog)
    assert [(s.kind, s.key) for s in steps] == [("tax_rate", "extracomunitario")]
    assert steps[0].params["display_name"] == "No sujeto (extracomunitario)"
    assert steps[0].params["percentage"] == 0


@pytest.mark.parametrize(
    ("key", "live", "env", "ok"),
    [
        ("rk_test_abc", False, "", True),
        ("sk_test_abc", False, "", True),
        ("rk_live_abc", False, "true", False),
        ("rk_live_abc", True, "", False),
        ("rk_live_abc", True, "true", True),
        ("rk_test_abc", True, "true", False),
        ("pk_test_abc", False, "", False),
        ("", False, "", False),
    ],
)
def test_live_keys_need_both_flags(key, live, env, ok):
    assert (bootstrap_module.check_key(key, live, env) is None) is ok


def test_without_a_key_it_does_nothing(monkeypatch, capsys):
    monkeypatch.delenv("FICHAITOR_STRIPE_SECRET_KEY", raising=False)
    assert bootstrap_module.main([]) == 2
    assert "No se hace nada" in capsys.readouterr().err
