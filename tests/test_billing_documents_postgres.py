"""Integration: the legal invoices and the daily jobs of the billing process against
PostgreSQL (TEST database only; docs/admin-facturacion.md, steps 8 and 9).

Stripe and Odoo are always the fake doubles (app/adapters/stripe/fake.py,
app/adapters/odoo_invoicing/fake.py and tests/fake_odoo_json2.py behind the real Odoo
adapter): nothing leaves the process.

- A paid charge enqueues its invoice; the invoice is issued in Odoo once, even after a
  cut, is retried while Odoo is down and goes to review (without email) when Odoo
  refuses it or its total differs. The worker attaches its PDF to the email.
- Opening and closing a block enqueue their emails, once.
- An exceptional refund ends the right, stops the renewal and issues the credit note.
- The renewal notice freezes the price once per year; a seat without a profile gets the
  other notice and is cancelled two days before.
- The end of the beta: the email once, then Gratis for the oldest and the rest off.
- The daily reconciliation applies a charge whose webhook was lost and flags what Odoo
  has differently; the purge deletes only what is past six years.

Skipped unless the four URLs are set. Every statement expected to fail runs alone.
"""

import asyncio
import json
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest

from app.adapters.billing_store.documents import PostgresBillingDocuments
from app.adapters.billing_store.postgres import PostgresBillingStore
from app.adapters.billing_store.postgres_billing import PostgresBillingProcessStore
from app.adapters.mailer.fake import FakeMailer
from app.adapters.odoo_invoicing.fake import FakeInvoicing
from app.adapters.odoo_invoicing.real import OdooInvoicing, OdooSettings
from app.adapters.store.postgres import PostgresStore
from app.adapters.stripe.fake import FakePayments
from app.billing.enforce import Enforce
from app.billing.events import StripeEvents
from app.billing.invoices import IssueCreditNote, IssueInvoice
from app.billing.purge import Purge
from app.billing.reconcile import Reconcile
from app.billing.refunds import Refunds
from app.billing.renewal_notice import RenewalNotice
from app.billing.requests import BillingRequests
from app.domain.billing import PriceKey, TaxTreatment, Territory
from app.domain.jobs import Job, JobError
from app.domain.plans import Plan
from app.ports.billing_store import FiscalProfile
from app.ports.invoicing import InvoicingRejected, InvoicingUnavailable
from app.worker.send_mail import SendMail
from tests import fake_odoo_json2 as odoo

API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")
WORKER_URL = os.environ.get("FICHAITOR_TEST_WORKER_URL")
CONVERSATION_URL = os.environ.get("FICHAITOR_TEST_CONVERSATION_URL")

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL and WORKER_URL and CONVERSATION_URL),
    reason="sin base de datos de pruebas configurada",
)


def _role_url(user: str) -> str:
    parts = urlsplit(API_URL or "postgresql://x@127.0.0.1:5433/x_test")
    return urlunsplit(parts._replace(netloc=f"{user}:dev@{parts.hostname}:{parts.port}"))


BILLING_URL = os.environ.get("FICHAITOR_TEST_BILLING_URL") or _role_url("fichaitor_billing_dev")
PREFIX = "pytest-p9-"
JOB_PREFIX = "pytest-"
DENIED = "InsufficientPrivilegeError"
EMAIL = "facturas@laparra.es"
PROFILE = FiscalProfile(
    legal_name="Bar La Parra S.L.",
    tax_id="B12345674",
    tax_id_type="es_cif",
    tax_id_kind="cif",
    address_line1="Calle Mayor 1",
    postal_code="18001",
    city="Granada",
    country="ES",
    territory=Territory.PENINSULA_BALEARES,
    tax_treatment=TaxTreatment.IVA_21,
    invoice_email=EMAIL,
)
BILLING_KINDS = (
    "issue_invoice",
    "issue_credit_note",
    "billing_refund",
    "billing_reconcile",
    "billing_renewal_notice",
    "billing_enforce",
    "billing_purge",
)


def run(coro):
    return asyncio.run(coro)


