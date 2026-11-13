"""The payments port (docs/admin-facturacion.md, sections 3, 4.1 and 4.2): the contract on
the fake, and the Stripe adapter against a mock transport. Nothing here calls Stripe."""

import asyncio
import inspect
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import stripe
from stripe._api_version import _ApiVersion

from app.adapters.stripe.fake import FakePayments
from app.adapters.stripe.real import (
    STRIPE_API_VERSION,
    StripePayments,
    StripeSettings,
    parse_invoice,
    parse_subscription,
)
from app.domain.billing import BlockReason, PriceKey, SeatStatus, TaxTreatment, seat_state
from app.ports.payments import (
    Address,
    CustomerDetails,
    InvoiceInfo,
    PaymentDeclined,
    Payments,
    PaymentsError,
    PaymentsNotFound,
    PaymentsRejected,
    PaymentsUnavailable,
    stripe_snapshot,
)

KEY = "rk_test_51FichaitorSecretKeyThatMustNeverLeak0123456789"  # gitleaks:allow
NOW = datetime(2027, 3, 14, 9, 0, tzinfo=UTC)
DETAILS = CustomerDetails(
    legal_name="Bar La Parra S.L.",
    tax_id="B12345674",
    tax_id_type="es_cif",
    address=Address("Calle Real 1", "18001", "Granada", "ES"),
    invoice_email="facturas@laparra.example",
)


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


# --- the contract, on the fake -------------------------------------------------------


def test_both_have_the_methods_of_the_port() -> None:
    wanted = {n for n, _ in inspect.getmembers(Payments) if not n.startswith("_")}
    for cls in (FakePayments, StripePayments):
        for name in wanted:
            port = inspect.signature(getattr(Payments, name))
            mine = inspect.signature(getattr(cls, name))
            assert list(port.parameters) == list(mine.parameters), (cls, name)
            assert [p.kind for p in port.parameters.values()] == [
                p.kind for p in mine.parameters.values()
            ], (cls, name)


def fake_with_customer() -> tuple[FakePayments, str]:
    fake = FakePayments(now=lambda: NOW)
    customer = run(fake.create_customer("acc_1", DETAILS, idempotency_key="acc_1:customer"))
    return fake, customer


def buy(fake: FakePayments, customer: str, seat: str = "seat_1", key: str | None = None) -> Any:
    return run(
        fake.create_subscription(
            customer,
            PriceKey.PRO_FIRST,
            TaxTreatment.IVA_21,
            "acc_1",
            seat,
            idempotency_key=key or f"{seat}:create",
        )
    )


def test_fake_prices_and_tax_rates_cover_every_key() -> None:
    fake = FakePayments()
    prices = run(fake.prices())
    assert set(prices) == set(PriceKey)
    assert prices[PriceKey.PRO_ADDITIONAL].unit_amount == 3325
    assert all(p.interval == "year" and p.currency == "eur" for p in prices.values())
    assert set(run(fake.tax_rates())) == set(TaxTreatment)


def test_fake_same_key_same_result_other_params_refused() -> None:
    fake, customer = fake_with_customer()
    again = run(fake.create_customer("acc_1", DETAILS, idempotency_key="acc_1:customer"))
    assert again == customer and len(fake.customers) == 1
    first = buy(fake, customer)
    assert buy(fake, customer) == first and len(fake.seats) == 1  # retried after a cut
    with pytest.raises(PaymentsRejected) as caught:
        buy(fake, customer, seat="seat_2", key="seat_1:create")
    assert caught.value.code == "idempotency_error" and not caught.value.retryable


def test_fake_charges_a_whole_year_with_vat() -> None:
    fake, customer = fake_with_customer()
    sub = buy(fake, customer)
    assert sub.status == "active" and sub.price_key is PriceKey.PRO_FIRST
    assert (sub.current_period_start, sub.current_period_end) == (NOW, NOW.replace(year=2028))
    assert (sub.account_id, sub.seat_id) == ("acc_1", "seat_1")
    invoice = run(fake.invoice(sub.latest_invoice_id))
    assert (invoice.subtotal, invoice.tax, invoice.amount_paid) == (3500, 735, 4235)
    assert invoice.billing_reason == "subscription_create" and invoice.charge_id
    state = seat_state(stripe_snapshot(sub, invoice))
    assert state.status is SeatStatus.ACTIVE and state.block is None


