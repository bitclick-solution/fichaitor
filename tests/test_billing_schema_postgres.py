"""Integration: db/schema_billing.sql and the paywall against PostgreSQL (TEST database
only; docs/admin-facturacion.md, steps 2, 6 and 8).

- The API does not write entitlements, seats, charges nor blocks, cannot resolve a
  request and cannot set the Stripe customer.
- Nobody lengthens an entitlement or dates it in the past; a paid one is only the
  billing process's, and the billing process writes no other kind.
- The billing process activates the profile of a seat just paid (reason 'billing' in the
  log), blocks and unblocks; it reads no Google connection, review nor waiting list.
- No new role has TEMPORARY; every new function fixes its search_path.
- A seat request per profile is atomic: requests at once get one row.
- A ready request only carries an https URL of Stripe.

Skipped unless the four URLs are set; the billing role's URL defaults to the dev user of
db/dev-init.sql (FICHAITOR_TEST_BILLING_URL, also checked by tests/conftest.py). Every
statement expected to fail runs alone, or carries WHERE false.
"""

import asyncio
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from app.adapters.billing_store.postgres import PostgresBillingStore
from app.adapters.billing_store.postgres_billing import PostgresBillingProcessStore
from app.adapters.store.postgres import PostgresStore
from app.adapters.stripe.fake import FakePayments
from app.billing.events import StripeEvents
from app.billing.requests import BillingRequests
from app.domain.billing import TaxTreatment, Territory
from app.domain.jobs import Job
from app.domain.plans import Plan
from app.ports.billing_store import FiscalProfile, SeatPending

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
PREFIX = "pytest-bill-"
JOB_PREFIX = "pytest-"
DENIED = "InsufficientPrivilegeError"
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
    invoice_email="facturas@laparra.es",
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
    """'ok' or the name of the error, each statement alone and rolled back."""
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
    for sql in (
        f"DELETE FROM account_billing_block WHERE account_id IN {mine}",
        f"DELETE FROM plan_entitlement WHERE account_id IN {mine}",
        f"DELETE FROM billing_charge WHERE account_id IN {mine}",
        f"DELETE FROM billing_seat WHERE account_id IN {mine}",
        f"DELETE FROM billing_request WHERE account_id IN {mine}",
        f"DELETE FROM billing_customer_history WHERE account_id IN {mine}",
        f"DELETE FROM billing_customer WHERE account_id IN {mine}",
        "DELETE FROM stripe_event WHERE id LIKE 'evt_pytest%'",
        f"UPDATE account SET selected_location_id = NULL WHERE id IN {mine}",
        f"DELETE FROM location_activation_log WHERE account_id IN {mine}",
        f"DELETE FROM location WHERE account_id IN {mine}",
        f"DELETE FROM account WHERE id IN {mine}",
    ):
        await owner(sql)


@pytest.fixture(autouse=True)
def clean():
    run(cleanup())
    yield
    run(cleanup())
    run(owner("UPDATE billing_setting SET launch_at = NULL, beta_ends_at = NULL"))


async def account_with(n_locations: int = 1) -> tuple[str, list[str]]:
    sub = f"{PREFIX}{uuid.uuid4().hex[:8]}"
    account = (
        await owner(
            "INSERT INTO account (google_sub, email) VALUES ($1, 'x@x') RETURNING id::text", sub
        )
    )[0]["id"]
    locations = []
    for i in range(n_locations):
        location = f"loc_pytestbill{uuid.uuid4().hex[:8]}"
        await owner(
            "INSERT INTO location (id, account_id, gbp_account, gbp_location, title)"
            " VALUES ($1, $2::uuid, 'accounts/1', $3, 'Bar')",
            location,
            account,
            f"locations/{sub}-{i}",
        )
        locations.append(location)
    return account, locations


# --- roles and functions ------------------------------------------------------------


def test_no_new_role_nor_its_users_can_create_temporary_tables():
    rows = run(
        owner(
            """
            SELECT r.rolname, has_database_privilege(r.rolname, current_database(), 'TEMP') AS temp
            FROM pg_roles r
            WHERE r.rolname IN ('fichaitor_billing', 'fichaitor_admin', 'fichaitor_billing_dev',
                                'fichaitor_admin_dev')
            """
        )
    )
    assert len(rows) == 4
    assert {r["rolname"]: r["temp"] for r in rows} == dict.fromkeys(
        ("fichaitor_billing", "fichaitor_admin", "fichaitor_billing_dev", "fichaitor_admin_dev"),
        False,
    )


