"""The invoicing port (docs/admin-facturacion.md, 4.3, 4.5 and 9.0.1): the same contract
for the fake and for the JSON-2 adapter against a tiny Odoo in memory; then what only
the real adapter does: the status codes, the cut halfway, the total, the secrets."""

import asyncio
import inspect
import logging
from dataclasses import replace
from datetime import date

import httpx
import pytest

from app.adapters.odoo_invoicing.fake import FakeInvoicing
from app.adapters.odoo_invoicing.real import (
    OdooInvoicing,
    OdooSettings,
    credit_base,
    to_cents,
)
from app.domain.billing import PriceKey, TaxTreatment, Territory, invoice_lines
from app.domain.plans import Plan
from app.ports.invoicing import (
    CreditNoteRequest,
    InvoiceCustomer,
    InvoiceRequest,
    Invoicing,
    InvoicingError,
    InvoicingRejected,
    InvoicingUnavailable,
    IssuedDocument,
)
from tests import fake_odoo_json2 as odoo

DAY = date(2027, 3, 14)


def settings(**changes: object) -> OdooSettings:
    values: dict[str, object] = {
        "url": odoo.URL,
        "db": odoo.DB,
        "api_key": odoo.API_KEY,
        "sale_journal_id": odoo.SALE_JOURNAL,
        "payment_journal_id": odoo.STRIPE_JOURNAL,
        "taxes": {
            TaxTreatment.IVA_21: 101,
            TaxTreatment.NO_SUJETO: 102,
            TaxTreatment.ISP_UE: 103,
            TaxTreatment.EXTRACOMUNITARIO: 104,
        },
        "products": {Plan.PRO: 201, Plan.PRO_WHATSAPP: 202},
        "fiscal_positions": {"nacional": 401, "canarias": 402, "extracomunitario": 403},
    }
    values.update(changes)
    return OdooSettings(**values)  # type: ignore[arg-type]


def real(server: odoo.FakeOdoo, **changes: object) -> OdooInvoicing:
    client = httpx.AsyncClient(transport=server.transport())
    return OdooInvoicing(settings(**changes), client)


CUSTOMER = InvoiceCustomer(
    stripe_customer_id="cus_A1",
    legal_name="Bar La Parra S.L.",
    tax_id="B12345674",
    country="ES",
    postal_code="18001",
    city="Granada",
    line1="Calle Real 1",
    email="facturas@laparra.example",
)


def request(
    stripe_id: str = "in_1",
    key: PriceKey = PriceKey.PRO_FIRST,
    treatment: TaxTreatment = TaxTreatment.IVA_21,
    territory: Territory = Territory.PENINSULA_BALEARES,
    paid: int | None = None,
) -> InvoiceRequest:
    draft = invoice_lines(key, "Bar La Parra", DAY, date(2028, 3, 14), treatment, territory)
    return InvoiceRequest(
        stripe_invoice_id=stripe_id,
        customer=CUSTOMER,
        draft=draft,
        treatment=treatment,
        invoice_date=DAY,
        operation_date=DAY,
        paid_cents=draft.amount_total if paid is None else paid,
        payment_reference="ch_1",
    )


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


# --- the contract, for both ------------------------------------------------------------


def make_fake() -> tuple[Invoicing, object]:
    fake = FakeInvoicing()
    return fake, fake


def make_real() -> tuple[Invoicing, object]:
    server = odoo.FakeOdoo()
    return real(server), server


BOTH = pytest.mark.parametrize("make", [make_fake, make_real], ids=["fake", "json2"])


def test_both_have_the_methods_of_the_port() -> None:
    wanted = {n for n, _ in inspect.getmembers(Invoicing) if not n.startswith("_")}
    for cls in (FakeInvoicing, OdooInvoicing):
        for name in wanted:
            port = inspect.signature(getattr(Invoicing, name))
            mine = inspect.signature(getattr(cls, name))
            assert list(port.parameters) == list(mine.parameters), (cls, name)