def test_fake_first_purchase_with_3ds_is_pending_and_never_blocks() -> None:
    fake, customer = fake_with_customer()
    fake.card = "3ds"
    sub = buy(fake, customer)
    assert sub.status == "incomplete" and sub.hosted_invoice_url
    invoice = run(fake.invoice(sub.latest_invoice_id))
    state = seat_state(stripe_snapshot(sub, invoice))
    assert state.status is SeatStatus.PENDING and state.block is None


def test_fake_failed_renewal_blocks_and_the_frozen_price_is_charged() -> None:
    fake, customer = fake_with_customer()
    sub = buy(fake, customer)
    t30 = "t30"
    run(fake.set_renewal_price(sub.id, PriceKey.PRO_ADDITIONAL, idempotency_key=t30))
    invoice = fake.renew(sub.id, card="declined")
    assert invoice.subtotal == 3325 and invoice.billing_reason == "subscription_cycle"
    sub = run(fake.subscription(sub.id))
    state = seat_state(stripe_snapshot(sub, invoice))
    assert state.block is BlockReason.PAYMENT_FAILED
    assert [i.id for i in run(fake.open_invoices(customer))] == [invoice.id]


def test_fake_cancel_at_period_end_and_undo() -> None:
    fake, customer = fake_with_customer()
    sub = buy(fake, customer)
    assert run(
        fake.set_cancel_at_period_end(sub.id, True, idempotency_key="c1")
    ).cancel_at_period_end
    assert not run(
        fake.set_cancel_at_period_end(sub.id, False, idempotency_key="c2")
    ).cancel_at_period_end


def test_fake_refund_only_what_was_paid() -> None:
    fake, customer = fake_with_customer()
    sub = buy(fake, customer)
    refund = run(fake.refund(sub.latest_invoice_id, 1210, "error_cobro", idempotency_key="r1"))
    assert refund.amount == 1210 and refund.invoice_id == sub.latest_invoice_id
    assert (
        run(fake.refund(sub.latest_invoice_id, 1210, "error_cobro", idempotency_key="r1")) == refund
    )
    with pytest.raises(PaymentsRejected):
        run(fake.refund(sub.latest_invoice_id, 5000, "error_cobro", idempotency_key="r2"))
    rest = run(fake.refund(sub.latest_invoice_id, None, "error_cobro", idempotency_key="r3"))
    assert rest.amount == 4235 - 1210
    assert run(fake.refund_info(rest.id)) == rest


def test_fake_dispute_blocks() -> None:
    fake, customer = fake_with_customer()
    sub = buy(fake, customer)
    dispute = fake.open_dispute(sub.latest_invoice_id)
    assert run(fake.dispute(dispute.id)).status == "needs_response"
    invoice = run(fake.invoice(sub.latest_invoice_id))
    assert seat_state(stripe_snapshot(sub, invoice)).block is BlockReason.DISPUTE


def test_fake_unknown_ids_are_not_found() -> None:
    fake = FakePayments()
    for call in (
        fake.subscription("sub_x"),
        fake.invoice("in_x"),
        fake.refund_info("re_x"),
        fake.dispute("dp_x"),
        fake.portal_url("cus_x", "https://fichaitor.es/app"),
    ):
        with pytest.raises(PaymentsNotFound):
            run(call)