def test_every_new_function_fixes_its_search_path():
    rows = run(
        owner(
            """
            SELECT p.proname, p.proconfig FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace AND n.nspname = 'public'
            WHERE p.proname IN ('fichaitor_account_blocked', 'fichaitor_session_in',
                                'billing_customer_guard', 'billing_customer_history_write',
                                'billing_request_guard', 'plan_entitlement_guard',
                                'account_beta_entitlement', 'free_allowance_guard',
                                'location_activation_audit')
            """
        )
    )
    assert len(rows) == 9
    for row in rows:
        assert "search_path=pg_catalog, public, pg_temp" in (row["proconfig"] or []), row["proname"]


# --- what the API cannot do ----------------------------------------------------------


def test_the_api_writes_no_entitlement_seat_charge_nor_block():
    async def go():
        account, (location,) = await account_with()
        results = {}
        for name, sql in {
            "entitlement": (
                "INSERT INTO plan_entitlement (account_id, plan, quantity, starts_at, source,"
                " external_ref, seat_id) SELECT $1::uuid, 'pro', 1, now(), 'pago', 'in_x',"
                " 'seat_00000000' WHERE false"
            ),
            "seat": (
                "INSERT INTO billing_seat (id, account_id, plan) SELECT 'seat_00000000',"
                " $1::uuid, 'pro' WHERE false"
            ),
            "charge": (
                "INSERT INTO billing_charge (stripe_invoice_id, account_id)"
                " SELECT 'in_x', $1::uuid WHERE false"
            ),
            "block": (
                "INSERT INTO account_billing_block (account_id, reason)"
                " SELECT $1::uuid, 'dispute' WHERE false"
            ),
            "resolve": (
                "UPDATE billing_request SET state = 'lista' WHERE account_id = $1::uuid AND false"
            ),
            "customer_id": (
                "UPDATE billing_customer SET stripe_customer_id = 'cus_x'"
                " WHERE account_id = $1::uuid AND false"
            ),
            "event_payload": (
                "UPDATE stripe_event SET outcome = 'processed' WHERE false AND $1::uuid IS NULL"
            ),
        }.items():
            results[name] = await attempt(API_URL, sql, account)
        return results

    assert run(go()) == dict.fromkeys(
        ("entitlement", "seat", "charge", "block", "resolve", "customer_id", "event_payload"),
        DENIED,
    )


def test_the_api_cannot_create_a_request_already_resolved():
    async def go():
        account, _ = await account_with()
        return await attempt(
            API_URL,
            "INSERT INTO billing_request (account_id, kind, state, url)"
            " VALUES ($1::uuid, 'pay', 'lista', 'https://checkout.stripe.com/x')",
            account,
        )

    assert run(go()) == DENIED


# --- entitlements ---------------------------------------------------------------------


