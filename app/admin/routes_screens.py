"""The screens of the backoffice (docs/admin-facturacion.md, 6.3 and 6.4; contract in
docs/contrato-backoffice.md). Every route calls one admin_* function of the database,
which checks the session, the role and, for sensitive actions, a TOTP of the last 5
minutes, and writes the audit row. The process only adds what the database cannot do
without the secrets of admin.env (the DMARC check of a pasted message) and what is pure
presentation (links to Stripe and Odoo, ARR from the domain prices).
"""

import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from app.admin.audit import (
    APP_CHANNELS_READ,
    APP_JOBS_READ,
    APP_OFFBOARDING_DMARC_FAILED,
    APP_SYNC_READ,
    APP_USERS_READ,
    audit_read,
)
from app.admin.auth import AuthKit
from app.admin.session import Actor, current_actor
from app.admin.store import AdminError, AdminStore
from app.domain.billing import PriceKey, parse_dmarc, price_cents

UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
LOCATION_PATTERN = r"^loc_[0-9a-z]{4,32}$"
INVOICE_PATTERN = r"^in_[A-Za-z0-9]{1,250}$"

# First-year price of each plan, for a seat whose next price is not frozen yet.
_FIRST_PRICE = {"pro": PriceKey.PRO_FIRST, "pro_whatsapp": PriceKey.WHATSAPP_FIRST}


class TicketIn(BaseModel):
    ticket_ref: str = Field(min_length=3, max_length=500)


class DmarcIn(BaseModel):
    # The original of the message, headers included ("Mostrar original" in the mail client).
    raw_message: str = Field(min_length=10, max_length=262_144)


class OffboardingIn(BaseModel):
    scope: Literal["ficha", "cuenta"]
    location_id: str | None = Field(default=None, pattern=LOCATION_PATTERN)
    ticket_ref: str = Field(min_length=3, max_length=500)


class GrantIn(BaseModel):
    plan: Literal["pro", "pro_whatsapp"]
    quantity: int = Field(ge=1, le=10)
    source: Literal["cortesia", "recomendacion", "manual", "beta"]
    ends_at: datetime
    note_code: str | None = Field(default=None, pattern=r"^[a-z_]{1,40}$")
    ticket_ref: str = Field(min_length=3, max_length=500)


class TrimIn(BaseModel):
    # Empty: now.
    ends_at: datetime | None = None
    ticket_ref: str = Field(min_length=3, max_length=500)


class RefundIn(BaseModel):
    stripe_invoice_id: str = Field(pattern=INVOICE_PATTERN)
    # Cents; empty: what is left to refund of that invoice.
    amount: int | None = Field(default=None, ge=1)
    reason: Literal["error_cobro", "decision_comercial", "cambio_tercero", "disputa"]
    ticket_ref: str = Field(min_length=3, max_length=80)


def stripe_links(livemode: bool) -> dict[str, str]:
    """Templates of the links to the dashboard of Stripe (section 6.4): replace {id}."""
    base = "https://dashboard.stripe.com/" + ("" if livemode else "test/")
    return {
        "customer": base + "customers/{id}",
        "invoice": base + "invoices/{id}",
        "subscription": base + "subscriptions/{id}",
        "payment": base + "payments/{id}",
    }


def with_links(billing: dict, erp_base_url: str) -> dict:
    """Adds the ready links: Stripe (by livemode) and the invoice in Odoo."""
    links = stripe_links(bool(billing.get("livemode")))
    customer = billing.get("customer") or {}
    if customer.get("stripe_customer_id"):
        customer["stripe_url"] = links["customer"].format(id=customer["stripe_customer_id"])
    for seat in billing.get("seats") or []:
        if seat.get("stripe_subscription_id"):
            seat["stripe_url"] = links["subscription"].format(id=seat["stripe_subscription_id"])
    for charge in billing.get("charges") or []:
        charge["stripe_url"] = links["invoice"].format(id=charge["stripe_invoice_id"])
        if charge.get("payment_intent_id"):
            charge["payment_url"] = links["payment"].format(id=charge["payment_intent_id"])
    for doc in billing.get("documents") or []:
        if doc.get("odoo_move_id") is not None:
            doc["odoo_url"] = f"{erp_base_url.rstrip('/')}/odoo/account.move/{doc['odoo_move_id']}"
        if doc.get("stripe_invoice_id"):
            doc["stripe_url"] = links["invoice"].format(id=doc["stripe_invoice_id"])
    return billing