def test_fake_checkout_then_payment() -> None:
    fake, customer = fake_with_customer()
    session = run(
        fake.create_checkout(
            customer,
            PriceKey.WHATSAPP_FIRST,
            TaxTreatment.NO_SUJETO,
            "acc_1",
            "seat_9",
            "https://fichaitor.es/app/ok",
            "https://fichaitor.es/app/ko",
            idempotency_key="k",
        )
    )
    sub = fake.complete_checkout(session.id)
    invoice = run(fake.invoice(sub.latest_invoice_id))
    assert (invoice.subtotal, invoice.tax, invoice.total) == (5900, 0, 5900)


def test_fake_fail_next() -> None:
    fake = FakePayments()
    fake.fail_next(PaymentsUnavailable("down", status=503))
    with pytest.raises(PaymentsUnavailable):
        run(fake.prices())
    assert run(fake.prices())


def test_no_card_data_in_the_port() -> None:
    import dataclasses

    from app.ports import payments

    card_words = ("card", "number", "cvc", "exp_", "iban", "last4", "fingerprint")
    for name in dir(payments):
        cls = getattr(payments, name)
        if dataclasses.is_dataclass(cls):
            for f in dataclasses.fields(cls):
                assert not any(w in f.name for w in card_words), (name, f.name)


def test_personal_data_stays_out_of_repr() -> None:
    text = repr(DETAILS)
    for value in ("Bar La Parra", "B12345674", "Calle Real", "18001", "facturas@"):
        assert value not in text


# --- the Stripe adapter, against a mock transport ------------------------------------


def _list(data: list[dict[str, Any]], url: str) -> dict[str, Any]:
    return {"object": "list", "data": data, "has_more": False, "url": url}


PRICES = [
    {
        "id": f"price_{k.name.lower()}",
        "object": "price",
        "lookup_key": k.value,
        "unit_amount": a,
        "currency": "eur",
        "recurring": {"interval": "year"},
    }
    for k, a in zip(PriceKey, (3500, 3325, 5900, 5605), strict=True)
]
TAX_RATES = [
    {"id": f"txr_{t.value}", "object": "tax_rate", "metadata": {"fichaitor_tax": t.value}}
    for t in TaxTreatment
] + [{"id": "txr_other", "object": "tax_rate", "metadata": {}}]

SUBSCRIPTION = {
    "id": "sub_1",
    "object": "subscription",
    "customer": "cus_1",
    "status": "active",
    "cancel_at_period_end": False,
    "canceled_at": None,
    "cancellation_details": {"reason": None},
    "metadata": {"account_id": "acc_1", "seat_id": "seat_1"},
    "items": _list(
        [
            {
                "id": "si_1",
                "object": "subscription_item",
                "price": {
                    "id": "price_pro_first",
                    "object": "price",
                    "lookup_key": "pro_first_yearly",
                },
                "current_period_start": 1804928400,
                "current_period_end": 1836464400,
            }
        ],
        "/v1/subscription_items",
    ),
    "latest_invoice": {
        "id": "in_1",
        "object": "invoice",
        "status": "paid",
        "hosted_invoice_url": "https://invoice.stripe.com/i/in_1",
    },
}
INVOICE = {
    "id": "in_1",
    "object": "invoice",
    "customer": "cus_1",
    "status": "paid",
    "billing_reason": "subscription_cycle",
    "currency": "eur",
    "subtotal": 3500,
    "total": 4235,
    "amount_paid": 4235,
    "total_taxes": [{"amount": 735}],
    "status_transitions": {"paid_at": 1804928400},
    "hosted_invoice_url": "https://invoice.stripe.com/i/in_1",
    "parent": {
        "subscription_details": {
            "subscription": "sub_1",
            "metadata": {"account_id": "acc_1", "seat_id": "seat_1"},
        }
    },
    "lines": _list(
        [
            {
                "id": "il_1",
                "object": "line_item",
                "period": {"start": 1804928400, "end": 1836464400},
                "pricing": {"price_details": {"price": "price_pro_first"}},
            }
        ],
        "/v1/x",
    ),
    "payments": _list(
        [
            {
                "id": "inpay_1",
                "object": "invoice_payment",
                "status": "paid",
                "payment": {"type": "payment_intent", "payment_intent": "pi_1"},
            }
        ],
        "/v1/y",
    ),
}
INTENT = {
    "id": "pi_1",
    "object": "payment_intent",
    "status": "succeeded",
    "latest_charge": {"id": "ch_1", "object": "charge", "disputed": True, "amount_refunded": 0},
}