def test_nobody_lengthens_an_entitlement_nor_dates_it_in_the_past():
    async def go():
        account, _ = await account_with()
        await owner(
            "INSERT INTO billing_seat (id, account_id, plan) VALUES ('seat_0000aaaa0001', $1::uuid,"
            " 'pro')",
            account,
        )
        results = {}
        results["billing_backdated"] = await attempt(
            BILLING_URL,
            "INSERT INTO plan_entitlement (account_id, plan, quantity, starts_at, ends_at, source,"
            " external_ref, seat_id) VALUES ($1::uuid, 'pro', 1, now() - interval '1 day',"
            " now() + interval '1 year', 'pago', 'in_pytest_back', 'seat_0000aaaa0001')",
            account,
        )
        results["billing_manual"] = await attempt(
            BILLING_URL,
            "INSERT INTO plan_entitlement (account_id, plan, quantity, starts_at, ends_at, source,"
            " granted_by) VALUES ($1::uuid, 'pro', 1, now(), now() + interval '1 year', 'manual',"
            " 'yo')",
            account,
        )
        results["billing_paid"] = await attempt(
            BILLING_URL,
            "INSERT INTO plan_entitlement (account_id, plan, quantity, starts_at, ends_at, source,"
            " external_ref, seat_id) VALUES ($1::uuid, 'pro', 1, now(), now() + interval '1 year',"
            " 'pago', 'in_pytest_ok', 'seat_0000aaaa0001')",
            account,
        )
        conn = await asyncpg.connect(BILLING_URL)
        try:
            await conn.execute(
                "INSERT INTO plan_entitlement (account_id, plan, quantity, starts_at, ends_at,"
                " source, external_ref, seat_id) VALUES ($1::uuid, 'pro', 1, now(),"
                " now() + interval '1 year', 'pago', 'in_pytest_real', 'seat_0000aaaa0001')",
                account,
            )
        finally:
            await conn.close()
        for name, sql in {
            "lengthen": "UPDATE plan_entitlement SET ends_at = ends_at + interval '1 day'"
            " WHERE external_ref = 'in_pytest_real'",
            "unend": "UPDATE plan_entitlement SET ends_at = NULL"
            " WHERE external_ref = 'in_pytest_real'",
            "quantity": "UPDATE plan_entitlement SET quantity = 5"
            " WHERE external_ref = 'in_pytest_real'",
            "shorten": "UPDATE plan_entitlement SET ends_at = ends_at - interval '1 day'"
            " WHERE external_ref = 'in_pytest_real'",
        }.items():
            results[name] = await attempt(BILLING_URL, sql)
        results["api"] = await attempt(
            API_URL, "UPDATE plan_entitlement SET ends_at = now() WHERE false"
        )
        return results

    assert run(go()) == {
        "billing_backdated": "CheckViolationError",
        "billing_manual": "InsufficientPrivilegeError",
        "billing_paid": "ok",
        "lengthen": "CheckViolationError",
        "unend": "CheckViolationError",
        "quantity": DENIED,  # not even a privilege on the column
        "shorten": "ok",
        "api": DENIED,
    }


def test_the_billing_process_reads_nothing_of_google_reviews_nor_the_waiting_list():
    async def go():
        return [
            await attempt(BILLING_URL, f"SELECT 1 FROM {table} WHERE false")
            for table in (
                "google_connection",
                "review",
                "waitlist_entry",
                "channel_identity",
                "profile_summary",
            )
        ]

    assert run(go()) == [DENIED] * 5


def test_the_beta_entitlement_is_only_given_before_the_launch():
    async def go():
        await owner("UPDATE billing_setting SET launch_at = now() - interval '1 day'")
        launched, _ = await account_with(0)
        await owner(
            "UPDATE billing_setting SET launch_at = now() + interval '30 days',"
            " beta_ends_at = now() + interval '40 days'"
        )
        beta, _ = await account_with(0)
        return (
            await owner(
                "SELECT count(*) AS n FROM plan_entitlement WHERE account_id = $1::uuid", launched
            ),
            await owner(
                "SELECT ends_at > now() + interval '39 days' AS ends FROM plan_entitlement"
                " WHERE account_id = $1::uuid",
                beta,
            ),
        )

    launched, beta = run(go())
    assert launched[0]["n"] == 0
    assert [r["ends"] for r in beta] == [True]


# --- the paywall, end to end on PostgreSQL ------------------------------------------


class Stage:
    def __init__(self) -> None:
        self.api = PostgresStore(API_URL)
        self.billing = PostgresBillingStore(self.api, job_prefix=JOB_PREFIX)
        self.process = PostgresBillingProcessStore(BILLING_URL)
        self.payments = FakePayments()
        self.requests = BillingRequests(self.process, self.payments, "https://fichaitor.es")
        self.events = StripeEvents(self.process, self.payments)
        self._n = 0

    async def __aenter__(self) -> "Stage":
        await self.api.connect()
        await self.process.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.api.close()
        await self.process.close()

    async def handle_request(self, request_id: str) -> None:
        await self.requests(Job("j", "billing_request", {"request_id": request_id}))

    async def event(self, type_: str, object_id: str) -> None:
        self._n += 1
        event_id = f"evt_pytest{uuid.uuid4().hex[:8]}{self._n}"
        assert await self.billing.record_stripe_event(
            event_id, type_, object_id, False, datetime.now(UTC)
        )
        assert not await self.billing.record_stripe_event(
            event_id, type_, object_id, False, datetime.now(UTC)
        )
        await self.events(Job("j", "stripe_event", {"event_id": event_id}))