async def owner(sql: str, *args):
    conn = await asyncpg.connect(OWNER_URL)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def attempt(url: str, sql: str, *args) -> str:
    conn = await asyncpg.connect(url)
    try:
        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute(sql, *args)
            return "ok"
        except asyncpg.PostgresError as exc:
            return type(exc).__name__
        finally:
            await tr.rollback()
    finally:
        await conn.close()


async def cleanup() -> None:
    mine = f"(SELECT id FROM account WHERE google_sub LIKE '{PREFIX}%')"
    await owner(f"DELETE FROM job WHERE kind LIKE '{JOB_PREFIX}%'")
    await owner("DELETE FROM job WHERE kind = ANY($1::text[])", list(BILLING_KINDS))
    await owner(
        "DELETE FROM job WHERE kind = 'send_mail' AND idempotency_key ~ "
        "'^send_mail:(factura|rectificativa|billing_blocked|billing_unblocked|renewal|beta_ending):'"
    )
    for sql in (
        f"DELETE FROM billing_document WHERE account_id IN {mine}",
        f"DELETE FROM billing_refund_request WHERE account_id IN {mine}",
        f"DELETE FROM account_billing_block WHERE account_id IN {mine}",
        f"DELETE FROM plan_entitlement WHERE account_id IN {mine}",
        f"DELETE FROM billing_charge WHERE account_id IN {mine}",
        f"DELETE FROM billing_seat WHERE account_id IN {mine}",
        f"DELETE FROM billing_request WHERE account_id IN {mine}",
        f"DELETE FROM billing_customer_history WHERE account_id IN {mine}",
        f"DELETE FROM billing_customer WHERE account_id IN {mine}",
        "DELETE FROM stripe_event WHERE id LIKE 'evt_pytestp9%'",
        f"UPDATE account SET selected_location_id = NULL WHERE id IN {mine}",
        f"DELETE FROM location_activation_log WHERE account_id IN {mine}",
        f"DELETE FROM location WHERE account_id IN {mine}",
        f"DELETE FROM account WHERE id IN {mine}",
    ):
        await owner(sql)
    await owner(
        "UPDATE billing_setting SET launch_at = NULL, beta_ends_at = NULL, beta_notice_for = NULL"
    )


@pytest.fixture(autouse=True)
def clean():
    run(cleanup())
    yield
    run(cleanup())


async def account_with(n_locations: int = 1) -> tuple[str, list[str]]:
    sub = f"{PREFIX}{uuid.uuid4().hex[:8]}"
    account = (
        await owner(
            "INSERT INTO account (google_sub, email) VALUES ($1, 'dueno@example.com')"
            " RETURNING id::text",
            sub,
        )
    )[0]["id"]
    locations = []
    for i in range(n_locations):
        location = f"loc_pytestnine{uuid.uuid4().hex[:8]}"
        await owner(
            "INSERT INTO location (id, account_id, gbp_account, gbp_location, title)"
            " VALUES ($1, $2::uuid, 'accounts/1', $3, $4)",
            location,
            account,
            f"locations/{sub}-{i}",
            f"Bar La Parra {i}",
        )
        locations.append(location)
    return account, locations


def real_odoo(server: odoo.FakeOdoo) -> OdooInvoicing:
    settings = OdooSettings(
        url=odoo.URL,
        db=odoo.DB,
        api_key=odoo.API_KEY,
        sale_journal_id=odoo.SALE_JOURNAL,
        payment_journal_id=odoo.STRIPE_JOURNAL,
        taxes={TaxTreatment.IVA_21: 101, TaxTreatment.NO_SUJETO: 102, TaxTreatment.ISP_UE: 103},
        products={Plan.PRO: 201, Plan.PRO_WHATSAPP: 202},
    )
    return OdooInvoicing(settings, httpx.AsyncClient(transport=server.transport()))