class FakeStripeServer:
    def __init__(self, routes: dict[tuple[str, str], Callable[[], httpx.Response]] | None = None):
        self.requests: list[httpx.Request] = []
        self.routes = routes or {}

    def form(self, index: int = -1) -> dict[str, list[str]]:
        return parse_qs(self.requests[index].content.decode())

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key in self.routes:
            return self.routes[key]()
        default = {
            ("GET", "/v1/prices"): _list(PRICES, "/v1/prices"),
            ("GET", "/v1/tax_rates"): _list(TAX_RATES, "/v1/tax_rates"),
            ("POST", "/v1/customers"): {"id": "cus_1", "object": "customer"},
            ("POST", "/v1/checkout/sessions"): {
                "id": "cs_1",
                "object": "checkout.session",
                "url": "https://checkout.stripe.com/c/pay/cs_1",
            },
            ("POST", "/v1/subscriptions"): SUBSCRIPTION,
            ("GET", "/v1/subscriptions/sub_1"): SUBSCRIPTION,
            ("GET", "/v1/invoices/in_1"): INVOICE,
            ("GET", "/v1/payment_intents/pi_1"): INTENT,
        }
        if key not in default:
            return stripe_error(404, "invalid_request_error", "resource_missing")
        return httpx.Response(200, json=default[key])


def stripe_error(status: int, type_: str, code: str | None = None, message: str = "") -> Any:
    body = {"error": {"type": type_, "code": code, "message": message or f"{type_} {KEY}"}}
    return httpx.Response(status, json=body)


def adapter(server: FakeStripeServer, retries: int = 0) -> StripePayments:
    return StripePayments(
        StripeSettings(KEY),
        transport=httpx.MockTransport(server.handle),
        max_network_retries=retries,
    )


def test_the_api_version_is_pinned_and_is_the_sdk_one() -> None:
    # Moving the SDK changes the shape of the objects: read the changelog, then this.
    assert STRIPE_API_VERSION == _ApiVersion.CURRENT
    server = FakeStripeServer()
    run(adapter(server).prices())
    assert server.requests[0].headers["stripe-version"] == STRIPE_API_VERSION
    assert server.requests[0].headers["authorization"] == f"Bearer {KEY}"


def test_writes_send_the_callers_idempotency_key() -> None:
    server = FakeStripeServer()
    customer = run(adapter(server).create_customer("acc_1", DETAILS, idempotency_key="acc:c"))
    assert customer == "cus_1"
    sent = server.requests[-1]
    assert sent.headers["idempotency-key"] == "acc:c"
    form = server.form()
    assert form["metadata[account_id]"] == ["acc_1"]
    assert form["tax_id_data[0][type]"] == ["es_cif"]
    assert form["tax_id_data[0][value]"] == ["B12345674"]
    with pytest.raises(ValueError):
        run(adapter(server).create_customer("acc_1", DETAILS, idempotency_key=""))


def test_a_retry_after_a_5xx_reuses_the_same_key() -> None:
    answers = iter([stripe_error(500, "api_error"), None])
    server = FakeStripeServer()

    def first_fails() -> httpx.Response:
        answer = next(answers)
        return answer or httpx.Response(200, json={"id": "cus_1", "object": "customer"})

    server.routes[("POST", "/v1/customers")] = first_fails
    run(adapter(server, retries=1).create_customer("acc_1", DETAILS, idempotency_key="k1"))
    posts = [r for r in server.requests if r.method == "POST"]
    assert len(posts) == 2 and {r.headers["idempotency-key"] for r in posts} == {"k1"}