@BOTH
def test_an_invoice_is_numbered_paid_and_checked(make) -> None:  # type: ignore[no-untyped-def]
    port, _ = make()
    doc = run(port.issue_invoice(request()))
    assert doc.kind == "invoice" and doc.created
    assert doc.number == "FTR/2027/00001"
    assert doc.state == "posted" and doc.paid
    assert (doc.amount_untaxed, doc.amount_tax, doc.amount_total) == (3500, 735, 4235)
    assert doc.total_ok


@BOTH
def test_the_same_stripe_invoice_twice_is_one_document(make) -> None:  # type: ignore[no-untyped-def]
    port, _ = make()
    first = run(port.issue_invoice(request()))
    again = run(port.issue_invoice(request()))
    assert again.move_id == first.move_id and again.number == first.number
    assert not again.created
    other = run(port.issue_invoice(request("in_2", PriceKey.PRO_ADDITIONAL)))
    assert other.number == "FTR/2027/00002"


@BOTH
def test_lines_that_do_not_add_up_to_the_charge_never_reach_odoo(make) -> None:  # type: ignore[no-untyped-def]
    port, _ = make()
    with pytest.raises(InvoicingRejected) as caught:
        run(port.issue_invoice(request(paid=4234)))
    assert caught.value.code == "draft_total_mismatch" and not caught.value.retryable
    assert run(port.find("in_1")) is None


@BOTH
def test_canarias_has_no_vat(make) -> None:  # type: ignore[no-untyped-def]
    port, _ = make()
    doc = run(
        port.issue_invoice(request(treatment=TaxTreatment.NO_SUJETO, territory=Territory.CANARIAS))
    )
    assert (doc.amount_tax, doc.amount_total) == (0, 3500) and doc.total_ok


@BOTH
def test_a_business_outside_the_eu_has_no_vat(make) -> None:  # type: ignore[no-untyped-def]
    port, _ = make()
    doc = run(
        port.issue_invoice(
            request(treatment=TaxTreatment.EXTRACOMUNITARIO, territory=Territory.FUERA_UE)
        )
    )
    assert (doc.amount_tax, doc.amount_total) == (0, 3500) and doc.total_ok


def test_what_goes_to_odoo_for_a_business_outside_the_eu() -> None:
    server = odoo.FakeOdoo()
    world = InvoiceCustomer(
        "cus_W1", "Taco Loco LLC", "12-3456789", "US", None, "Austin", "1 Main St"
    )
    req = request(treatment=TaxTreatment.EXTRACOMUNITARIO, territory=Territory.FUERA_UE)
    run(real(server).issue_invoice(replace(req, customer=world)))
    move = server.moves("out_invoice")[0]
    assert move["fiscal_position_id"] == 403
    assert "reglas de localización (art. 69 Ley 37/1992)" in move["narration"]
    (line,) = server.tables["account.move.line"].values()
    assert line["tax_ids"] == [104]
    partner = next(iter(server.tables["res.partner"].values()))
    # The tax id as the customer's country writes it; no postal code is no zip.
    assert partner["vat"] == "12-3456789" and partner["zip"] is False
    assert partner["country_id"] == odoo.COUNTRIES["US"]


@BOTH
def test_a_credit_note_needs_its_invoice_and_is_idempotent(make) -> None:  # type: ignore[no-untyped-def]
    port, _ = make()
    credit = CreditNoteRequest("re_1", "in_1", 4235, "error_cobro", DAY)
    with pytest.raises(InvoicingUnavailable) as caught:
        run(port.issue_credit_note(credit))
    assert caught.value.retryable  # the invoice is still on its way
    run(port.issue_invoice(request()))
    doc = run(port.issue_credit_note(credit))
    assert doc.kind == "credit_note" and doc.number == "RFTR/2027/00001"
    assert (doc.amount_untaxed, doc.amount_total) == (3500, 4235) and doc.paid
    again = run(port.issue_credit_note(credit))
    assert again.move_id == doc.move_id and not again.created


