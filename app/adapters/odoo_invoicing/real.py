"""Legal invoices in Bitclick's Odoo over its JSON-2 API (decision 9.0.1).

Taken from Conectores ERP (`cerp_api/invoicing.py` and `invoices.py`), which charges with
Stripe and invoices in the same Odoo in production: `POST /json/2/<model>/<method>` with
`Authorization: bearer <API key>` and `X-Odoo-Database`; one partner per Stripe customer;
the invoice with the Stripe id as its `ref`, searched before it is created; `action_post`;
the payment in the «Stripe» journal with `account.payment.register`; the total checked
against Stripe; credit notes with `reversed_entry_id`; the PDF from the stored report or
the portal; and `setup_problems` at startup.

Adapted to Fichaitor:

- The `ref` is `stripe:<in_…>` or `stripe:<re_…>` (section 4.3) and every search is
  limited to the «Fichaitor» journal (FTR / RFTR).
- A call cut at any step is resumed: a draft is posted, a posted invoice without its
  payment gets it. Conectores ERP stopped at «found».
- The lines and the tax come from `billing.invoice_lines`, not from the Stripe invoice:
  one line, one product per plan, one Odoo tax per `TaxTreatment`, ids set by the owner.
- A total that does not match is not paid nor sent: it goes to review (section 4.3).
  Conectores ERP logged it and carried on.
- No fallback that drops the NIF when Odoo refuses it: a full invoice needs it.
- The customer's mail is ours (`send_mail`, with the PDF); Odoo does not send it.
- Errors and logs carry the model, the method and the status; never the key, the body
  sent nor Odoo's message.
"""

import base64
import binascii
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.domain.billing import TAX_PERCENT, TaxTreatment
from app.domain.plans import PAID_PLANS, Plan
from app.ports.invoicing import (
    CreditNoteRequest,
    DocumentKind,
    InvoiceCustomer,
    InvoiceRequest,
    InvoicingError,
    InvoicingRejected,
    InvoicingUnavailable,
    IssuedDocument,
)

log = logging.getLogger("fichaitor.billing.odoo")

REF_PREFIX = "stripe:"
TIMEOUT = 30.0
PDF_TIMEOUT = 60.0
MAX_PDF_BYTES = 10 * 1024 * 1024
_TEMPORARY = {408, 425, 429}
_MOVE_FIELDS = [
    "id",
    "name",
    "ref",
    "move_type",
    "partner_id",
    "state",
    "payment_state",
    "amount_untaxed",
    "amount_tax",
    "amount_total",
]
_KIND: Mapping[str, DocumentKind] = {"out_invoice": "invoice", "out_refund": "credit_note"}
_FISCAL_POSITIONS = ("nacional", "canarias", "extracomunitario", "intracomunitario")


@dataclass(frozen=True)
class OdooSettings:
    """`billing.env` of the server (section 9.0.1). Ids are those of the owner's Odoo."""

    url: str
    db: str
    api_key: str = field(repr=False)
    sale_journal_id: int  # «Fichaitor», prefix FTR, credit notes RFTR
    payment_journal_id: int  # bank journal «Stripe»
    taxes: Mapping[TaxTreatment, int]
    products: Mapping[Plan, int]
    fiscal_positions: Mapping[str, int] = field(default_factory=dict)
    verifactu_field: str | None = None  # the state field of the VERI*FACTU module

    def __post_init__(self) -> None:
        parts = urlsplit(self.url)
        local = parts.hostname in ("localhost", "127.0.0.1")
        if parts.scheme != "https" and not (parts.scheme == "http" and local):
            raise ValueError("FICHAITOR_ODOO_URL must be https")
        if not self.api_key:
            raise ValueError("FICHAITOR_ODOO_API_KEY is empty")
        object.__setattr__(self, "url", self.url.rstrip("/"))

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "OdooSettings":
        """Read and check the variables. The error names the variable, never its value."""

        def number(name: str) -> int:
            raw = env.get(name, "")
            if not raw.strip().isdigit():
                raise ValueError(f"{name} must be the id of a record")
            return int(raw)

        for name in ("FICHAITOR_ODOO_URL", "FICHAITOR_ODOO_DB", "FICHAITOR_ODOO_API_KEY"):
            if not env.get(name):
                raise ValueError(f"{name} is not set")
        positions = {
            name: number(f"FICHAITOR_ODOO_FPOS_{name.upper()}")
            for name in _FISCAL_POSITIONS
            if env.get(f"FICHAITOR_ODOO_FPOS_{name.upper()}")
        }
        return cls(
            url=env["FICHAITOR_ODOO_URL"],
            db=env["FICHAITOR_ODOO_DB"],
            api_key=env["FICHAITOR_ODOO_API_KEY"],
            sale_journal_id=number("FICHAITOR_ODOO_JOURNAL_ID"),
            payment_journal_id=number("FICHAITOR_ODOO_PAYMENT_JOURNAL_ID"),
            taxes={t: number(f"FICHAITOR_ODOO_TAX_{t.value.upper()}") for t in TaxTreatment},
            products={p: number(f"FICHAITOR_ODOO_PRODUCT_{p.value.upper()}") for p in PAID_PLANS},
            fiscal_positions=positions,
            verifactu_field=env.get("FICHAITOR_ODOO_VERIFACTU_FIELD") or None,
        )


