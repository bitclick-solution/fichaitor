"""Stripe for `fichaitor-billing`, with the official SDK (`stripe`) and its asynchronous
`StripeClient` over httpx.

Why the SDK and not plain httpx: webhooks are verified with `stripe.Webhook` (step 6) and
the SDK keeps the encoding of nested parameters, the idempotent retries and the typed
errors right. Conectores ERP uses the same SDK, but synchronously in a thread; here every
call is `*_async`, so no thread is needed. Its other habits are kept: prices found by
`lookup_key`, the Tax Rates found by their metadata, card only, Stripe objects read with
`to_dict()`.

- The API version is fixed (`STRIPE_API_VERSION`) and must be the one of the installed
  SDK, whose models follow it; a test fails when the SDK moves, so the change is read.
- Every write sends the caller's idempotency key. The SDK retries a network failure or a
  5xx with that same key, so Stripe applies it once.
- No card data is read or kept: payment methods are never expanded.
- Errors are ours (`app.ports.payments`) and carry the status and Stripe's code only.
  Stripe's message is left out, and so is the original exception (`from None`), so
  neither reaches a log. The SDK's own logger is kept at WARNING: at DEBUG it writes the
  bodies sent and received, with the customer's fiscal data.
"""

import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar

import httpx
import stripe

from app.domain.billing import PAYMENT_METHOD_TYPES, PriceKey, TaxTreatment
from app.ports.payments import (
    CheckoutInfo,
    CheckoutSession,
    CustomerDetails,
    DisputeInfo,
    InvoiceInfo,
    PaymentDeclined,
    PaymentsError,
    PaymentsNotFound,
    PaymentsRejected,
    PaymentsUnavailable,
    Price,
    RefundInfo,
    SubscriptionInfo,
)

log = logging.getLogger("fichaitor.billing.stripe")

STRIPE_API_VERSION = "2026-10-28.endive"
TIMEOUT = 30.0
NETWORK_RETRIES = 2
PRICES_TTL = 600.0  # seconds a price list is kept before it is read again
MAX_PAGES = 50  # pages of 100 in a list: the daily reconciliation reads no more
# An open Checkout blocks a second purchase of its profile (seat_pending) until it
# expires: 31 minutes, because Stripe refuses less than 30 when the request arrives.
CHECKOUT_TTL_SECONDS = 1860
TAX_RATE_METADATA = "fichaitor_tax"  # set by scripts/stripe_bootstrap.py
_KEY_PREFIXES = ("sk_test_", "rk_test_", "sk_live_", "rk_live_")

T = TypeVar("T")


@dataclass(frozen=True)
class StripeSettings:
    """`billing.env`: the restricted key (`rk_…`, section 4.1) and the mode it must be."""

    api_key: str = field(repr=False)
    livemode: bool = False

    def __post_init__(self) -> None:
        if not self.api_key.startswith(_KEY_PREFIXES):
            raise ValueError("FICHAITOR_STRIPE_SECRET_KEY is not a Stripe secret key")
        if ("_live_" in self.api_key) != self.livemode:
            raise ValueError("FICHAITOR_STRIPE_SECRET_KEY does not match FICHAITOR_STRIPE_LIVEMODE")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "StripeSettings":
        return cls(
            api_key=env.get("FICHAITOR_STRIPE_SECRET_KEY", ""),
            livemode=env.get("FICHAITOR_STRIPE_LIVEMODE", "").lower() == "true",
        )


class _HttpxClient(stripe.HTTPXClient):
    """The SDK's httpx client, with a transport the tests can replace."""

    def __init__(self, timeout: float, transport: httpx.AsyncBaseTransport | None) -> None:
        super().__init__(timeout=timeout)
        if transport is not None:
            self._client_async = httpx.AsyncClient(transport=transport)

    def _get_request_args_kwargs(
        self, method: str, url: str, headers: Mapping[str, str], post_data: Any
    ) -> Any:
        # The SDK sends the encoded form as `data=<str>`, which httpx deprecates.
        args, kwargs = super()._get_request_args_kwargs(method, url, headers, post_data)
        data = kwargs.pop("data", None)
        if data:
            kwargs["content"] = data
        return args, kwargs


def _ts(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), UTC) if value else None