@BOTH
def test_a_partial_credit_note_adds_up_exactly(make) -> None:  # type: ignore[no-untyped-def]
    port, _ = make()
    run(port.issue_invoice(request()))
    doc = run(port.issue_credit_note(CreditNoteRequest("re_2", "in_1", 1210, "x", DAY)))
    assert (doc.amount_untaxed, doc.amount_tax, doc.amount_total) == (1000, 210, 1210)
    with pytest.raises(InvoicingRejected):
        run(port.issue_credit_note(CreditNoteRequest("re_3", "in_1", 9999, "x", DAY)))


@BOTH
def test_find_and_issued_since(make) -> None:  # type: ignore[no-untyped-def]
    port, _ = make()
    doc = run(port.issue_invoice(request()))
    found = run(port.find("in_1"))
    assert found is not None and found.number == doc.number and found.expected_total is None
    assert run(port.find("in_nope")) is None
    assert [d.stripe_id for d in run(port.issued_since(DAY))] == ["in_1"]
    assert run(port.issued_since(date(2027, 3, 15))) == []


@BOTH
def test_pdf_and_setup(make) -> None:  # type: ignore[no-untyped-def]
    port, _ = make()
    doc = run(port.issue_invoice(request()))
    assert run(port.pdf(doc.move_id)).startswith(b"%PDF")
    assert run(port.setup_problems()) == []


# --- only the JSON-2 adapter -----------------------------------------------------------


def test_a_cut_after_posting_resumes_without_a_second_invoice() -> None:
    server = odoo.FakeOdoo()
    port = real(server)
    server.cut_after.add(("account.move", "action_post"))
    with pytest.raises(InvoicingUnavailable):
        run(port.issue_invoice(request()))
    assert server.moves()[0]["state"] == "posted" and server.payments() == []
    doc = run(port.issue_invoice(request()))
    assert len(server.moves()) == 1 and len(server.payments()) == 1
    assert doc.number == "FTR/2027/00001" and doc.paid and not doc.created


def test_a_cut_after_creating_posts_the_draft_it_left() -> None:
    server = odoo.FakeOdoo()
    port = real(server)
    server.cut_after.add(("account.move", "create"))
    with pytest.raises(InvoicingUnavailable):
        run(port.issue_invoice(request()))
    assert server.moves()[0]["state"] == "draft"
    doc = run(port.issue_invoice(request()))
    assert len(server.moves()) == 1 and doc.number == "FTR/2027/00001" and doc.paid


def test_a_cut_after_paying_does_not_pay_twice() -> None:
    server = odoo.FakeOdoo()
    port = real(server)
    server.cut_after.add(("account.payment.register", "action_create_payments"))
    with pytest.raises(InvoicingUnavailable):
        run(port.issue_invoice(request()))
    doc = run(port.issue_invoice(request()))
    assert len(server.moves()) == 1 and len(server.payments()) == 1 and doc.paid


def test_what_goes_to_odoo() -> None:
    server = odoo.FakeOdoo()
    run(real(server).issue_invoice(request()))
    move = server.moves("out_invoice")[0]
    assert move["ref"] == "stripe:in_1"
    assert move["journal_id"] == odoo.SALE_JOURNAL
    assert move["fiscal_position_id"] == 401
    assert move["invoice_date"] == "2027-03-14"
    partner = next(iter(server.tables["res.partner"].values()))
    assert partner["vat"] == "ESB12345674" and partner["ref"] == "stripe:cus_A1"
    assert partner["is_company"] and partner["country_id"] == odoo.COUNTRIES["ES"]
    payment = server.payments()[0]
    assert payment["journal_id"] == odoo.STRIPE_JOURNAL and payment["memo"] == "ch_1"
    request_seen = server.requests[0]
    assert request_seen.url.path.startswith("/json/2/")
    assert request_seen.headers["x-odoo-database"] == odoo.DB


def test_the_partner_is_reused_by_stripe_customer_and_by_nif() -> None:
    server = odoo.FakeOdoo()
    port = real(server)
    run(port.issue_invoice(request("in_1")))
    run(port.issue_invoice(request("in_2")))
    assert len(server.tables["res.partner"]) == 1
    server.tables["res.partner"][1] = {"id": 1, "vat": "B99999999", "is_company": True}
    other = InvoiceCustomer("cus_B2", "Otra S.L.", "B99999999", "ES", "18001", "Granada", "C/ 2")
    req = request("in_3")
    run(port.issue_invoice(replace(req, customer=other)))
    assert len(server.tables["res.partner"]) == 2  # the one with that NIF, not a third