def odoo_ref(stripe_id: str) -> str:
    return REF_PREFIX + stripe_id


def to_cents(amount: Any) -> int:
    """An Odoo monetary float as cents, without binary surprises (0.1 + 0.2)."""
    value = Decimal(str(amount or 0)) * 100
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def to_amount(cents: int) -> float:
    return float(Decimal(cents) / 100)


def _tax(base: int, percent: int) -> int:
    return (base * percent + 50) // 100  # per line and half up, as Stripe and billing.py


def credit_base(amount: int, percent: int) -> int | None:
    """The base whose total, tax rounded per line, is exactly `amount`; None if none is."""
    estimate = amount * 100 // (100 + percent)
    for base in range(max(estimate - 2, 0), estimate + 3):
        if base + _tax(base, percent) == amount:
            return base
    return None


_ACCESS_TOKEN = re.compile(r"access_token=[^&\s\"']+")


class _AccessTokenRedactor(logging.Filter):
    """httpx logs each URL at INFO; the portal link of a PDF carries the access token
    of the invoice. It is rewritten before any handler sees it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and any("access_token=" in str(a) for a in record.args):
            record.args = tuple(
                _ACCESS_TOKEN.sub("access_token=***", str(a)) if "access_token=" in str(a) else a
                for a in record.args
            )
        if isinstance(record.msg, str) and "access_token=" in record.msg:
            record.msg = _ACCESS_TOKEN.sub("access_token=***", record.msg)
        return True


def redact_access_tokens_in_logs() -> None:
    logger = logging.getLogger("httpx")
    if not any(isinstance(f, _AccessTokenRedactor) for f in logger.filters):
        logger.addFilter(_AccessTokenRedactor())


def like_literal(value: str) -> str:
    """A value for `=like` that stands for itself (Conectores ERP)."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class OdooInvoicing:
    """The `Invoicing` port over JSON-2. `client` lets the tests pass a mock transport."""

    def __init__(self, settings: OdooSettings, client: httpx.AsyncClient | None = None) -> None:
        self._s = settings
        self._http = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False)
        redact_access_tokens_in_logs()

    def __repr__(self) -> str:
        return f"OdooInvoicing(url={self._s.url!r}, db={self._s.db!r})"

    async def close(self) -> None:
        await self._http.aclose()

    # --- transport -----------------------------------------------------------------

    async def call(self, model: str, method: str, **payload: Any) -> Any:
        what = f"{model}.{method}"
        try:
            resp = await self._http.post(
                f"{self._s.url}/json/2/{model}/{method}",
                headers={
                    "Authorization": f"bearer {self._s.api_key}",
                    "X-Odoo-Database": self._s.db,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        except httpx.TimeoutException:
            log.warning("odoo %s: time out", what)
            raise InvoicingUnavailable(f"{what}: tiempo agotado", code="timeout") from None
        except httpx.HTTPError as exc:
            # `from None`: the httpx error holds the request, with the key in its headers.
            log.warning("odoo %s: %s", what, type(exc).__name__)
            raise InvoicingUnavailable(f"{what}: error de red", code="network") from None
        status = resp.status_code
        if status >= 400:
            code = _odoo_error_name(resp)
            log.warning("odoo %s: HTTP %s %s", what, status, code or "")
            if status >= 500 or status in _TEMPORARY:
                raise InvoicingUnavailable(f"{what}: HTTP {status}", status=status, code=code)
            raise InvoicingRejected(f"{what}: HTTP {status}", status=status, code=code)
        try:
            return resp.json()
        except ValueError:
            raise InvoicingUnavailable(f"{what}: respuesta no JSON", status=status) from None

    async def _one(self, model: str, domain: list[Any], fields: list[str]) -> dict[str, Any] | None:
        rows = await self.call(model, "search_read", domain=domain, fields=fields, limit=1)
        return rows[0] if rows else None

    # --- documents -----------------------------------------------------------------

    def _fields(self) -> list[str]:
        extra = [self._s.verifactu_field] if self._s.verifactu_field else []
        return _MOVE_FIELDS + extra

    def _document(
        self, row: Mapping[str, Any], expected: int | None = None, created: bool = False
    ) -> IssuedDocument:
        ref = str(row.get("ref") or "")
        verifactu = row.get(self._s.verifactu_field) if self._s.verifactu_field else None
        name = row.get("name")
        return IssuedDocument(
            move_id=int(row["id"]),
            kind=_KIND.get(str(row.get("move_type")), "invoice"),
            stripe_id=ref.removeprefix(REF_PREFIX),
            number=str(name) if name and name != "/" else None,
            state=str(row.get("state") or ""),
            payment_state=str(row["payment_state"]) if row.get("payment_state") else None,
            amount_untaxed=to_cents(row.get("amount_untaxed")),
            amount_tax=to_cents(row.get("amount_tax")),
            amount_total=to_cents(row.get("amount_total")),
            expected_total=expected,
            verifactu_state=str(verifactu) if verifactu else None,
            created=created,
        )

    async def _move_by_ref(self, stripe_id: str, move_type: str) -> dict[str, Any] | None:
        return await self._one(
            "account.move",
            [
                ["ref", "=", odoo_ref(stripe_id)],
                ["journal_id", "=", self._s.sale_journal_id],
                ["move_type", "=", move_type],
            ],
            self._fields(),
        )

    async def _read_move(self, move_id: int) -> dict[str, Any]:
        rows = await self.call("account.move", "read", ids=[move_id], fields=self._fields())
        if not rows:
            raise InvoicingRejected(f"account.move {move_id} no existe", code="missing")
        return rows[0]

    async def find(self, stripe_id: str) -> IssuedDocument | None:
        row = await self._one(
            "account.move",
            [
                ["ref", "=", odoo_ref(stripe_id)],
                ["journal_id", "=", self._s.sale_journal_id],
                ["move_type", "in", list(_KIND)],
            ],
            self._fields(),
        )
        return self._document(row) if row else None

    async def issued_since(self, day: date) -> list[IssuedDocument]:
        rows = await self.call(
            "account.move",
            "search_read",
            domain=[
                ["ref", "=like", like_literal(REF_PREFIX) + "%"],
                ["journal_id", "=", self._s.sale_journal_id],
                ["move_type", "in", list(_KIND)],
                ["invoice_date", ">=", day.isoformat()],
            ],
            fields=self._fields(),
            order="invoice_date, id",
        )
        return [self._document(r) for r in rows]

    async def issue_invoice(self, request: InvoiceRequest) -> IssuedDocument:
        draft = request.draft
        if draft.amount_total != request.paid_cents:
            # The lines would not be what was charged: nothing goes to Odoo.
            raise InvoicingRejected(
                f"{request.stripe_invoice_id}: las líneas no suman lo cobrado",
                code="draft_total_mismatch",
            )
        row = await self._move_by_ref(request.stripe_invoice_id, "out_invoice")
        created = row is None
        if row is None:
            partner = await self._partner(request.customer)
            move_id = await self._create_invoice(request, partner)
        else:
            move_id = int(row["id"])
        return await self._finish(
            move_id, request.paid_cents, request.operation_date, request.payment_reference, created
        )

    async def _finish(
        self, move_id: int, expected: int, day: date, payment_reference: str, created: bool
    ) -> IssuedDocument:
        """From any step on: post, check the total, pay. Each step is skipped if done."""
        row = await self._read_move(move_id)
        if row.get("state") == "cancel":
            raise InvoicingRejected(f"account.move {move_id} anulado en Odoo", code="cancelled")
        if row.get("state") == "draft":
            await self.call("account.move", "action_post", ids=[move_id])
            row = await self._read_move(move_id)
        document = self._document(row, expected, created)
        if not document.total_ok:
            log.error(
                "odoo move %s: total %s distinto de Stripe %s; queda en revisión",
                move_id,
                document.amount_total,
                expected,
            )
            return document
        if not document.paid:
            await self._register_payment(move_id, day, payment_reference)
            document = self._document(await self._read_move(move_id), expected, created)
        return document

    async def _create_invoice(self, request: InvoiceRequest, partner: int) -> int:
        draft = request.draft
        lines = []
        for line in draft.lines:
            plan = Plan(line.product)
            treatment = TaxTreatment(line.tax_rate)
            lines.append(
                [
                    0,
                    0,
                    {
                        "product_id": self._s.products[plan],
                        "name": line.description,
                        "quantity": line.quantity,
                        "price_unit": to_amount(line.price_unit_cents),
                        "tax_ids": [[6, 0, [self._s.taxes[treatment]]]],
                    },
                ]
            )
        notes = [n for n in (draft.legal_note,) if n]
        if request.operation_date != request.invoice_date:
            notes.append(f"Fecha de la operación: {request.operation_date:%d/%m/%Y}.")
        notes.append("Pagada con tarjeta mediante Stripe.")
        values: dict[str, Any] = {
            "move_type": "out_invoice",
            "journal_id": self._s.sale_journal_id,
            "partner_id": partner,
            "ref": odoo_ref(request.stripe_invoice_id),
            "invoice_date": request.invoice_date.isoformat(),
            "invoice_line_ids": lines,
            "narration": " ".join(notes),
        }
        position = self._s.fiscal_positions.get(draft.fiscal_position)
        if position:
            values["fiscal_position_id"] = position
        created = await self.call("account.move", "create", vals_list=[values])
        move_id = int(created[0])
        log.info("odoo move %s creado para %s", move_id, request.stripe_invoice_id)
        return move_id

    async def _register_payment(
        self, move_id: int, day: date, reference: str, *, outbound: bool = False
    ) -> None:
        context = {"active_model": "account.move", "active_ids": [move_id]}
        values = {
            "journal_id": self._s.payment_journal_id,
            "payment_date": day.isoformat(),
            "communication": reference,
        }
        wizard = await self.call(
            "account.payment.register", "create", vals_list=[values], context=context
        )
        await self.call(
            "account.payment.register", "action_create_payments", ids=wizard, context=context
        )
        log.info("odoo move %s: %s registrado", move_id, "reembolso" if outbound else "cobro")

    async def _partner(self, customer: InvoiceCustomer) -> int:
        """Ours by `ref` (`stripe:<cus_…>`), updated with the data of today; else one with
        the same NIF, used as it is; else a new company."""
        own = await self._one(
            "res.partner", [["ref", "=", odoo_ref(customer.stripe_customer_id)]], ["id"]
        )
        if own is not None:
            values = await self._partner_values(customer)
            await self.call("res.partner", "write", ids=[int(own["id"])], vals=values)
            return int(own["id"])
        vats = _vat_variants(customer)
        same_vat = await self._one(
            "res.partner", [["vat", "in", vats], ["is_company", "=", True]], ["id"]
        )
        if same_vat is not None:
            return int(same_vat["id"])
        values = await self._partner_values(customer)
        values |= {"ref": odoo_ref(customer.stripe_customer_id), "is_company": True}
        created = await self.call("res.partner", "create", vals_list=[values])
        return int(created[0])

    async def _partner_values(self, customer: InvoiceCustomer) -> dict[str, Any]:
        country = await self._one("res.country", [["code", "=", customer.country]], ["id"])
        if country is None:
            raise InvoicingRejected("país no encontrado en Odoo", code="country_missing")
        return {
            "name": customer.legal_name,
            "vat": _vat_variants(customer)[-1],
            "email": customer.email or False,
            "street": customer.line1,
            "street2": customer.line2 or False,
            "zip": customer.postal_code or False,
            "city": customer.city,
            "country_id": int(country["id"]),
        }

    async def issue_credit_note(self, request: CreditNoteRequest) -> IssuedDocument:
        row = await self._move_by_ref(request.stripe_refund_id, "out_refund")
        created = row is None
        if row is None:
            move_id = await self._create_credit_note(request)
        else:
            move_id = int(row["id"])
        return await self._finish_credit_note(move_id, request, created)

    async def _create_credit_note(self, request: CreditNoteRequest) -> int:
        original = await self._move_by_ref(request.stripe_invoice_id, "out_invoice")
        if original is None or original.get("state") != "posted":
            # Its invoice is still pending: the credit note waits for it.
            raise InvoicingUnavailable(
                f"{request.stripe_refund_id}: falta la factura original", code="original_missing"
            )
        original_total = to_cents(original.get("amount_total"))
        if not 0 < request.amount_cents <= original_total:
            raise InvoicingRejected(
                f"{request.stripe_refund_id}: importe fuera de la factura", code="amount_invalid"
            )
        lines = await self.call(
            "account.move.line",
            "search_read",
            domain=[["move_id", "=", int(original["id"])], ["display_type", "=", "product"]],
            fields=["product_id", "tax_ids", "price_subtotal", "price_total"],
            limit=1,
        )
        if not lines:
            raise InvoicingRejected("la factura original no tiene líneas", code="no_lines")
        line = lines[0]
        subtotal, total = to_cents(line["price_subtotal"]), to_cents(line["price_total"])
        if request.amount_cents == original_total:
            base = subtotal
        else:
            percent = round((total - subtotal) * 100 / subtotal) if subtotal else 0
            base = credit_base(request.amount_cents, percent)
            if base is None:
                raise InvoicingRejected("importe sin base exacta", code="amount_invalid")
        number = original.get("name") or ""
        partner_id = original.get("partner_id")
        values = {
            "move_type": "out_refund",
            "journal_id": self._s.sale_journal_id,
            "partner_id": partner_id[0] if isinstance(partner_id, list) else partner_id,
            "ref": odoo_ref(request.stripe_refund_id),
            "invoice_date": request.credit_date.isoformat(),
            "reversed_entry_id": int(original["id"]),
            "narration": f"Rectifica la factura {number}. Motivo: {request.reason}.",
            "invoice_line_ids": [
                [
                    0,
                    0,
                    {
                        "product_id": line["product_id"][0],
                        "name": f"Rectificación de la factura {number}",
                        "quantity": 1,
                        "price_unit": to_amount(base),
                        "tax_ids": [[6, 0, list(line["tax_ids"])]],
                    },
                ]
            ],
        }
        created = await self.call("account.move", "create", vals_list=[values])
        return int(created[0])

    async def _finish_credit_note(
        self, move_id: int, request: CreditNoteRequest, created: bool
    ) -> IssuedDocument:
        row = await self._read_move(move_id)
        if row.get("state") == "cancel":
            raise InvoicingRejected(f"account.move {move_id} anulado en Odoo", code="cancelled")
        if row.get("state") == "draft":
            await self.call("account.move", "action_post", ids=[move_id])
            row = await self._read_move(move_id)
        document = self._document(row, request.amount_cents, created)
        if document.total_ok and not document.paid:
            await self._register_payment(
                move_id, request.credit_date, request.stripe_refund_id, outbound=True
            )
            document = self._document(await self._read_move(move_id), request.amount_cents, created)
        return document

    # --- PDF -----------------------------------------------------------------------

    async def pdf(self, move_id: int) -> bytes:
        """The report Odoo stored for the document; else Odoo renders it through the
        portal link, whose access token never leaves this process (Conectores ERP)."""
        read = await self.call(
            "account.move", "read", ids=[move_id], fields=["invoice_pdf_report_id"]
        )
        attachment = read[0].get("invoice_pdf_report_id") if read else None
        if isinstance(attachment, list) and attachment:
            rows = await self.call(
                "ir.attachment", "read", ids=[int(attachment[0])], fields=["datas"]
            )
            if rows and rows[0].get("datas"):
                try:
                    data = base64.b64decode(rows[0]["datas"], validate=False)
                except (binascii.Error, ValueError) as exc:
                    raise InvoicingUnavailable("PDF ilegible", code="bad_pdf") from exc
                return _checked_pdf(data)
        path = await self.call("account.move", "get_portal_url", ids=[move_id])
        if not isinstance(path, str) or not path.startswith("/my/"):
            raise InvoicingRejected("enlace del portal inesperado", code="bad_portal_url")
        sep = "&" if "?" in path else "?"
        try:
            resp = await self._http.get(
                f"{self._s.url}{path}{sep}report_type=pdf&download=true", timeout=PDF_TIMEOUT
            )
        except httpx.HTTPError:
            raise InvoicingUnavailable("PDF: error de red", code="network") from None
        if resp.status_code != 200:
            error = InvoicingUnavailable if resp.status_code >= 500 else InvoicingRejected
            raise error(f"PDF: HTTP {resp.status_code}", status=resp.status_code)
        return _checked_pdf(resp.content)

    # --- setup ---------------------------------------------------------------------

    async def setup_problems(self) -> list[str]:
        """Each tax a sale tax of the rate Stripe charges; the journals of their type; the
        products; the fiscal positions; and the tax rounding per line, as Stripe. Never
        raises for a wrong setup; an unreachable Odoo is a problem too."""
        try:
            return await self._setup_problems()
        except InvoicingError as exc:
            return [f"Odoo no responde como se espera ({exc})"]

    async def _setup_problems(self) -> list[str]:
        s = self._s
        problems: list[str] = []
        taxes = {
            int(t["id"]): t
            for t in await self.call(
                "account.tax",
                "search_read",
                domain=[["id", "in", list(s.taxes.values())]],
                fields=["amount", "amount_type", "type_tax_use", "price_include"],
            )
        }
        for treatment, tax_id in s.taxes.items():
            name = f"FICHAITOR_ODOO_TAX_{treatment.value.upper()}"
            tax = taxes.get(tax_id)
            rate = TAX_PERCENT[treatment]
            if tax is None:
                problems.append(f"{name}: el impuesto {tax_id} no existe")
            elif (
                tax.get("type_tax_use") != "sale"
                or tax.get("amount_type") != "percent"
                or abs(float(tax.get("amount") or 0) - rate) > 0.001
                or tax.get("price_include")
            ):
                problems.append(f"{name}: el impuesto {tax_id} no es de venta al {rate} % sin IVA")
        journals = {
            int(j["id"]): j
            for j in await self.call(
                "account.journal",
                "search_read",
                domain=[["id", "in", [s.sale_journal_id, s.payment_journal_id]]],
                fields=["type", "company_id", "refund_sequence"],
            )
        }
        sale = journals.get(s.sale_journal_id)
        if sale is None or sale.get("type") != "sale":
            problems.append(f"FICHAITOR_ODOO_JOURNAL_ID: {s.sale_journal_id} no es de ventas")
        elif not sale.get("refund_sequence"):
            problems.append("FICHAITOR_ODOO_JOURNAL_ID: sin secuencia propia de rectificativas")
        payment = journals.get(s.payment_journal_id)
        if payment is None or payment.get("type") != "bank":
            problems.append(
                f"FICHAITOR_ODOO_PAYMENT_JOURNAL_ID: {s.payment_journal_id} no es de banco"
            )
        products = await self.call(
            "product.product",
            "search_read",
            domain=[["id", "in", list(s.products.values())]],
            fields=["id"],
        )
        found = {int(p["id"]) for p in products}
        for plan, product_id in s.products.items():
            if product_id not in found:
                problems.append(
                    f"FICHAITOR_ODOO_PRODUCT_{plan.value.upper()}: el producto {product_id} "
                    "no existe"
                )
        if s.fiscal_positions:
            positions = await self.call(
                "account.fiscal.position",
                "search_read",
                domain=[["id", "in", list(s.fiscal_positions.values())]],
                fields=["id"],
            )
            have = {int(p["id"]) for p in positions}
            for name, position_id in s.fiscal_positions.items():
                if position_id not in have:
                    problems.append(f"FICHAITOR_ODOO_FPOS_{name.upper()}: {position_id} no existe")
        company = sale.get("company_id") if sale else None
        if isinstance(company, list) and company:
            rows = await self.call(
                "res.company",
                "read",
                ids=[int(company[0])],
                fields=["tax_calculation_rounding_method"],
            )
            method = rows[0].get("tax_calculation_rounding_method") if rows else None
            if method != "round_per_line":
                problems.append("Odoo redondea los impuestos de forma global, no por línea")
        return problems


def _vat_variants(customer: InvoiceCustomer) -> list[str]:
    """How Odoo may hold the NIF: as typed and with the country prefix. The last one is
    the one written. Outside Spain it is the one we keep, already normalised (a tax id
    outside the EU keeps its separators: it is printed as the customer's country writes
    it)."""
    if customer.country == "ES":
        vat = customer.tax_id.replace(" ", "").upper()
        return [vat] if vat.startswith("ES") else [vat, "ES" + vat]
    return [customer.tax_id.strip().upper()]


def _odoo_error_name(resp: httpx.Response) -> str | None:
    """The exception class Odoo names (`odoo.exceptions.ValidationError`), never its
    message, which may repeat the customer's data."""
    try:
        body = resp.json()
    except ValueError:
        return None
    name = body.get("name") if isinstance(body, dict) else None
    if isinstance(name, str) and len(name) <= 80 and name.replace(".", "").isidentifier():
        return name
    return None


def _checked_pdf(data: bytes) -> bytes:
    if not data.startswith(b"%PDF") or len(data) > MAX_PDF_BYTES:
        raise InvoicingRejected("no es un PDF o es demasiado grande", code="bad_pdf")
    return data
