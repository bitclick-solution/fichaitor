"""A tiny Odoo behind JSON-2, for the tests of the Odoo invoicing adapter: enough of
`account.move`, `res.partner` and the payment wizard to issue, number, pay and credit an
invoice, with the taxes rounded per line. Served through `httpx.MockTransport`; nothing
leaves the process.

`cut_after` applies a call and then loses its answer (a time out), as a cut between Odoo
and us would; `fail` answers a status, or raises a network error, without applying."""

import base64
import json
from typing import Any

import httpx

API_KEY = "odoo-test-key-0123456789abcdef"  # gitleaks:allow (clave falsa de pruebas)
DB = "bitclick_test"
URL = "https://erp.example.test"
SALE_JOURNAL = 30
STRIPE_JOURNAL = 31
TAXES = {101: 21, 102: 0, 103: 0, 104: 0}  # iva_21, no_sujeto, isp_ue, extracomunitario
PRODUCTS = {201: "Fichaitor Pro", 202: "Fichaitor Pro WhatsApp"}
COUNTRIES = {"ES": 68, "FR": 75, "US": 233}
PDF = b"%PDF-1.7\n% invoice\n%%EOF\n"


def _matches(row: dict[str, Any], domain: list[Any]) -> bool:
    for field, op, value in domain:
        have = row.get(field)
        if isinstance(have, list) and field.endswith("_id"):
            have = have[0] if have else None
        if op == "=" and have != value:
            return False
        if op == "in" and have not in value:
            return False
        if op == "=like" and not str(have or "").startswith(value.rstrip("%").replace("\\", "")):
            return False
        if op == ">=" and not (have and str(have) >= str(value)):
            return False
    return True