class Stage:
    def __init__(self, invoicing=None) -> None:
        self.api = PostgresStore(API_URL)
        self.billing = PostgresBillingStore(self.api, job_prefix=JOB_PREFIX)
        self.process = PostgresBillingProcessStore(BILLING_URL)
        self.payments = FakePayments()
        # Ids of this stage only: the fake numbers from 1, and billing_charge is shared.
        self.payments._counter = uuid.uuid4().int % 10**9
        self.invoicing = invoicing if invoicing is not None else FakeInvoicing()
        self.requests = BillingRequests(self.process, self.payments, "https://fichaitor.es")
        self.events = StripeEvents(self.process, self.payments)
        self.issue = IssueInvoice(self.process, self.invoicing)
        self.credit = IssueCreditNote(self.process, self.invoicing, self.payments)
        self._n = 0

    async def __aenter__(self) -> "Stage":
        await self.api.connect()
        await self.process.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.api.close()
        await self.process.close()

    async def event(self, type_: str, object_id: str) -> None:
        self._n += 1
        event_id = f"evt_pytestp9{uuid.uuid4().hex[:8]}{self._n}"
        assert await self.billing.record_stripe_event(
            event_id, type_, object_id, False, datetime.now(UTC)
        )
        await self.events(Job("j", "stripe_event", {"event_id": event_id}))

    async def buy(self, account: str, location: str, plan: Plan = Plan.PRO) -> str:
        """A seat bought and paid in Checkout. Returns the subscription id."""
        request_id = await self.billing.request_seat(account, location, plan)
        await self.requests(Job("j", "billing_request", {"request_id": request_id}))
        ready = await self.billing.get_request(account, request_id)
        session_id = ready.url.rsplit("/", 1)[1]
        sub = self.payments.complete_checkout(session_id)
        await self.event("checkout.session.completed", session_id)
        return sub.id


async def launched() -> None:
    await owner("UPDATE billing_setting SET launch_at = now() - interval '1 day'")


async def jobs(kind: str, account_key: str | None = None) -> list[dict]:
    rows = await owner(
        "SELECT payload, idempotency_key, max_attempts FROM job WHERE kind = $1"
        " ORDER BY created_at",
        kind,
    )
    return [
        {**dict(r), "payload": json.loads(r["payload"])}
        for r in rows
        if account_key is None or account_key in (r["idempotency_key"] or "")
    ]


async def document(stripe_id: str) -> asyncpg.Record:
    rows = await owner(
        "SELECT * FROM billing_document WHERE stripe_invoice_id = $1 OR stripe_refund_id = $1"
        " ORDER BY kind",
        stripe_id,
    )
    return rows[0] if rows else None


def issue_job(invoice_id: str, attempts: int = 1) -> Job:
    return Job("j", "issue_invoice", {"stripe_invoice_id": invoice_id}, attempts, 40)


# --- the invoice of a charge -----------------------------------------------------------