def _id(value: Any) -> str | None:
    """The id of a field Stripe may give expanded (a dict) or not (a string)."""
    if isinstance(value, dict):
        value = value.get("id")
    return str(value) if value else None


def _price_key(value: Any) -> PriceKey | None:
    try:
        return PriceKey(value) if value else None
    except ValueError:
        return None


def mapped_error(exc: stripe.StripeError, operation: str) -> PaymentsError:
    """Our error for one of the SDK: temporary or permanent, without Stripe's message."""
    status = exc.http_status
    code = exc.code if isinstance(exc.code, str) else None
    if isinstance(exc, stripe.APIConnectionError):
        return PaymentsUnavailable(f"Stripe {operation}: error de red o tiempo agotado")
    if isinstance(exc, stripe.RateLimitError) or status == 429:
        return PaymentsUnavailable(f"Stripe {operation}: HTTP 429", status=status, code=code)
    if isinstance(exc, stripe.IdempotencyError):
        return PaymentsRejected(
            f"Stripe {operation}: clave de idempotencia reutilizada",
            status=status,
            code="idempotency_error",
        )
    if isinstance(exc, stripe.CardError):
        return PaymentDeclined(f"Stripe {operation}: tarjeta rechazada", status=status, code=code)
    if status == 404 or code == "resource_missing":
        return PaymentsNotFound(f"Stripe {operation}: no existe", status=status, code=code)
    if status is None or status >= 500 or status == 409:
        return PaymentsUnavailable(f"Stripe {operation}: HTTP {status}", status=status, code=code)
    return PaymentsRejected(f"Stripe {operation}: HTTP {status}", status=status, code=code)


def parse_subscription(data: Mapping[str, Any]) -> SubscriptionInfo:
    items = ((data.get("items") or {}).get("data")) or [{}]
    item = items[0]
    latest = data.get("latest_invoice")
    latest = latest if isinstance(latest, dict) else {}
    metadata = data.get("metadata") or {}
    start = _ts(item.get("current_period_start"))
    end = _ts(item.get("current_period_end"))
    if start is None or end is None:
        raise PaymentsRejected("Stripe: suscripción sin periodo", code="bad_object")
    return SubscriptionInfo(
        id=str(data["id"]),
        customer_id=_id(data.get("customer")) or "",
        status=str(data.get("status") or ""),
        price_key=_price_key((item.get("price") or {}).get("lookup_key")),
        current_period_start=start,
        current_period_end=end,
        cancel_at_period_end=bool(data.get("cancel_at_period_end")),
        canceled_at=_ts(data.get("canceled_at")),
        cancellation_reason=(data.get("cancellation_details") or {}).get("reason"),
        account_id=metadata.get("account_id"),
        seat_id=metadata.get("seat_id"),
        latest_invoice_id=_id(data.get("latest_invoice")),
        latest_invoice_status=latest.get("status"),
        hosted_invoice_url=latest.get("hosted_invoice_url"),
        trial_end=_ts(data.get("trial_end")),
    )


def parse_invoice(
    data: Mapping[str, Any],
    intent: Mapping[str, Any] | None = None,
    price_key: PriceKey | None = None,
) -> InvoiceInfo:
    """An invoice of API 2026-10-28 (the subscription and its metadata under
    `parent.subscription_details`, the taxes in `total_taxes`), and the PaymentIntent that
    paid it with its `latest_charge` expanded."""
    details = (data.get("parent") or {}).get("subscription_details") or {}
    metadata = details.get("metadata") or {}
    line = (((data.get("lines") or {}).get("data")) or [{}])[0]
    period = line.get("period") or {}
    charge = (intent or {}).get("latest_charge")
    charge = charge if isinstance(charge, dict) else {}
    return InvoiceInfo(
        id=str(data["id"]),
        customer_id=_id(data.get("customer")) or "",
        subscription_id=_id(details.get("subscription")),
        status=data.get("status"),
        billing_reason=data.get("billing_reason"),
        currency=str(data.get("currency") or "eur"),
        subtotal=int(data.get("subtotal") or 0),
        tax=sum(int(t.get("amount") or 0) for t in data.get("total_taxes") or []),
        total=int(data.get("total") or 0),
        amount_paid=int(data.get("amount_paid") or 0),
        price_key=price_key,
        period_start=_ts(period.get("start")),
        period_end=_ts(period.get("end")),
        paid_at=_ts((data.get("status_transitions") or {}).get("paid_at")),
        hosted_invoice_url=data.get("hosted_invoice_url"),
        payment_intent_id=_id(intent),
        payment_intent_status=(intent or {}).get("status"),
        charge_id=_id(charge) or _id((intent or {}).get("latest_charge")),
        disputed=bool(charge.get("disputed")),
        amount_refunded=int(charge.get("amount_refunded") or 0),
        account_id=metadata.get("account_id"),
        seat_id=metadata.get("seat_id"),
    )