def test_a_total_odoo_adds_up_differently_goes_to_review_unpaid() -> None:
    server = odoo.FakeOdoo()
    server.tax_rates[101] = 20  # a tax set up differently in Odoo
    port = real(server)
    doc = run(port.issue_invoice(request()))
    assert not doc.total_ok and not doc.paid
    assert (doc.amount_total, doc.expected_total) == (4200, 4235)
    assert server.payments() == []
    again = run(port.issue_invoice(request()))  # the retry neither pays nor duplicates
    assert again.move_id == doc.move_id and not again.total_ok and server.payments() == []


@pytest.mark.parametrize("status", [500, 502, 503, 504, 429, 408])
def test_5xx_429_and_408_are_temporary(status: int) -> None:
    server = odoo.FakeOdoo()
    server.fail[("account.move", "search_read")] = [status]
    with pytest.raises(InvoicingUnavailable) as caught:
        run(real(server).issue_invoice(request()))
    assert caught.value.retryable and caught.value.status == status


@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout("t"), httpx.ConnectTimeout("t"), httpx.ConnectError("x")]
)
def test_time_outs_and_network_errors_are_temporary(error: Exception) -> None:
    server = odoo.FakeOdoo()
    server.fail[("account.move", "search_read")] = [error]
    with pytest.raises(InvoicingUnavailable) as caught:
        run(real(server).issue_invoice(request()))
    assert caught.value.retryable


@pytest.mark.parametrize("status", [400, 403, 404, 422])
def test_a_4xx_is_permanent(status: int) -> None:
    server = odoo.FakeOdoo()
    server.fail[("account.move", "create")] = [status]
    with pytest.raises(InvoicingRejected) as caught:
        run(real(server).issue_invoice(request()))
    assert not caught.value.retryable and caught.value.status == status
    assert caught.value.code == "odoo.exceptions.ValidationError"


def test_a_wrong_key_is_permanent() -> None:
    server = odoo.FakeOdoo()
    port = real(server, api_key="another-key")
    with pytest.raises(InvoicingRejected) as caught:
        run(port.find("in_1"))
    assert caught.value.status == 401