def test_a_paid_charge_is_invoiced_in_odoo_once_and_mailed_with_its_pdf():
    server = odoo.FakeOdoo()

    async def go():
        await launched()
        account, (bar,) = await account_with()
        async with Stage(real_odoo(server)) as s:
            await s.billing.save_profile(account, PROFILE)
            sub_id = await s.buy(account, bar)
            invoice_id = s.payments.seats[sub_id].subscription.latest_invoice_id
            await s.event("invoice.paid", invoice_id)  # again: one job only
            queued = await jobs("issue_invoice", invoice_id)
            await s.issue(issue_job(invoice_id))
            await s.issue(issue_job(invoice_id))  # again: nothing more in Odoo
            doc = await document(invoice_id)
            mails = await jobs("send_mail", str(doc["id"]))
            funnel = await owner(
                "SELECT kind, source FROM funnel_event WHERE kind IN ('checkout_started', 'paid')"
                " AND at > now() - interval '1 minute'"
            )
            listed = await s.billing.invoices(account)
            pdf = await s.billing.invoice_pdf(account, str(doc["id"]))
        pool = await asyncpg.create_pool(WORKER_URL, min_size=1, max_size=1)
        try:
            mailer = FakeMailer()
            sender = SendMail(None, mailer, "https://fichaitor.es", PostgresBillingDocuments(pool))
            await sender(Job("m", "send_mail", dict(mails[0]["payload"])))
        finally:
            await pool.close()
        return queued, doc, mails, funnel, listed, pdf, mailer.sent

    queued, doc, mails, funnel, listed, pdf, sent = run(go())
    assert len(queued) == 1 and queued[0]["max_attempts"] == 40
    assert doc["kind"] == "factura" and doc["state"] == "emitida"
    assert doc["number"].startswith("FTR/")
    assert (doc["amount_untaxed"], doc["amount_tax"], doc["amount_total"]) == (3500, 735, 4235)
    assert doc["pdf"] == odoo.PDF and doc["review_reason"] is None
    assert len(server.moves("out_invoice")) == 1 and len(server.payments()) == 1
    move = server.moves("out_invoice")[0]
    assert move["ref"].startswith("stripe:in_")
    assert len(mails) == 1
    payload = mails[0]["payload"]
    assert payload["template"] == "invoice_issued" and payload["to"] == EMAIL
    assert payload["ficha"] == "Bar La Parra 0" and payload["plan"] == "Pro"
    assert payload["importe"] == "42,35 €" and payload["numero"] == doc["number"]
    assert payload["periodo"].startswith("del ")
    assert "enlace" not in payload and "url" not in payload
    assert {("checkout_started", "pro"), ("paid", "pro")} <= {
        (r["kind"], r["source"]) for r in funnel
    }
    assert [(d.kind, d.number, d.amount_total) for d in listed] == [
        ("factura", doc["number"], 4235)
    ]
    assert pdf == odoo.PDF
    (mail,) = sent
    assert mail.to == EMAIL and mail.subject == f"Tu factura de Fichaitor {doc['number']}"
    assert [(a.filename, a.content) for a in mail.attachments] == [
        (doc["number"].replace("/", "-") + ".pdf", odoo.PDF)
    ]
    assert "https://fichaitor.es/app#facturacion" in mail.text


def test_odoo_down_is_retried_for_days_and_the_invoice_comes_out_after():
    async def go():
        await launched()
        account, (bar,) = await account_with()
        fake = FakeInvoicing()
        async with Stage(fake) as s:
            await s.billing.save_profile(account, PROFILE)
            sub_id = await s.buy(account, bar)
            invoice_id = s.payments.seats[sub_id].subscription.latest_invoice_id
            fake.fail_next(InvoicingUnavailable("caído", status=503))
            with pytest.raises(JobError) as error:
                await s.issue(issue_job(invoice_id, attempts=20))
            pending = await document(invoice_id)
            # The service does not depend on the invoice: the profile is active.
            plan = (await owner("SELECT plan FROM location WHERE id = $1", bar))[0]["plan"]
            await s.issue(issue_job(invoice_id, attempts=21))
            done = await document(invoice_id)
        return error.value.retry_in, pending, plan, done

    retry_in, pending, plan, done = run(go())
    assert retry_in == 7200  # waits grow up to two hours: 40 attempts, about three days
    assert pending["state"] == "pendiente" and plan == "pro"
    assert done["state"] == "emitida" and done["number"].startswith("FTR/")


def test_a_cut_after_odoo_posted_the_invoice_never_numbers_it_twice():
    server = odoo.FakeOdoo()
    server.cut_after.add(("account.move", "action_post"))

    async def go():
        await launched()
        account, (bar,) = await account_with()
        async with Stage(real_odoo(server)) as s:
            await s.billing.save_profile(account, PROFILE)
            sub_id = await s.buy(account, bar)
            invoice_id = s.payments.seats[sub_id].subscription.latest_invoice_id
            with pytest.raises(JobError):
                await s.issue(issue_job(invoice_id))
            await s.issue(issue_job(invoice_id, attempts=2))
            return await document(invoice_id)

    doc = run(go())
    assert doc["state"] == "emitida"
    moves = server.moves("out_invoice")
    assert len(moves) == 1 and moves[0]["name"] == doc["number"]
    assert len(server.payments()) == 1