def test_a_paid_seat_activates_its_profile_and_a_failed_renewal_blocks_the_account():
    async def go():
        await owner("UPDATE billing_setting SET launch_at = now() - interval '1 day'")
        account, (bar,) = await account_with()
        async with Stage() as s:
            await s.billing.save_profile(account, PROFILE)
            request_id = await s.billing.request_seat(account, bar, Plan.PRO)
            jobs = await owner(
                "SELECT kind, payload, idempotency_key FROM job WHERE idempotency_key = $1",
                f"{JOB_PREFIX}billing_request:{request_id}",
            )
            assert len(jobs) == 1
            with pytest.raises(SeatPending) as pending:
                await s.billing.request_seat(account, bar, Plan.PRO)
            assert pending.value.request_id == request_id
            await s.handle_request(request_id)
            ready = await s.billing.get_request(account, request_id)
            assert ready.state == "lista" and ready.url.startswith("https://checkout.stripe.com/")
            session_id = ready.url.rsplit("/", 1)[1]
            sub = s.payments.complete_checkout(session_id)
            await s.event("checkout.session.completed", session_id)
            await s.event("invoice.paid", sub.latest_invoice_id)  # again, idempotent
            located = await owner("SELECT plan FROM location WHERE id = $1", bar)
            log = await owner(
                "SELECT event, plan, reason, written_by FROM location_activation_log"
                " WHERE location_id = $1 ORDER BY id",
                bar,
            )
            rights = await owner(
                "SELECT source, external_ref, seat_id FROM plan_entitlement"
                " WHERE account_id = $1::uuid AND source = 'pago'",
                account,
            )
            seats = await s.billing.seats(account)
            invoice = s.payments.renew(sub.id, card="declined")
            await s.event("invoice.payment_failed", invoice.id)
            conn = await asyncpg.connect(API_URL)
            try:
                blocked = await conn.fetchval("SELECT fichaitor_account_blocked($1::uuid)", account)
            finally:
                await conn.close()
            s.payments.invoices[invoice.id] = replace(
                invoice, status="paid", payment_intent_status="succeeded"
            )
            await s.event("invoice.paid", invoice.id)
            conn = await asyncpg.connect(WORKER_URL)
            try:
                after = await conn.fetchval("SELECT fichaitor_account_blocked($1::uuid)", account)
            finally:
                await conn.close()
        return bar, located, log, rights, seats, blocked, after

    bar, located, log, rights, seats, blocked, after = run(go())
    assert located[0]["plan"] == "pro"
    assert [(r["event"], r["plan"], r["reason"]) for r in log] == [("activated", "pro", "billing")]
    assert log[0]["written_by"] == "fichaitor_billing_dev"
    assert len(rights) == 1 and rights[0]["external_ref"].startswith("in_")
    assert [(x.location_id, x.status.value) for x in seats] == [(bar, "active")]
    assert blocked is True and after is False


def test_seat_requests_at_once_for_the_same_profile_create_one():
    async def go():
        await owner("UPDATE billing_setting SET launch_at = now() - interval '1 day'")
        account, (bar,) = await account_with()
        async with Stage() as s:
            await s.billing.save_profile(account, PROFILE)
            results = await asyncio.gather(
                *(s.billing.request_seat(account, bar, Plan.PRO) for _ in range(6)),
                return_exceptions=True,
            )
        rows = await owner(
            "SELECT count(*) AS n FROM billing_request WHERE account_id = $1::uuid", account
        )
        return results, rows[0]["n"]

    results, n = run(go())
    created = [r for r in results if isinstance(r, str)]
    assert len(created) == 1 and n == 1
    assert all(isinstance(r, SeatPending) for r in results if not isinstance(r, str))


def test_a_ready_request_only_carries_a_stripe_url():
    async def go():
        account, _ = await account_with()
        async with Stage() as s:
            request_id = await s.billing.request(account, "portal")
        return (
            await attempt(
                BILLING_URL,
                "UPDATE billing_request SET state = 'lista', url = 'https://fichaitor.es.evil/x'"
                " WHERE id = $1::uuid",
                request_id,
            ),
            await attempt(
                BILLING_URL,
                "UPDATE billing_request SET state = 'lista',"
                " url = 'https://billing.stripe.com/p/session/x' WHERE id = $1::uuid",
                request_id,
            ),
        )

    assert run(go()) == ("CheckViolationError", "ok")