def test_checkout_is_card_only_with_the_price_by_lookup_key() -> None:
    server = FakeStripeServer()
    session = run(
        adapter(server).create_checkout(
            "cus_1",
            PriceKey.PRO_ADDITIONAL,
            TaxTreatment.IVA_21,
            "acc_1",
            "seat_2",
            "https://fichaitor.es/app/ok",
            "https://fichaitor.es/app/ko",
            idempotency_key="s2",
        )
    )
    assert session.url.startswith("https://checkout.stripe.com/")
    form = server.form()
    assert form["mode"] == ["subscription"]
    assert form["payment_method_types[0]"] == ["card"] and "payment_method_types[1]" not in form
    assert form["line_items[0][price]"] == ["price_pro_additional"]
    assert form["line_items[0][tax_rates][0]"] == ["txr_iva_21"]
    assert form["subscription_data[metadata][seat_id]"] == ["seat_2"]
    assert not any("trial" in k for k in form)


def test_a_subscription_is_a_whole_year_without_proration() -> None:
    server = FakeStripeServer()
    sub = run(
        adapter(server).create_subscription(
            "cus_1",
            PriceKey.PRO_FIRST,
            TaxTreatment.IVA_21,
            "acc_1",
            "seat_1",
            idempotency_key="seat_1:create",
        )
    )
    form = server.form()
    assert form["proration_behavior"] == ["none"]
    assert form["payment_settings[payment_method_types][0]"] == ["card"]
    assert form["metadata[seat_id]"] == ["seat_1"]
    assert sub.price_key is PriceKey.PRO_FIRST and sub.latest_invoice_status == "paid"


def test_an_invoice_is_read_with_its_payment() -> None:
    server = FakeStripeServer()
    invoice = run(adapter(server).invoice("in_1"))
    assert invoice.subscription_id == "sub_1" and invoice.seat_id == "seat_1"
    assert (invoice.subtotal, invoice.tax, invoice.total, invoice.amount_paid) == (
        3500,
        735,
        4235,
        4235,
    )
    assert invoice.price_key is PriceKey.PRO_FIRST
    assert (invoice.payment_intent_id, invoice.charge_id) == ("pi_1", "ch_1")
    assert invoice.disputed
    assert invoice.period_end == datetime.fromtimestamp(1836464400, UTC)


def test_parse_subscription_of_a_cancelled_seat() -> None:
    data = {
        **SUBSCRIPTION,
        "status": "canceled",
        "canceled_at": 1806000000,
        "cancellation_details": {"reason": "payment_failed"},
        "latest_invoice": "in_9",
    }
    sub = parse_subscription(data)
    assert sub.latest_invoice_id == "in_9" and sub.latest_invoice_status is None
    state = seat_state(stripe_snapshot(sub, None))
    assert state.status is SeatStatus.CANCELED
    assert state.entitled_until == datetime.fromtimestamp(1806000000, UTC)


def test_parse_invoice_without_payment() -> None:
    info = parse_invoice({"id": "in_2", "customer": {"id": "cus_1"}, "status": "open"})
    assert isinstance(info, InvoiceInfo) and info.customer_id == "cus_1"
    assert info.payment_intent_id is None and info.tax == 0 and not info.disputed


def test_missing_prices_or_tax_rates_are_permanent() -> None:
    server = FakeStripeServer(
        {("GET", "/v1/prices"): lambda: httpx.Response(200, json=_list(PRICES[:3], "/v1/p"))}
    )
    with pytest.raises(PaymentsRejected) as caught:
        run(adapter(server).prices())
    assert caught.value.code == "price_missing"
    server = FakeStripeServer(
        {("GET", "/v1/tax_rates"): lambda: httpx.Response(200, json=_list(TAX_RATES[1:], "/t"))}
    )
    with pytest.raises(PaymentsRejected) as caught:
        run(adapter(server).tax_rates())
    assert caught.value.code == "tax_rate_missing"


def _raise(error: Exception) -> Callable[[], httpx.Response]:
    def go() -> httpx.Response:
        raise error

    return go