@pytest.mark.parametrize("trouble", ["total", "rejected"])
def test_a_total_that_differs_or_a_refusal_goes_to_review_without_email(trouble):
    async def go():
        await launched()
        account, (bar,) = await account_with()
        fake = FakeInvoicing()
        if trouble == "total":
            fake.tax_offset = 1
        else:
            fake.fail_next(InvoicingRejected("no", status=422, code="odoo.exceptions.UserError"))
        async with Stage(fake) as s:
            await s.billing.save_profile(account, PROFILE)
            sub_id = await s.buy(account, bar)
            invoice_id = s.payments.seats[sub_id].subscription.latest_invoice_id
            await s.issue(issue_job(invoice_id))
            await s.issue(issue_job(invoice_id))  # nothing more: it waits for a person
            doc = await document(invoice_id)
            listed = await s.billing.invoices(account)
        mails = await jobs("send_mail", str(doc["id"]))
        return doc, mails, listed, fake.calls

    doc, mails, listed, calls = run(go())
    assert doc["state"] == "revision" and mails == [] and listed == []
    assert doc["review_reason"] == (
        "total_mismatch" if trouble == "total" else "odoo_exceptions_usererror"
    )
    assert calls == 1


def test_the_worker_reads_only_the_pdf_of_a_document_and_the_api_no_other_account():
    async def go():
        results = {
            "worker_pdf": await attempt(WORKER_URL, "SELECT id, state, pdf FROM billing_document"),
            "worker_amounts": await attempt(
                WORKER_URL, "SELECT number, amount_total FROM billing_document"
            ),
            "worker_write": await attempt(
                WORKER_URL, "UPDATE billing_document SET state = 'emitida' WHERE false"
            ),
            "billing_delete": await attempt(
                BILLING_URL, "DELETE FROM billing_document WHERE false"
            ),
            "api_purge": await attempt(API_URL, "SELECT billing_purge()"),
        }
        return results

    assert run(go()) == {
        "worker_pdf": "ok",
        "worker_amounts": DENIED,
        "worker_write": DENIED,
        "billing_delete": DENIED,
        "api_purge": DENIED,
    }


# --- blocks ----------------------------------------------------------------------------


def test_opening_and_closing_a_block_enqueue_their_emails_once():
    async def go():
        await launched()
        account, (bar,) = await account_with()
        async with Stage() as s:
            await s.billing.save_profile(account, PROFILE)
            sub_id = await s.buy(account, bar)
            failed = s.payments.renew(sub_id, card="declined")
            await s.event("invoice.payment_failed", failed.id)
            await s.event("invoice.payment_failed", failed.id)  # again
            blocked = await jobs("send_mail", f"billing_blocked:{failed.id}")
            s.payments.invoices[failed.id] = replace(
                failed,
                status="paid",
                amount_paid=failed.total,
                payment_intent_status="succeeded",
                charge_id="ch_pytest9",
                paid_at=datetime.now(UTC),
            )
            await s.event("invoice.paid", failed.id)
            unblocked = await jobs("send_mail", f"billing_unblocked:{failed.id}")
            invoice_jobs = await jobs("issue_invoice", failed.id)
        return blocked, unblocked, invoice_jobs

    blocked, unblocked, invoice_jobs = run(go())
    assert [j["payload"] for j in blocked] == [
        {
            "template": "billing_blocked",
            "to": EMAIL,
            "importe": "42,35 €",
            "ficha": "Bar La Parra 0",
            "motivo": "la tarjeta ha sido rechazada",
        }
    ]
    assert [j["payload"] for j in unblocked] == [
        {"template": "billing_unblocked", "to": EMAIL, "ficha": "Bar La Parra 0"}
    ]
    assert len(invoice_jobs) == 1  # the renewal paid late has its invoice too


# --- refunds ---------------------------------------------------------------------------