class FakeOdoo:
    def __init__(self) -> None:
        self.tables: dict[str, dict[int, dict[str, Any]]] = {
            "res.partner": {},
            "account.move": {},
            "account.move.line": {},
            "account.payment.register": {},
            "account.payment": {},
            "ir.attachment": {},
        }
        self.tax_rates = dict(TAXES)  # what Odoo applies; change it to break the totals
        self.sequences: dict[str, int] = {}
        self.calls: list[tuple[str, str]] = []
        self.requests: list[httpx.Request] = []
        self.cut_after: set[tuple[str, str]] = set()
        self.fail: dict[tuple[str, str], list[int | Exception]] = {}
        self.rounding = "round_per_line"
        self._next = 1000

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # --- plumbing ------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, content=PDF)
        parts = request.url.path.split("/")  # /json/2/<model>/<method>
        model, method = parts[3], parts[4]
        key = (model, method)
        self.calls.append(key)
        if request.headers.get("authorization") != f"bearer {API_KEY}":
            return httpx.Response(401, json={"name": "werkzeug.exceptions.Unauthorized"})
        pending = self.fail.get(key)
        if pending:
            outcome = pending.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return httpx.Response(
                outcome,
                json={"name": "odoo.exceptions.ValidationError", "message": f"bad {API_KEY}"},
            )
        payload = json.loads(request.content or b"{}")
        result = getattr(self, "_" + method)(model, payload)
        if key in self.cut_after:
            self.cut_after.discard(key)
            raise httpx.ReadTimeout("cut", request=request)
        return httpx.Response(200, json=result)

    def _id(self) -> int:
        self._next += 1
        return self._next

    def _rows(self, model: str) -> dict[int, dict[str, Any]]:
        if model in self.tables:
            return self.tables[model]
        if model == "account.tax":
            return {
                i: {
                    "id": i,
                    "amount": float(r),
                    "amount_type": "percent",
                    "type_tax_use": "sale",
                    "price_include": False,
                }
                for i, r in TAXES.items()
            }
        if model == "account.journal":
            return {
                SALE_JOURNAL: {
                    "id": SALE_JOURNAL,
                    "type": "sale",
                    "company_id": [1, "Bitclick"],
                    "refund_sequence": True,
                },
                STRIPE_JOURNAL: {"id": STRIPE_JOURNAL, "type": "bank", "company_id": [1, "B"]},
            }
        if model == "product.product":
            return {i: {"id": i, "name": n} for i, n in PRODUCTS.items()}
        if model == "res.country":
            return {i: {"id": i, "code": c} for c, i in COUNTRIES.items()}
        if model == "res.company":
            return {1: {"id": 1, "tax_calculation_rounding_method": self.rounding}}
        if model == "account.fiscal.position":
            return {401: {"id": 401}, 402: {"id": 402}, 403: {"id": 403}}
        raise KeyError(model)

    # --- generic ORM ---------------------------------------------------------------

    def _search_read(self, model: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows = [r for r in self._rows(model).values() if _matches(r, payload.get("domain", []))]
        rows.sort(key=lambda r: r["id"])
        if payload.get("limit"):
            rows = rows[: payload["limit"]]
        fields = payload.get("fields") or []
        return [{f: r.get(f) for f in ["id", *fields]} for r in rows]

    def _read(self, model: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows = self._rows(model)
        fields = payload.get("fields") or []
        return [{f: rows[i].get(f) for f in ["id", *fields]} for i in payload["ids"] if i in rows]

    def _write(self, model: str, payload: dict[str, Any]) -> bool:
        for i in payload["ids"]:
            self.tables[model][i].update(payload["vals"])
        return True

    def _create(self, model: str, payload: dict[str, Any]) -> list[int]:
        made = []
        for values in payload["vals_list"]:
            new = self._id()
            row = {"id": new, **values}
            if model == "account.move":
                row = self._new_move(new, values)
            if model == "account.payment.register":
                row["context"] = payload.get("context")
            self.tables[model][new] = row
            made.append(new)
        return made

    # --- accounting ----------------------------------------------------------------

    def _new_move(self, move_id: int, values: dict[str, Any]) -> dict[str, Any]:
        untaxed = tax = 0
        for _, _, line in values.get("invoice_line_ids", []):
            line_id = self._id()
            base = round(line["price_unit"] * line["quantity"] * 100)
            tax_ids = line["tax_ids"][0][2]
            line_tax = sum((base * self.tax_rates[t] + 50) // 100 for t in tax_ids)
            if self.rounding != "round_per_line":
                line_tax += 1  # a global rounding that differs by a cent
            untaxed, tax = untaxed + base, tax + line_tax
            self.tables["account.move.line"][line_id] = {
                "id": line_id,
                "move_id": [move_id, "/"],
                "display_type": "product",
                "product_id": [line["product_id"], PRODUCTS[line["product_id"]]],
                "tax_ids": tax_ids,
                "price_subtotal": base / 100,
                "price_total": (base + line_tax) / 100,
            }
        partner = values.get("partner_id")
        return {
            **{k: v for k, v in values.items() if k != "invoice_line_ids"},
            "id": move_id,
            "name": "/",
            "state": "draft",
            "payment_state": "not_paid",
            "partner_id": [partner, "Cliente"] if partner else False,
            "amount_untaxed": untaxed / 100,
            "amount_tax": tax / 100,
            "amount_total": (untaxed + tax) / 100,
            "invoice_pdf_report_id": False,
        }

    def _action_post(self, model: str, payload: dict[str, Any]) -> bool:
        for i in payload["ids"]:
            move = self.tables["account.move"][i]
            if move["state"] != "draft":
                raise AssertionError("posted twice")
            prefix = "FTR" if move["move_type"] == "out_invoice" else "RFTR"
            year = str(move["invoice_date"])[:4]
            self.sequences[prefix] = self.sequences.get(prefix, 0) + 1
            move["name"] = f"{prefix}/{year}/{self.sequences[prefix]:05d}"
            move["state"] = "posted"
        return True

    def _action_create_payments(self, model: str, payload: dict[str, Any]) -> bool:
        for wizard_id in payload["ids"]:
            wizard = self.tables["account.payment.register"][wizard_id]
            for move_id in wizard["context"]["active_ids"]:
                move = self.tables["account.move"][move_id]
                if move["payment_state"] == "paid":
                    raise AssertionError("paid twice")
                move["payment_state"] = "paid"
                payment = self._id()
                self.tables["account.payment"][payment] = {
                    "id": payment,
                    "move_id": move_id,
                    "journal_id": wizard["journal_id"],
                    "memo": wizard.get("communication"),
                }
        return True

    def _get_portal_url(self, model: str, payload: dict[str, Any]) -> str:
        return f"/my/invoices/{payload['ids'][0]}?access_token=secret-token"

    def stored_pdf(self, move_id: int) -> None:
        attachment = self._id()
        self.tables["ir.attachment"][attachment] = {
            "id": attachment,
            "datas": base64.b64encode(PDF).decode(),
        }
        self.tables["account.move"][move_id]["invoice_pdf_report_id"] = [attachment, "pdf"]

    # --- what the tests look at ----------------------------------------------------

    def moves(self, move_type: str | None = None) -> list[dict[str, Any]]:
        return [
            m
            for m in self.tables["account.move"].values()
            if move_type is None or m["move_type"] == move_type
        ]

    def payments(self) -> list[dict[str, Any]]:
        return list(self.tables["account.payment"].values())