def _intent_id(invoice: Mapping[str, Any]) -> str | None:
    """The PaymentIntent of the payment of an invoice: the paid one, else the latest."""
    payments = ((invoice.get("payments") or {}).get("data")) or []
    ordered = sorted(payments, key=lambda p: p.get("status") != "paid")
    for payment in ordered:
        inner = payment.get("payment") or {}
        if inner.get("type") == "payment_intent" and inner.get("payment_intent"):
            return _id(inner["payment_intent"])
    return None


class StripePayments:
    """The `Payments` port over Stripe. `transport` and `max_network_retries` are for the
    tests: a mock transport and no waits."""

    def __init__(
        self,
        settings: StripeSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_network_retries: int = NETWORK_RETRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        logging.getLogger("stripe").setLevel(logging.WARNING)
        self._livemode = settings.livemode
        self._client = stripe.StripeClient(
            settings.api_key,
            stripe_version=STRIPE_API_VERSION,
            max_network_retries=max_network_retries,
            http_client=_HttpxClient(TIMEOUT, transport),
        )
        self._clock = clock
        self._prices: tuple[float, dict[PriceKey, Price]] | None = None
        self._tax_rates: dict[TaxTreatment, str] | None = None

    def __repr__(self) -> str:
        return f"StripePayments(livemode={self._livemode})"

    async def _do(self, operation: str, call: Callable[[], Awaitable[T]]) -> T:
        try:
            return await call()
        except stripe.StripeError as exc:
            error = mapped_error(exc, operation)
            log.warning("stripe %s: %s %s", operation, error.status, error.code or "")
            raise error from None

    @staticmethod
    def _options(idempotency_key: str) -> Any:
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        return {"idempotency_key": idempotency_key}

    # --- catalogue -----------------------------------------------------------------

    async def prices(self) -> dict[PriceKey, Price]:
        if self._prices and self._clock() - self._prices[0] < PRICES_TTL:
            return self._prices[1]
        keys = [k.value for k in PriceKey]
        found = await self._do(
            "prices.list",
            lambda: self._client.v1.prices.list_async(
                params={"lookup_keys": keys, "active": True, "limit": 100}
            ),
        )
        prices: dict[PriceKey, Price] = {}
        for item in found.data:
            data = item.to_dict()
            key = _price_key(data.get("lookup_key"))
            if key is None:
                continue
            recurring = data.get("recurring") or {}
            prices[key] = Price(
                id=str(data["id"]),
                lookup_key=key,
                unit_amount=int(data.get("unit_amount") or 0),
                currency=str(data.get("currency") or ""),
                interval=recurring.get("interval"),
            )
        missing = [k.value for k in PriceKey if k not in prices]
        if missing:
            raise PaymentsRejected(f"Stripe: faltan precios {missing}", code="price_missing")
        self._prices = (self._clock(), prices)
        return prices

    async def _price_id(self, key: PriceKey) -> str:
        return (await self.prices())[key].id

    async def _key_for_price(self, price_id: str | None) -> PriceKey | None:
        if not price_id:
            return None
        return next(
            (p.lookup_key for p in (await self.prices()).values() if p.id == price_id), None
        )

    async def tax_rates(self) -> dict[TaxTreatment, str]:
        if self._tax_rates is not None:
            return self._tax_rates
        found = await self._do(
            "tax_rates.list",
            lambda: self._client.v1.tax_rates.list_async(params={"active": True, "limit": 100}),
        )
        rates: dict[TaxTreatment, str] = {}
        for item in found.data:
            data = item.to_dict()
            tag = (data.get("metadata") or {}).get(TAX_RATE_METADATA)
            if tag in {t.value for t in TaxTreatment}:
                rates[TaxTreatment(tag)] = str(data["id"])
        missing = [t.value for t in TaxTreatment if t not in rates]
        if missing:
            raise PaymentsRejected(f"Stripe: faltan Tax Rates {missing}", code="tax_rate_missing")
        self._tax_rates = rates
        return rates

    # --- customers -----------------------------------------------------------------

    @staticmethod
    def _customer_params(details: CustomerDetails) -> dict[str, Any]:
        address = details.address
        return {
            "name": details.legal_name,
            "email": details.invoice_email,
            "address": {
                "line1": address.line1,
                "line2": address.line2 or "",
                "postal_code": address.postal_code or "",
                "city": address.city,
                "state": address.state or "",
                "country": address.country,
            },
            "preferred_locales": ["es"],
        }

    async def create_customer(
        self, account_id: str, details: CustomerDetails, *, idempotency_key: str
    ) -> str:
        params = {
            **self._customer_params(details),
            "metadata": {"account_id": account_id},
        }
        if details.tax_id_type is not None:
            # In the same call: a tax id Stripe refuses leaves no customer behind. Outside
            # the EU there is none: it stays in our database and in Odoo (section 2.7).
            params["tax_id_data"] = [{"type": details.tax_id_type, "value": details.tax_id}]
        options = self._options(idempotency_key)
        customer = await self._do(
            "customers.create",
            lambda: self._client.v1.customers.create_async(params=params, options=options),
        )
        return str(customer.id)

    async def update_customer(
        self, customer_id: str, details: CustomerDetails, *, idempotency_key: str
    ) -> None:
        """The new tax id is added before the old ones go: one Stripe refuses leaves the
        customer as it was (Conectores ERP). A customer now outside the EU keeps none."""
        ids = self._client.v1.customers.tax_ids
        current = await self._do(
            "tax_ids.list", lambda: ids.list_async(customer_id, params={"limit": 20})
        )
        keep = next(
            (
                t.id
                for t in current.data
                if t.value == details.tax_id and t.type == details.tax_id_type
            ),
            None,
        )
        if keep is None and details.tax_id_type is not None:
            options = self._options(f"{idempotency_key}:tax_id")
            created = await self._do(
                "tax_ids.create",
                lambda: ids.create_async(
                    customer_id,
                    params={"type": details.tax_id_type, "value": details.tax_id},  # type: ignore[typeddict-item]
                    options=options,
                ),
            )
            keep = created.id
        for tax in current.data:
            if tax.id != keep:
                await self._do("tax_ids.delete", lambda t=tax: ids.delete_async(customer_id, t.id))
        params = self._customer_params(details)
        options = self._options(idempotency_key)
        await self._do(
            "customers.update",
            lambda: self._client.v1.customers.update_async(
                customer_id, params=params, options=options
            ),
        )

    # --- seats ---------------------------------------------------------------------

    async def create_checkout(
        self,
        customer_id: str,
        price_key: PriceKey,
        treatment: TaxTreatment,
        account_id: str,
        seat_id: str,
        success_url: str,
        cancel_url: str,
        *,
        idempotency_key: str,
        trial_end: datetime | None = None,
    ) -> CheckoutSession:
        metadata = {"account_id": account_id, "seat_id": seat_id}
        subscription_data: dict[str, Any] = {"metadata": metadata}
        if trial_end is not None:
            # The beta window: the first year is charged when it ends (at least 48 hours
            # ahead, which billing.first_charge_at keeps). The card is still taken now.
            subscription_data["trial_end"] = int(trial_end.timestamp())
        params = {
            "mode": "subscription",
            "customer": customer_id,
            "client_reference_id": seat_id,
            "line_items": [
                {
                    "price": await self._price_id(price_key),
                    "quantity": 1,
                    "tax_rates": [(await self.tax_rates())[treatment]],
                }
            ],
            "payment_method_types": list(PAYMENT_METHOD_TYPES),
            # Always the card, also when nothing is due today (the beta window).
            "payment_method_collection": "always",
            "subscription_data": subscription_data,
            "metadata": metadata,
            "locale": "es",
            # An open Checkout blocks a second purchase of the same profile (seat_pending)
            # until it expires: the shortest Stripe allows.
            "expires_at": int(time.time()) + CHECKOUT_TTL_SECONDS,
            "success_url": success_url,
            "cancel_url": cancel_url,
        }
        options = self._options(idempotency_key)
        session = await self._do(
            "checkout.sessions.create",
            lambda: self._client.v1.checkout.sessions.create_async(
                params=params,  # type: ignore[arg-type]
                options=options,
            ),
        )
        return CheckoutSession(id=str(session.id), url=str(session.url))

    async def checkout_session(self, session_id: str) -> CheckoutInfo:
        session = await self._do(
            "checkout.sessions.retrieve",
            lambda: self._client.v1.checkout.sessions.retrieve_async(session_id),
        )
        data = session.to_dict()
        metadata = data.get("metadata") or {}
        return CheckoutInfo(
            id=str(data["id"]),
            status=data.get("status"),
            subscription_id=_id(data.get("subscription")),
            customer_id=_id(data.get("customer")),
            account_id=metadata.get("account_id"),
            seat_id=metadata.get("seat_id"),
        )

    async def create_subscription(
        self,
        customer_id: str,
        price_key: PriceKey,
        treatment: TaxTreatment,
        account_id: str,
        seat_id: str,
        *,
        idempotency_key: str,
    ) -> SubscriptionInfo:
        params = {
            "customer": customer_id,
            "items": [
                {
                    "price": await self._price_id(price_key),
                    "tax_rates": [(await self.tax_rates())[treatment]],
                }
            ],
            "metadata": {"account_id": account_id, "seat_id": seat_id},
            # Charged at once to the saved card; a 3DS leaves it incomplete with the
            # invoice to confirm. A whole year, no trial, no proration.
            "payment_behavior": "allow_incomplete",
            "payment_settings": {"payment_method_types": list(PAYMENT_METHOD_TYPES)},
            "proration_behavior": "none",
            "expand": ["latest_invoice"],
        }
        options = self._options(idempotency_key)
        sub = await self._do(
            "subscriptions.create",
            lambda: self._client.v1.subscriptions.create_async(
                params=params,  # type: ignore[arg-type]
                options=options,
            ),
        )
        return parse_subscription(sub.to_dict())

    async def subscription(self, subscription_id: str) -> SubscriptionInfo:
        sub = await self._do(
            "subscriptions.retrieve",
            lambda: self._client.v1.subscriptions.retrieve_async(
                subscription_id, params={"expand": ["latest_invoice"]}
            ),
        )
        return parse_subscription(sub.to_dict())

    async def _update_subscription(
        self, subscription_id: str, params: dict[str, Any], idempotency_key: str
    ) -> SubscriptionInfo:
        options = self._options(idempotency_key)
        sub = await self._do(
            "subscriptions.update",
            lambda: self._client.v1.subscriptions.update_async(
                subscription_id,
                params={**params, "expand": ["latest_invoice"]},
                options=options,
            ),
        )
        return parse_subscription(sub.to_dict())

    async def set_cancel_at_period_end(
        self, subscription_id: str, cancel: bool, *, idempotency_key: str
    ) -> SubscriptionInfo:
        return await self._update_subscription(
            subscription_id, {"cancel_at_period_end": cancel}, idempotency_key
        )

    async def set_renewal_price(
        self, subscription_id: str, price_key: PriceKey, *, idempotency_key: str
    ) -> SubscriptionInfo:
        sub = await self._do(
            "subscriptions.retrieve",
            lambda: self._client.v1.subscriptions.retrieve_async(subscription_id),
        )
        items = (sub.to_dict().get("items") or {}).get("data") or []
        if len(items) != 1:
            raise PaymentsRejected("Stripe: la plaza no tiene un solo elemento", code="bad_object")
        params = {
            "items": [{"id": items[0]["id"], "price": await self._price_id(price_key)}],
            "proration_behavior": "none",
        }
        return await self._update_subscription(subscription_id, params, idempotency_key)

    # --- invoices ------------------------------------------------------------------

    async def invoice(self, invoice_id: str) -> InvoiceInfo:
        inv = await self._do(
            "invoices.retrieve",
            lambda: self._client.v1.invoices.retrieve_async(
                invoice_id, params={"expand": ["payments"]}
            ),
        )
        return await self._invoice_info(inv.to_dict())

    async def _invoice_info(self, data: Mapping[str, Any]) -> InvoiceInfo:
        intent: dict[str, Any] | None = None
        intent_id = _intent_id(data)
        if intent_id:
            found = await self._do(
                "payment_intents.retrieve",
                lambda: self._client.v1.payment_intents.retrieve_async(
                    intent_id, params={"expand": ["latest_charge"]}
                ),
            )
            intent = found.to_dict()
        line = (((data.get("lines") or {}).get("data")) or [{}])[0]
        price_id = ((line.get("pricing") or {}).get("price_details") or {}).get("price")
        return parse_invoice(data, intent, await self._key_for_price(_id(price_id)))

    async def open_invoices(self, customer_id: str) -> list[InvoiceInfo]:
        found = await self._do(
            "invoices.list",
            lambda: self._client.v1.invoices.list_async(
                params={
                    "customer": customer_id,
                    "status": "open",
                    "limit": 100,
                    "expand": ["data.payments"],
                }
            ),
        )
        return [await self._invoice_info(inv.to_dict()) for inv in found.data]

    async def paid_invoices_since(self, since: datetime) -> list[InvoiceInfo]:
        params: dict[str, Any] = {
            "status": "paid",
            "created": {"gte": int(since.timestamp())},
            "limit": 100,
            "expand": ["data.payments"],
        }
        found: list[InvoiceInfo] = []
        for _ in range(MAX_PAGES):
            page = await self._do(
                "invoices.list",
                lambda params=dict(params): self._client.v1.invoices.list_async(params=params),
            )
            found.extend([await self._invoice_info(inv.to_dict()) for inv in page.data])
            if not page.has_more or not page.data:
                break
            params["starting_after"] = page.data[-1].id
        return found

    async def portal_url(self, customer_id: str, return_url: str) -> str:
        session = await self._do(
            "billing_portal.sessions.create",
            lambda: self._client.v1.billing_portal.sessions.create_async(
                params={"customer": customer_id, "return_url": return_url, "locale": "es"}
            ),
        )
        return str(session.url)

    # --- refunds and disputes ------------------------------------------------------

    async def refund(
        self, invoice_id: str, amount: int | None, reason: str, *, idempotency_key: str
    ) -> RefundInfo:
        invoice = await self.invoice(invoice_id)
        if invoice.payment_intent_id is None or invoice.status != "paid":
            raise PaymentsRejected("Stripe: la factura no está cobrada", code="not_paid")
        params: dict[str, Any] = {
            "payment_intent": invoice.payment_intent_id,
            "reason": "requested_by_customer",
            "metadata": {"invoice_id": invoice_id, "fichaitor_reason": reason},
        }
        if amount is not None:
            params["amount"] = amount
        options = self._options(idempotency_key)
        refund = await self._do(
            "refunds.create",
            lambda: self._client.v1.refunds.create_async(params=params, options=options),
        )
        return self._refund_info(refund.to_dict())

    async def refund_info(self, refund_id: str) -> RefundInfo:
        refund = await self._do(
            "refunds.retrieve", lambda: self._client.v1.refunds.retrieve_async(refund_id)
        )
        return self._refund_info(refund.to_dict())

    async def refunds_since(self, since: datetime) -> list[RefundInfo]:
        params: dict[str, Any] = {"created": {"gte": int(since.timestamp())}, "limit": 100}
        found: list[RefundInfo] = []
        for _ in range(MAX_PAGES):
            page = await self._do(
                "refunds.list",
                lambda params=dict(params): self._client.v1.refunds.list_async(params=params),
            )
            found.extend(self._refund_info(r.to_dict()) for r in page.data)
            if not page.has_more or not page.data:
                break
            params["starting_after"] = page.data[-1].id
        return found

    @staticmethod
    def _refund_info(data: Mapping[str, Any]) -> RefundInfo:
        return RefundInfo(
            id=str(data["id"]),
            invoice_id=(data.get("metadata") or {}).get("invoice_id"),
            payment_intent_id=_id(data.get("payment_intent")),
            charge_id=_id(data.get("charge")),
            amount=int(data.get("amount") or 0),
            status=data.get("status"),
        )

    async def dispute(self, dispute_id: str) -> DisputeInfo:
        found = await self._do(
            "disputes.retrieve", lambda: self._client.v1.disputes.retrieve_async(dispute_id)
        )
        data = found.to_dict()
        return DisputeInfo(
            id=str(data["id"]),
            charge_id=_id(data.get("charge")),
            payment_intent_id=_id(data.get("payment_intent")),
            amount=int(data.get("amount") or 0),
            status=str(data.get("status") or ""),
        )