def test_the_worker_deletes_old_requests_and_processed_events():
    async def go():
        account, _ = await account_with()
        return [
            await attempt(
                WORKER_URL,
                "DELETE FROM billing_request WHERE created_at <= now() - interval '30 days'"
                " AND false",
            ),
            await attempt(
                WORKER_URL,
                "DELETE FROM stripe_event WHERE processed_at IS NOT NULL AND false",
            ),
            await attempt(WORKER_URL, "DELETE FROM billing_seat WHERE false"),
        ]

    assert run(go()) == ["ok", "ok", DENIED]


def test_in_the_beta_no_plan_is_pro_and_a_free_profile_moves_to_pro_with_the_beta_seat():
    async def go():
        account, (a, b, c) = await account_with(3)
        store = PostgresStore(API_URL)
        await store.connect()
        try:
            beta = await store.activate(account, a)  # launch_at empty: the beta
            free = await store.activate(account, b, Plan.GRATIS)
            upgraded = await store.activate(account, b, Plan.PRO)  # Gratis -> Pro
            await owner("UPDATE billing_setting SET launch_at = now() - interval '1 day'")
            charging = await store.activate(account, c)  # once charging: Gratis first
        finally:
            await store.close()
        return beta, free, upgraded, charging

    assert run(go()) == (Plan.PRO, Plan.GRATIS, Plan.PRO, Plan.GRATIS)


# --- customers outside the EU (decision 9.0, 12) -------------------------------------

WORLD_PROFILE = replace(
    PROFILE,
    legal_name="Taco Loco LLC",
    tax_id="12-3456789",
    tax_id_type=None,
    tax_id_kind="foreign",
    postal_code=None,
    city="Austin",
    province="TX",
    country="US",
    territory=Territory.FUERA_UE,
    tax_treatment=TaxTreatment.EXTRACOMUNITARIO,
)


def test_a_business_outside_the_eu_is_kept_without_a_stripe_tax_id_nor_postal_code():
    async def go():
        account, _ = await account_with()
        async with Stage() as s:
            await s.billing.save_profile(account, WORLD_PROFILE)
            saved = await s.billing.profile(account)
            # Back to Spain: the same row takes a NIF and a postal code again.
            await s.billing.save_profile(account, PROFILE)
            back = await s.billing.profile(account)
        return saved, back

    saved, back = run(go())
    assert saved == WORLD_PROFILE
    assert back == PROFILE


def test_the_schema_keeps_the_four_fields_of_a_customer_outside_the_eu_together():
    async def go():
        account, _ = await account_with()
        insert = (
            "INSERT INTO billing_customer (account_id, legal_name, tax_id_type, tax_id_kind,"
            " tax_id, address_line1, postal_code, city, country, territory, tax_treatment,"
            " invoice_email) VALUES ($1::uuid, 'X', $2, $3, $4, 'C/ 1', $5, 'Y', 'US', $6, $7,"
            " 'a@b.c')"
        )
        cases = {
            "ok": (None, "foreign", "CHE-123.456.789 MWST", None, "fuera_ue", "extracomunitario"),
            "stripe_type": (
                "eu_vat",
                "foreign",
                "12-3456789",
                None,
                "fuera_ue",
                "extracomunitario",
            ),
            "kind": (None, "eu_vat", "123456789", None, "fuera_ue", "extracomunitario"),
            "treatment": (None, "foreign", "12-3456789", None, "fuera_ue", "isp_ue"),
            "no_digit": (None, "foreign", "N/A", None, "fuera_ue", "extracomunitario"),
            "lower": (None, "foreign", "ab-123", None, "fuera_ue", "extracomunitario"),
            "too_long": (None, "foreign", "1" * 33, None, "fuera_ue", "extracomunitario"),
            "eu_no_postal": ("eu_vat", "eu_vat", "FR40303265045", None, "ue", "isp_ue"),
            "eu_no_type": (None, "eu_vat", "FR40303265045", "75001", "ue", "isp_ue"),
        }
        return {
            name: await attempt(OWNER_URL, insert, account, *values)
            for name, values in cases.items()
        }

    results = run(go())
    assert results.pop("ok") == "ok"
    assert results == dict.fromkeys(results, "CheckViolationError")
