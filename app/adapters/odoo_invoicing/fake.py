"""Fake Odoo invoicing for tests and local development: documents in memory, numbered
FTR/<year>/00001 and RFTR/<year>/00001 as the «Fichaitor» journal would.

Keeps the contract of `app.ports.invoicing`: idempotent by Stripe id, the total checked
against what Stripe charged, nothing paid when it does not match, and the credit note
waiting for its invoice. `fail_next` makes the next calls fail, to try the retries;
`tax_offset` makes Odoo add up a different total, to try the review."""

from dataclasses import replace
from datetime import date

from app.ports.invoicing import (
    CreditNoteRequest,
    InvoiceRequest,
    InvoicingError,
    InvoicingRejected,
    InvoicingUnavailable,
    IssuedDocument,
)

FAKE_PDF = b"%PDF-1.7\n% factura de prueba de Fichaitor\n%%EOF\n"


class FakeInvoicing:
    def __init__(self) -> None:
        self.documents: dict[str, IssuedDocument] = {}  # by Stripe id
        self.dates: dict[str, date] = {}
        self.partners: dict[str, str] = {}  # Stripe customer -> tax id
        self.failures: list[InvoicingError] = []
        self.tax_offset = 0  # cents Odoo adds to the tax of the next documents
        self.calls = 0
        self._sequence: dict[str, int] = {}
        self._next_id = 1

    def fail_next(self, *errors: InvoicingError) -> None:
        self.failures.extend(errors)

    def _maybe_fail(self) -> None:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)

    def _number(self, prefix: str, day: date) -> str:
        key = f"{prefix}/{day.year}"
        self._sequence[key] = self._sequence.get(key, 0) + 1
        return f"{key}/{self._sequence[key]:05d}"

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def issue_invoice(self, request: InvoiceRequest) -> IssuedDocument:
        self._maybe_fail()
        draft = request.draft
        if draft.amount_total != request.paid_cents:
            raise InvoicingRejected("las líneas no suman lo cobrado", code="draft_total_mismatch")
        found = self.documents.get(request.stripe_invoice_id)
        if found is not None:
            return replace(found, created=False)
        self.partners[request.customer.stripe_customer_id] = request.customer.tax_id
        tax = draft.amount_tax + self.tax_offset
        total = draft.amount_untaxed + tax
        ok = total == request.paid_cents
        document = IssuedDocument(
            move_id=self._new_id(),
            kind="invoice",
            stripe_id=request.stripe_invoice_id,
            number=self._number("FTR", request.invoice_date),
            state="posted",
            payment_state="paid" if ok else "not_paid",
            amount_untaxed=draft.amount_untaxed,
            amount_tax=tax,
            amount_total=total,
            expected_total=request.paid_cents,
            created=True,
        )
        self.documents[request.stripe_invoice_id] = document
        self.dates[request.stripe_invoice_id] = request.invoice_date
        return document

    async def issue_credit_note(self, request: CreditNoteRequest) -> IssuedDocument:
        self._maybe_fail()
        found = self.documents.get(request.stripe_refund_id)
        if found is not None:
            return replace(found, created=False)
        original = self.documents.get(request.stripe_invoice_id)
        if original is None or original.kind != "invoice":
            raise InvoicingUnavailable("falta la factura original", code="original_missing")
        if not 0 < request.amount_cents <= original.amount_total:
            raise InvoicingRejected("importe fuera de la factura", code="amount_invalid")
        if request.amount_cents == original.amount_total:
            base = original.amount_untaxed
        else:
            base = round(request.amount_cents * original.amount_untaxed / original.amount_total)
        document = IssuedDocument(
            move_id=self._new_id(),
            kind="credit_note",
            stripe_id=request.stripe_refund_id,
            number=self._number("RFTR", request.credit_date),
            state="posted",
            payment_state="paid",
            amount_untaxed=base,
            amount_tax=request.amount_cents - base,
            amount_total=request.amount_cents,
            expected_total=request.amount_cents,
            created=True,
        )
        self.documents[request.stripe_refund_id] = document
        self.dates[request.stripe_refund_id] = request.credit_date
        return document

    async def find(self, stripe_id: str) -> IssuedDocument | None:
        self._maybe_fail()
        found = self.documents.get(stripe_id)
        return replace(found, expected_total=None, created=False) if found else None

    async def issued_since(self, day: date) -> list[IssuedDocument]:
        self._maybe_fail()
        return [
            replace(d, expected_total=None, created=False)
            for key, d in self.documents.items()
            if self.dates[key] >= day
        ]

    async def pdf(self, move_id: int) -> bytes:
        self._maybe_fail()
        if not any(d.move_id == move_id for d in self.documents.values()):
            raise InvoicingRejected(f"account.move {move_id} no existe", code="missing")
        return FAKE_PDF

    async def setup_problems(self) -> list[str]:
        return []