def test_an_exceptional_refund_ends_the_right_and_issues_the_credit_note():
    async def go():
        await launched()
        account, (bar,) = await account_with()
        async with Stage() as s:
            await s.billing.save_profile(account, PROFILE)
            sub_id = await s.buy(account, bar)
            invoice_id = s.payments.seats[sub_id].subscription.latest_invoice_id
            await s.issue(issue_job(invoice_id))
            request_id = (
                await owner(
                    "INSERT INTO billing_refund_request (account_id, stripe_invoice_id, reason,"
                    " ticket_ref, requested_by) VALUES ($1::uuid, $2, 'error_cobro', 'T-1', 'ana')"
                    " RETURNING id::text",
                    account,
                    invoice_id,
                )
            )[0]["id"]
            refunds = Refunds(s.process, s.payments)
            job = Job("r", "billing_refund", {"refund_request_id": request_id})
            await refunds(job)
            await refunds(job)  # again: nothing twice
            request = (
                await owner("SELECT * FROM billing_refund_request WHERE id = $1::uuid", request_id)
            )[0]
            rights = await owner(
                "SELECT ends_at <= now() AS ended FROM plan_entitlement WHERE external_ref = $1",
                invoice_id,
            )
            refund_id = request["stripe_refund_id"]
            credit_jobs = await jobs("issue_credit_note", refund_id)
            await s.credit(Job("c", "issue_credit_note", {"stripe_refund_id": refund_id}))
            credit = await document(refund_id)
            original = await document(invoice_id)
            mails = await jobs("send_mail", str(credit["id"]))
            listed = await s.billing.invoices(account)
            cancelled = s.payments.seats[sub_id].subscription.cancel_at_period_end
        return request, rights, credit_jobs, credit, original, mails, listed, cancelled, s

    request, rights, credit_jobs, credit, original, mails, listed, cancelled, s = run(go())
    assert request["state"] == "hecho" and request["stripe_refund_id"].startswith("re_")
    assert len(s.payments.refunds) == 1
    assert [r["ended"] for r in rights] == [True]
    assert cancelled is True
    assert len(credit_jobs) == 1
    assert credit["kind"] == "rectificativa" and credit["state"] == "emitida"
    assert credit["number"].startswith("RFTR/") and credit["amount_total"] == 4235
    payload = mails[0]["payload"]
    assert payload["template"] == "credit_note_issued"
    assert payload["numero_original"] == original["number"]
    assert payload["importe"] == "−42,35 €"
    assert {(d.kind, d.amount_total) for d in listed} == {
        ("factura", 4235),
        ("rectificativa", 4235),
    }


def test_a_refund_stripe_refuses_fails_and_no_credit_note_is_made():
    async def go():
        await launched()
        account, (bar,) = await account_with()
        async with Stage() as s:
            await s.billing.save_profile(account, PROFILE)
            sub_id = await s.buy(account, bar)
            invoice_id = s.payments.seats[sub_id].subscription.latest_invoice_id
            request_id = (
                await owner(
                    "INSERT INTO billing_refund_request (account_id, stripe_invoice_id, amount,"
                    " reason, ticket_ref, requested_by) VALUES ($1::uuid, $2, 999999,"
                    " 'decision_comercial', 'T-2', 'ana') RETURNING id::text",
                    account,
                    invoice_id,
                )
            )[0]["id"]
            await Refunds(s.process, s.payments)(
                Job("r", "billing_refund", {"refund_request_id": request_id})
            )
            state = (
                await owner(
                    "SELECT state FROM billing_refund_request WHERE id = $1::uuid", request_id
                )
            )[0]["state"]
            credit = await owner(
                "SELECT 1 FROM billing_document WHERE account_id = $1::uuid"
                " AND kind = 'rectificativa'",
                account,
            )
        return state, credit

    assert run(go()) == ("fallido", [])


# --- the renewal notice ---------------------------------------------------------------


