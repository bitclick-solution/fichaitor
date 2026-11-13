"""Invoicing port: the legal invoice in Bitclick's Odoo for what Stripe charged
(docs/admin-facturacion.md, sections 2.2, 2.5, 4.3 and 9.0.1).

Odoo is reached through its JSON-2 API with the API key of a user with invoicing rights
only; no module is installed in Odoo (decision 9.0.1). Rules every implementation keeps:

- Idempotent by `ref`: the Stripe invoice (`in_…`) for an invoice, the Stripe refund
  (`re_…`) for a credit note. A call repeated after a cut, at any step, finds the
  document by its `ref` and finishes it: never two numbers for one charge. The caller
  serialises calls with the same `ref` (`billing_document` unique per Stripe invoice and
  the job key `issue_invoice:<in_…>`).
- The total of Odoo must equal what Stripe charged, to the cent. If it does not, the
  document stays posted and unpaid in Odoo and comes back with `total_ok` False: it goes
  to review, without mail to the customer. A posted invoice is never deleted; an error is
  corrected with a credit note.
- Credit notes only for the exceptional refunds of an administrator (P2).
- Errors carry a status and a short code, never the API key, a request body or Odoo's
  message (which may echo the customer's data).
"""

from dataclasses import dataclass, field
from datetime import date
from typing import ClassVar, Literal, Protocol

from app.domain.billing import InvoiceDraft, TaxTreatment


class InvoicingError(Exception):
    """Odoo did not do what was asked. `retryable` says whether the same call may work
    later (section 4.5: `issue_invoice` retries with backoff for about three days)."""

    retryable: ClassVar[bool] = True

    def __init__(self, message: str = "", *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class InvoicingUnavailable(InvoicingError):
    """Temporary: network error, time out, 408, 429 or 5xx; or the invoice a credit note
    refers to is not in Odoo yet."""

    retryable = True


class InvoicingRejected(InvoicingError):
    """Permanent: Odoo refused the request (a 4xx: validation, access, missing record), or
    what was asked cannot be right (the lines do not add up to what Stripe charged). The
    document goes to review; retrying will not help."""

    retryable = False


@dataclass(frozen=True)
class InvoiceCustomer:
    """The customer as the invoice must show it (art. 6 of RD 1619/2012). Personal fields
    stay out of `repr`."""

    stripe_customer_id: str
    legal_name: str = field(repr=False)
    tax_id: str = field(repr=False)  # as TaxProfile.tax_id: no ES prefix for Spain
    country: str  # ISO 3166-1 alpha-2
    postal_code: str | None = field(repr=False)  # None only outside the EU
    city: str = field(repr=False)
    line1: str = field(repr=False)
    line2: str | None = field(default=None, repr=False)
    email: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class InvoiceRequest:
    """The invoice of a charge. `draft` comes from `billing.invoice_lines`; `paid_cents` is
    Stripe's `amount_paid`, and both must agree before anything reaches Odoo."""

    stripe_invoice_id: str  # in_…
    customer: InvoiceCustomer
    draft: InvoiceDraft
    treatment: TaxTreatment
    invoice_date: date  # today in Europe/Madrid
    operation_date: date  # the day Stripe charged
    paid_cents: int
    payment_reference: str  # the ch_… or pi_… of Stripe, kept with the payment


@dataclass(frozen=True)
class CreditNoteRequest:
    """The credit note of an exceptional refund, partial or whole."""

    stripe_refund_id: str  # re_…
    stripe_invoice_id: str  # the in_… of the invoice it corrects
    amount_cents: int  # refunded, VAT included
    reason: str  # error_cobro, decision_comercial, cambio_tercero or disputa
    credit_date: date


DocumentKind = Literal["invoice", "credit_note"]


@dataclass(frozen=True)
class IssuedDocument:
    """A document in Odoo. Amounts in cents. `expected_total` is what Stripe charged or
    refunded (None when only read); `created` is False when a retry found it."""

    move_id: int
    kind: DocumentKind
    stripe_id: str  # the in_… or re_… it belongs to
    number: str | None  # FTR/2027/00001; None while a draft
    state: str  # draft, posted, cancel
    payment_state: str | None  # not_paid, in_payment, paid, partial, reversed
    amount_untaxed: int
    amount_tax: int
    amount_total: int
    expected_total: int | None = None
    verifactu_state: str | None = None
    created: bool = False

    @property
    def total_ok(self) -> bool:
        return self.expected_total is None or self.amount_total == self.expected_total

    @property
    def paid(self) -> bool:
        return self.payment_state in ("paid", "in_payment")


class Invoicing(Protocol):
    async def issue_invoice(self, request: InvoiceRequest) -> IssuedDocument:
        """Find or create the customer, create the invoice, post it (Odoo numbers it and
        VERI*FACTU records it), check its total and register the payment in the «Stripe»
        journal. Resumes a call cut at any step. With `total_ok` False the payment is not
        registered."""
        ...

    async def issue_credit_note(self, request: CreditNoteRequest) -> IssuedDocument:
        """The credit note of a refund, against the original invoice, posted, checked and
        paid back from the «Stripe» journal. InvoicingUnavailable if the original invoice
        is not in Odoo yet."""
        ...

    async def find(self, stripe_id: str) -> IssuedDocument | None:
        """The document of a Stripe invoice or refund, if Odoo has it."""
        ...

    async def issued_since(self, day: date) -> list[IssuedDocument]:
        """Documents of Fichaitor dated `day` or later, for the daily reconciliation."""
        ...

    async def pdf(self, move_id: int) -> bytes:
        """The PDF of a posted document, as Odoo prints it."""
        ...

    async def setup_problems(self) -> list[str]:
        """What is wrong in Odoo's setup (journals, taxes, products, rounding), checked at
        startup. Empty when it is ready. Never raises for a wrong setup."""
        ...