def test_no_secret_in_errors_logs_or_repr(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    server = odoo.FakeOdoo()
    port = real(server)
    errors: list[BaseException] = []
    for status in (500, 422):
        server.fail[("account.move", "search_read")] = [status]
        with pytest.raises(InvoicingError) as caught:
            run(port.issue_invoice(request()))
        errors.append(caught.value)
    server.fail[("account.move", "search_read")] = [httpx.ReadTimeout(odoo.API_KEY)]
    with pytest.raises(InvoicingUnavailable) as timeout:
        run(port.issue_invoice(request()))
    errors.append(timeout.value)
    doc = run(port.issue_invoice(request()))
    assert run(port.pdf(doc.move_id)).startswith(b"%PDF")  # through the portal link
    texts = [repr(port), repr(settings()), caplog.text]
    texts += [str(e) + repr(e) + repr(e.__cause__) for e in errors]
    for text in texts:
        assert odoo.API_KEY not in text
        assert "B12345674" not in text  # nor the customer's data Odoo echoed
        assert "secret-token" not in text  # nor the access token of the portal link


def test_customer_data_stays_out_of_repr() -> None:
    assert "Bar La Parra" not in repr(CUSTOMER) and "B12345674" not in repr(CUSTOMER)


def test_pdf_from_the_stored_report_or_the_portal() -> None:
    server = odoo.FakeOdoo()
    port = real(server)
    doc = run(port.issue_invoice(request()))
    assert run(port.pdf(doc.move_id)) == odoo.PDF  # rendered through the portal
    assert server.requests[-1].method == "GET"
    server.stored_pdf(doc.move_id)
    calls = len(server.requests)
    assert run(port.pdf(doc.move_id)) == odoo.PDF
    assert all(r.method == "POST" for r in server.requests[calls:])


def test_setup_problems_finds_a_wrong_setup() -> None:
    server = odoo.FakeOdoo()
    server.rounding = "round_globally"
    port = real(
        server,
        taxes={
            TaxTreatment.IVA_21: 102,
            TaxTreatment.NO_SUJETO: 102,
            TaxTreatment.ISP_UE: 999,
            TaxTreatment.EXTRACOMUNITARIO: 104,
        },
        payment_journal_id=odoo.SALE_JOURNAL,
        products={Plan.PRO: 201, Plan.PRO_WHATSAPP: 299},
    )
    problems = run(port.setup_problems())
    text = "\n".join(problems)
    assert "FICHAITOR_ODOO_TAX_IVA_21" in text
    assert "FICHAITOR_ODOO_TAX_ISP_UE" in text
    assert "FICHAITOR_ODOO_PAYMENT_JOURNAL_ID" in text
    assert "FICHAITOR_ODOO_PRODUCT_PRO_WHATSAPP" in text
    assert "por línea" in text
    assert "FICHAITOR_ODOO_TAX_NO_SUJETO" not in text


def test_setup_problems_never_raises() -> None:
    server = odoo.FakeOdoo()
    server.fail[("account.tax", "search_read")] = [503]
    assert run(real(server).setup_problems())


def test_settings_from_env() -> None:
    env = {
        "FICHAITOR_ODOO_URL": "https://erp.example.test/",
        "FICHAITOR_ODOO_DB": "bitclick",
        "FICHAITOR_ODOO_API_KEY": "k" * 40,
        "FICHAITOR_ODOO_JOURNAL_ID": "30",
        "FICHAITOR_ODOO_PAYMENT_JOURNAL_ID": "31",
        "FICHAITOR_ODOO_TAX_IVA_21": "101",
        "FICHAITOR_ODOO_TAX_NO_SUJETO": "102",
        "FICHAITOR_ODOO_TAX_ISP_UE": "103",
        "FICHAITOR_ODOO_TAX_EXTRACOMUNITARIO": "104",
        "FICHAITOR_ODOO_PRODUCT_PRO": "201",
        "FICHAITOR_ODOO_PRODUCT_PRO_WHATSAPP": "202",
        "FICHAITOR_ODOO_FPOS_CANARIAS": "402",
    }
    parsed = OdooSettings.from_env(env)
    assert parsed.url == "https://erp.example.test" and parsed.fiscal_positions == {"canarias": 402}
    assert "k" * 40 not in repr(parsed)
    assert parsed.taxes[TaxTreatment.EXTRACOMUNITARIO] == 104
    missing = {k: v for k, v in env.items() if k != "FICHAITOR_ODOO_TAX_EXTRACOMUNITARIO"}
    with pytest.raises(ValueError, match="FICHAITOR_ODOO_TAX_EXTRACOMUNITARIO"):
        OdooSettings.from_env(missing)
    with pytest.raises(ValueError, match="FICHAITOR_ODOO_TAX_ISP_UE") as caught:
        OdooSettings.from_env({**env, "FICHAITOR_ODOO_TAX_ISP_UE": "abc"})
    assert "abc" not in str(caught.value)
    with pytest.raises(ValueError, match="https"):
        OdooSettings.from_env({**env, "FICHAITOR_ODOO_URL": "http://erp.example.test"})


def test_money_helpers() -> None:
    assert to_cents(42.35) == 4235 and to_cents(0.1 + 0.2) == 30 and to_cents(None) == 0
    assert credit_base(4235, 21) == 3500 and credit_base(1210, 21) == 1000
    for amount in range(1, 3000):
        base = credit_base(amount, 0)
        assert base == amount


def test_issued_document_flags() -> None:
    doc = IssuedDocument(1, "invoice", "in_1", "FTR/1", "posted", "in_payment", 1, 0, 1, 1)
    assert doc.paid and doc.total_ok