def test_the_renewal_notice_freezes_the_price_once_and_a_seat_without_profile_is_cancelled():
    async def go():
        await launched()
        account, (bar, shop) = await account_with(2)
        async with Stage() as s:
            await s.billing.save_profile(account, PROFILE)
            first = await s.buy(account, bar)
            second = await s.buy(account, shop)
            end = s.payments.seats[first].subscription.current_period_end
            # The first profile is switched off: its seat is left without a profile.
            await owner("UPDATE location SET active_since = NULL WHERE id = $1", bar)
            notice = RenewalNotice(s.process, s.payments, clock=lambda: end - timedelta(days=20))
            await notice(Job("n", "billing_renewal_notice", {}))
            await notice(Job("n", "billing_renewal_notice", {}))  # again: no second email
            mails = await jobs("send_mail", "renewal")
            keys = {sub: s.payments.seats[sub].renewal_key for sub in (first, second)}
            seats = {x.stripe_subscription_id: x for x in await s.billing.seats(account)}
            two_days = RenewalNotice(s.process, s.payments, clock=lambda: end - timedelta(days=1))
            await two_days(Job("n", "billing_renewal_notice", {}))
            cancelled = {
                sub: s.payments.seats[sub].subscription.cancel_at_period_end
                for sub in (first, second)
            }
        return first, second, mails, keys, seats, cancelled

    first, second, mails, keys, seats, cancelled = run(go())
    by_template = {m["payload"]["template"]: m["payload"] for m in mails}
    assert len(mails) == 2
    # Only the second seat renews, so it is the first one of the account: full price. The
    # seat without a profile would come after it, with the 5 %.
    assert keys == {first: PriceKey.PRO_ADDITIONAL, second: PriceKey.PRO_FIRST}
    assert seats[second].next_price_key is PriceKey.PRO_FIRST
    assert by_template["renewal_notice"]["ficha"] == "Bar La Parra 1"
    assert by_template["renewal_notice"]["importe"] == "42,35 €"
    assert "ficha" not in by_template["renewal_notice_unassigned"]
    assert cancelled == {first: True, second: False}


# --- the end of the beta --------------------------------------------------------------


def test_the_end_of_the_beta_mails_once_then_keeps_the_oldest_in_gratis():
    async def go():
        account, (bar, shop) = await account_with(2)  # before the launch: beta rights
        for i, location in enumerate((bar, shop)):
            await owner(
                "UPDATE location SET active_since = now(), plan = 'pro' WHERE id = $1", location
            )
            await asyncio.sleep(0.01 * (i + 1))
        await owner(
            "UPDATE billing_setting SET launch_at = now() + interval '10 days',"
            " beta_ends_at = now() + interval '20 days'"
        )
        async with Stage() as s:
            enforce = Enforce(s.process, offer="")
            await enforce(Job("e", "billing_enforce", {}))
            await enforce(Job("e", "billing_enforce", {}))  # once per date
            mails = await jobs("send_mail", account)
            plans_before = await owner(
                "SELECT id, plan FROM location WHERE account_id = $1::uuid ORDER BY id", account
            )
            later = Enforce(s.process, clock=lambda: datetime.now(UTC) + timedelta(days=21))
            await later(Job("e", "billing_enforce", {}))
            plans_after = await owner(
                "SELECT id, plan, active_since IS NOT NULL AS active FROM location"
                " WHERE account_id = $1::uuid",
                account,
            )
            log = await owner(
                "SELECT location_id, event, reason FROM location_activation_log"
                " WHERE account_id = $1::uuid AND reason = 'billing'",
                account,
            )
        return bar, shop, mails, plans_before, plans_after, log

    bar, shop, mails, before, after, log = run(go())
    assert len(mails) == 1
    payload = mails[0]["payload"]
    assert payload["template"] == "beta_ending" and payload["oferta"] == ""
    assert payload["to"] == "dueno@example.com"
    assert {r["plan"] for r in before} == {"pro"}
    state = {r["id"]: (r["plan"], r["active"]) for r in after}
    assert state == {bar: ("gratis", True), shop: (None, False)}
    assert {(r["location_id"], r["event"]) for r in log} == {
        (bar, "plan_changed"),
        (shop, "deactivated"),
    }


# --- the daily reconciliation ---------------------------------------------------------