def recurring_revenue(seats_in_force: list[dict]) -> dict:
    """ARR = the next price of every seat in force (active and not set to cancel); MRR =
    ARR / 12. In cents, without VAT. A seat whose next price is not frozen yet counts at
    the first-year price of its plan (`estimated_seats`)."""
    arr = 0
    estimated = 0
    for row in seats_in_force:
        key = row.get("next_price_key")
        if key:
            cents = price_cents(PriceKey(key))
        else:
            cents = price_cents(_FIRST_PRICE[row["plan"]])
            estimated += row["seats"]
        arr += cents * row["seats"]
    return {"arr_cents": arr, "mrr_cents": round(arr / 12), "estimated_seats": estimated}


def build_router(kit: AuthKit, store: AdminStore, authserv_id: str, erp_base_url: str) -> APIRouter:
    router = APIRouter(prefix="/admin/api")

    async def actor(request: Request) -> Actor:
        return await current_actor(request, kit, store)

    # --- accounts ------------------------------------------------------------------------

    @router.get("/accounts")
    async def find_accounts(request: Request, q: str = Query(min_length=3, max_length=254)) -> dict:
        me = await actor(request)
        return {"accounts": await store.find_accounts(me.hash, q)}

    @router.get("/accounts/{account_id}")
    async def account_overview(request: Request, account_id: str) -> dict:
        me = await actor(request)
        _uuid(account_id)
        return await store.account_overview(me.hash, account_id)

    @router.post("/accounts/{account_id}/locations")
    async def account_locations(request: Request, account_id: str, body: TicketIn) -> dict:
        """The names of the profiles (Google data): only with the ticket, audited."""
        me = await actor(request)
        _uuid(account_id)
        return {"locations": await store.account_detail(me.hash, account_id, body.ticket_ref)}

    @router.get("/accounts/{account_id}/sync")
    async def account_sync(request: Request, account_id: str) -> dict:
        me = await actor(request)
        _uuid(account_id)
        rows = await store.sync_status(me.hash, account_id)
        await audit_read(store, me, APP_SYNC_READ, account_id=account_id)
        return {"sync": rows}

    @router.get("/accounts/{account_id}/channels")
    async def account_channels(request: Request, account_id: str) -> dict:
        me = await actor(request)
        _uuid(account_id)
        rows = await store.channel_links(me.hash, account_id)
        await audit_read(store, me, APP_CHANNELS_READ, account_id=account_id)
        return {"channels": rows}

    @router.get("/accounts/{account_id}/billing")
    async def account_billing(request: Request, account_id: str) -> dict:
        me = await actor(request)
        _uuid(account_id)
        return with_links(await store.account_billing(me.hash, account_id), erp_base_url)

    @router.post("/accounts/{account_id}/billing/contact")
    async def billing_contact(request: Request, account_id: str, body: TicketIn) -> dict:
        me = await actor(request)
        _uuid(account_id)
        return await store.billing_contact(me.hash, account_id, body.ticket_ref)

    # --- leave asked for by email (section 6.4) -------------------------------------------

    @router.post("/accounts/{account_id}/offboarding/check")
    async def offboarding_check(request: Request, account_id: str, body: DmarcIn) -> dict:
        """Step 1 and 2: DMARC of the pasted message (only the first Authentication-Results,
        from our server) and the comparison of From: with the email of the account, made
        by the database without showing it. Never answers with the address."""
        me = await actor(request)
        _uuid(account_id)
        # The database checks the role again (admin_email_matches); this only avoids
        # parsing a message for a role that may not use it.
        if me.role not in ("soporte", "propietario"):
            raise AdminError(403, "role_not_allowed", "Tu rol no permite hacer esto.")
        if not authserv_id.strip():
            raise AdminError(
                503, "authserv_not_configured", "Falta FICHAITOR_ADMIN_AUTHSERV_ID en admin.env."
            )
        check = parse_dmarc(body.raw_message, authserv_id)
        if not check.passed or not check.from_address or not check.message_id_sha256:
            await store.audit(
                me.user_id,
                APP_OFFBOARDING_DMARC_FAILED,
                account_id=account_id,
                detail={"reason": check.reason},
            )
            raise AdminError(
                422, "dmarc_failed", f"El mensaje no prueba su remitente ({check.reason})."
            )
        matched = await store.email_matches(
            me.hash, account_id, check.from_address, check.message_id_sha256
        )
        return {"dmarc": "pass", "from_domain": check.from_domain, "matches_account": matched}

    @router.post("/accounts/{account_id}/offboarding")
    async def offboarding(request: Request, account_id: str, body: OffboardingIn) -> dict:
        """Step 3 and 4: the leave, scheduled 48 hours ahead, with a fresh TOTP."""
        me = await actor(request)
        _uuid(account_id)
        done = await store.email_offboarding(
            me.hash, account_id, body.scope, body.location_id, body.ticket_ref
        )
        return {"offboarding_id": done.get("offboarding_id"), "delete_at": done.get("delete_at")}

    # --- entitlements, blocks and refunds ---------------------------------------------------

    @router.post("/accounts/{account_id}/entitlements")
    async def grant_entitlement(request: Request, account_id: str, body: GrantIn) -> dict:
        me = await actor(request)
        _uuid(account_id)
        granted = await store.grant_entitlement(
            me.hash,
            account_id,
            body.plan,
            body.quantity,
            body.source,
            _aware(body.ends_at),
            body.note_code,
            body.ticket_ref,
        )
        return {"entitlement_id": granted}

    @router.post("/entitlements/{entitlement_id}/trim")
    async def trim_entitlement(request: Request, entitlement_id: str, body: TrimIn) -> dict:
        me = await actor(request)
        _uuid(entitlement_id)
        ends = _aware(body.ends_at) if body.ends_at else None
        await store.trim_entitlement(me.hash, entitlement_id, ends, body.ticket_ref)
        return {"ok": True}

    @router.post("/blocks/{block_id}/close")
    async def close_block(request: Request, block_id: int, body: TicketIn) -> dict:
        me = await actor(request)
        account = await store.close_block(me.hash, block_id, body.ticket_ref)
        return {"account_id": account}

    @router.post("/refunds", status_code=202)
    async def request_refund(request: Request, body: RefundIn) -> dict:
        me = await actor(request)
        request_id = await store.request_refund(
            me.hash, body.stripe_invoice_id, body.amount, body.reason, body.ticket_ref
        )
        return {"refund_request_id": request_id}

    @router.get("/billing/review")
    async def billing_review(request: Request) -> dict:
        me = await actor(request)
        review = await store.billing_review(me.hash)
        review["links"] = stripe_links(bool(review.get("livemode")))
        review["odoo_link"] = erp_base_url.rstrip("/") + "/odoo/account.move/{id}"
        return review

    # --- health, metrics, waiting list, users and audit -------------------------------------

    @router.get("/health/overview")
    async def health_overview(request: Request) -> dict:
        me = await actor(request)
        summary = await store.health_summary(me.hash)
        result = {
            **summary,
            "jobs": await store.job_stats(me.hash),
            "failed_jobs": await store.failed_jobs(me.hash),
            "retention_runs": await store.retention_runs(me.hash),
            "sync_errors": await store.sync_status(me.hash, None),
        }
        await audit_read(store, me, APP_JOBS_READ)
        return result

    @router.get("/metrics")
    async def metrics(
        request: Request,
        since: Annotated[date | None, Query(alias="from")] = None,
        until: Annotated[date | None, Query(alias="to")] = None,
    ) -> dict:
        """Totals of the period [from, to), whole UTC days; by default the last 30 days."""
        me = await actor(request)
        end = until or (datetime.now(UTC).date() + timedelta(days=1))
        start = since or (end - timedelta(days=30))
        result = await store.metrics(
            me.hash,
            datetime.combine(start, time(), tzinfo=UTC),
            datetime.combine(end, time(), tzinfo=UTC),
        )
        result["revenue"] = recurring_revenue(result.get("seats_in_force") or [])
        result["period"] = {"from": start.isoformat(), "to": end.isoformat()}
        return result

    @router.get("/waitlist")
    async def waitlist(request: Request) -> dict:
        me = await actor(request)
        return {"cells": await store.waitlist_totals(me.hash), "min_cell": 5}

    @router.get("/users")
    async def users(request: Request) -> dict:
        me = await actor(request)
        rows = await store.users(me.hash)
        await audit_read(store, me, APP_USERS_READ)
        return {"users": rows}

    @router.get("/audit")
    async def audit(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        before: int | None = Query(default=None, ge=1),
    ) -> dict:
        me = await actor(request)
        rows = await store.audit_read(me.hash, limit, before)
        return {"rows": rows, "next_before": rows[-1]["id"] if rows else None}

    return router


def _uuid(value: str) -> None:
    if not re.match(UUID_PATTERN, value):
        raise AdminError(404, "not_found", "No existe.")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