CASES = [
    (lambda: stripe_error(500, "api_error"), PaymentsUnavailable, 500),
    (lambda: stripe_error(503, "api_error"), PaymentsUnavailable, 503),
    (lambda: stripe_error(429, "invalid_request_error", "rate_limit"), PaymentsUnavailable, 429),
    (_raise(httpx.ReadTimeout("slow")), PaymentsUnavailable, None),
    (_raise(httpx.ConnectError("down")), PaymentsUnavailable, None),
    (
        lambda: stripe_error(400, "invalid_request_error", "parameter_invalid_empty"),
        PaymentsRejected,
        400,
    ),
    (lambda: stripe_error(402, "card_error", "card_declined"), PaymentDeclined, 402),
    (lambda: stripe_error(404, "invalid_request_error", "resource_missing"), PaymentsNotFound, 404),
    (
        lambda: stripe_error(
            401, "invalid_request_error", None, f"Invalid API Key provided: {KEY}"
        ),
        PaymentsRejected,
        401,
    ),
    (lambda: stripe_error(403, "invalid_request_error"), PaymentsRejected, 403),
]


@pytest.mark.parametrize(("answer", "error", "status"), CASES)
def test_temporary_and_permanent_errors(
    answer: Callable[[], httpx.Response], error: type[PaymentsError], status: int | None
) -> None:
    server = FakeStripeServer({("POST", "/v1/customers"): answer})
    with pytest.raises(PaymentsError) as caught:
        run(adapter(server).create_customer("acc_1", DETAILS, idempotency_key="k"))
    assert type(caught.value) is error
    assert caught.value.status == status
    assert caught.value.retryable is (error is PaymentsUnavailable)


def test_a_key_reused_with_other_params_is_permanent() -> None:
    server = FakeStripeServer(
        {("POST", "/v1/customers"): lambda: stripe_error(400, "idempotency_error")}
    )
    with pytest.raises(PaymentsRejected) as caught:
        run(adapter(server).create_customer("acc_1", DETAILS, idempotency_key="k"))
    assert caught.value.code == "idempotency_error"


def test_no_secret_in_errors_logs_or_repr(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    texts = [repr(StripeSettings(KEY)), repr(adapter(FakeStripeServer()))]
    for answer, _, _ in CASES:
        server = FakeStripeServer({("POST", "/v1/customers"): answer})
        with pytest.raises(PaymentsError) as caught:
            run(adapter(server).create_customer("acc_1", DETAILS, idempotency_key="k"))
        error = caught.value
        assert error.__cause__ is None and error.__suppress_context__
        texts.append(f"{error} {error!r} {error.args}")
    run(adapter(FakeStripeServer()).create_customer("acc_1", DETAILS, idempotency_key="k"))
    texts.append(caplog.text)
    for text in texts:
        assert KEY not in text and KEY[-12:] not in text
        assert "B12345674" not in text  # the fiscal data the SDK logs at DEBUG


def test_settings_check_the_key_and_the_mode() -> None:
    assert not StripeSettings(KEY).livemode
    assert StripeSettings("rk_live_x", livemode=True).livemode
    with pytest.raises(ValueError) as caught:
        StripeSettings("rk_live_abcdef")  # a live key without FICHAITOR_STRIPE_LIVEMODE
    assert "abcdef" not in str(caught.value)
    with pytest.raises(ValueError):
        StripeSettings(KEY, livemode=True)
    with pytest.raises(ValueError):
        StripeSettings("pk_test_publishable")
    env = {"FICHAITOR_STRIPE_SECRET_KEY": KEY, "FICHAITOR_STRIPE_LIVEMODE": "false"}
    assert StripeSettings.from_env(env).api_key == KEY


def test_the_sdk_errors_are_the_ones_mapped() -> None:
    # A guard for SDK upgrades: the classes the adapter tells apart still exist.
    for name in ("APIConnectionError", "RateLimitError", "IdempotencyError", "CardError"):
        assert issubclass(getattr(stripe, name), stripe.StripeError)
    assert json.dumps(INVOICE)  # the sample stays plain JSON