def test_the_reconciliation_applies_a_lost_webhook_and_flags_what_odoo_has_otherwise():
    async def go():
        await launched()
        account, (bar, shop) = await account_with(2)
        fake = FakeInvoicing()
        async with Stage(fake) as s:
            await s.billing.save_profile(account, PROFILE)
            first = await s.buy(account, bar)
            first_invoice = s.payments.seats[first].subscription.latest_invoice_id
            await s.issue(issue_job(first_invoice))
            # A second purchase whose webhooks never arrive.
            request_id = await s.billing.request_seat(account, shop, Plan.PRO)
            await s.requests(Job("j", "billing_request", {"request_id": request_id}))
            ready = await s.billing.get_request(account, request_id)
            lost = s.payments.complete_checkout(ready.url.rsplit("/", 1)[1])
            # Odoo has the first invoice with another number.
            fake.documents[first_invoice] = replace(fake.documents[first_invoice], number="X/1")
            reconcile = Reconcile(s.process, s.payments, fake, s.events)
            await reconcile(Job("r", "billing_reconcile", {}))
            report = reconcile.last
            shop_plan = (await owner("SELECT plan FROM location WHERE id = $1", shop))[0]["plan"]
            queued = await jobs("issue_invoice", lost.latest_invoice_id)
            flagged = await document(first_invoice)
        return report, shop_plan, queued, flagged

    report, shop_plan, queued, flagged = run(go())
    assert report.charges_recovered == 1 and shop_plan == "pro"
    assert len(queued) == 1
    assert flagged["state"] == "emitida" and flagged["review_reason"] == "odoo_number_mismatch"
    assert report.odoo_flagged == {"odoo_number_mismatch": 1}


# --- the purge -------------------------------------------------------------------------


def test_the_purge_deletes_only_what_is_past_six_years():
    async def go():
        account, _ = await account_with()
        await owner(
            "INSERT INTO billing_document (kind, account_id, stripe_invoice_id, state, created_at,"
            " issued_at) VALUES ('factura', $1::uuid, 'in_pytestold', 'emitida',"
            " now() - interval '7 years', now() - interval '7 years'),"
            " ('factura', $1::uuid, 'in_pytestnew', 'emitida', now() - interval '5 years',"
            " now() - interval '5 years')",
            account,
        )
        await owner(
            "INSERT INTO account_billing_block (account_id, reason, blocked_at, unblocked_at,"
            " unblocked_by) VALUES ($1::uuid, 'dispute', now() - interval '8 years',"
            " now() - interval '7 years', 'payment')",
            account,
        )
        await owner(
            "INSERT INTO account_billing_block (account_id, reason, blocked_at)"
            " VALUES ($1::uuid, 'payment_failed', now() - interval '7 years')",
            account,
        )
        store = PostgresBillingProcessStore(BILLING_URL)
        await store.connect()
        try:
            await Purge(store)(Job("p", "billing_purge", {}))
        finally:
            await store.close()
        docs = await owner(
            "SELECT stripe_invoice_id FROM billing_document WHERE account_id = $1::uuid", account
        )
        blocks = await owner(
            "SELECT reason FROM account_billing_block WHERE account_id = $1::uuid", account
        )
        return docs, blocks

    docs, blocks = run(go())
    assert [r["stripe_invoice_id"] for r in docs] == ["in_pytestnew"]
    assert [r["reason"] for r in blocks] == ["payment_failed"]  # an open block is never purged


def test_the_purge_function_fixes_its_search_path_and_only_billing_runs_it():
    rows = run(
        owner(
            """
            SELECT p.prosecdef, p.proconfig,
                   has_function_privilege('fichaitor_billing', p.oid, 'EXECUTE') AS billing,
                   has_function_privilege('fichaitor_worker', p.oid, 'EXECUTE') AS worker,
                   has_function_privilege('fichaitor_admin', p.oid, 'EXECUTE') AS admin
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public' AND p.proname = 'billing_purge'
            """
        )
    )
    (row,) = rows
    assert row["prosecdef"] is True
    assert "search_path=pg_catalog, public, pg_temp" in row["proconfig"]
    assert (row["billing"], row["worker"], row["admin"]) == (True, False, False)
