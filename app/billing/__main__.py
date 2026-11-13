"""Billing process (`python -m app.billing`): the only one that talks to Stripe and to
Odoo (docs/admin-facturacion.md, 4.1 and 9.0.1). Its own database role (fichaitor_billing)
and its own secrets: the restricted Stripe key and the API key of Odoo's invoicing user.
Same queue and loop as the worker. Its jobs:

- `billing_request` (what the web asks) and `stripe_event` (what Stripe tells);
- `issue_invoice` and `issue_credit_note`: the legal invoice and credit note in Odoo;
- `billing_refund`: an exceptional refund an administrator asked for;
- daily: `billing_reconcile`, `billing_renewal_notice` (T-30, price frozen),
  `billing_enforce` (end of the beta) and `billing_purge` (six years).

The emails are enqueued here and sent by the worker (`send_mail`).
"""

import asyncio
import logging
import os
import sys

import asyncpg
from pydantic import ValidationError

from app.adapters.billing_store.postgres_billing import PostgresBillingProcessStore
from app.adapters.job_queue.postgres import PostgresJobQueue
from app.adapters.odoo_invoicing.fake import FakeInvoicing
from app.adapters.odoo_invoicing.real import OdooInvoicing
from app.adapters.stripe.fake import FakePayments
from app.adapters.stripe.real import StripePayments, StripeSettings
from app.billing import (
    enforce,
    events,
    invoices,
    purge,
    reconcile,
    refunds,
    renewal_notice,
    requests,
)
from app.billing.config import BillingConfigError, BillingSettings, odoo_settings
from app.ports.billing_store import BillingJobsStore
from app.ports.invoicing import Invoicing
from app.ports.payments import Payments
from app.worker import log
from app.worker.__main__ import install_stop_signals
from app.worker.loop import DailyJob, Worker
from app.worker.registry import HandlerRegistry

DAILY_KINDS = (reconcile.KIND, renewal_notice.KIND, enforce.KIND, purge.KIND)


def build_payments(settings: BillingSettings) -> Payments:
    if settings.fake_stripe:
        log.event("fake_stripe_enabled", logging.WARNING)
        return FakePayments()
    return StripePayments(
        StripeSettings(settings.stripe_secret_key.get_secret_value(), settings.stripe_livemode)
    )


def build_invoicing(settings: BillingSettings) -> Invoicing | None:
    """None without Odoo configured: invoices wait, pending (and logged as overdue)."""
    if settings.fake_odoo:
        log.event("fake_odoo_enabled", logging.WARNING)
        return FakeInvoicing()
    odoo = odoo_settings(os.environ)
    if odoo is None:
        log.event("odoo_missing", logging.WARNING)
        return None
    return OdooInvoicing(odoo)


def build_registry(
    store: BillingJobsStore,
    payments: Payments,
    settings: BillingSettings,
    prefix: str = "",
    invoicing: Invoicing | None = None,
) -> HandlerRegistry:
    """`prefix`: tests register their own job kinds ("pytest-...")."""
    registry = HandlerRegistry()
    registry.register(
        prefix + requests.KIND,
        requests.BillingRequests(store, payments, settings.public_base_url),
    )
    stripe_events = events.StripeEvents(store, payments, livemode=settings.stripe_livemode)
    registry.register(prefix + events.KIND, stripe_events)
    registry.register(prefix + invoices.IssueInvoice.KIND, invoices.IssueInvoice(store, invoicing))
    registry.register(
        prefix + invoices.IssueCreditNote.KIND,
        invoices.IssueCreditNote(store, invoicing, payments),
    )
    registry.register(prefix + refunds.KIND, refunds.Refunds(store, payments))
    registry.register(
        prefix + reconcile.KIND,
        reconcile.Reconcile(store, payments, invoicing, stripe_events),
    )
    registry.register(prefix + renewal_notice.KIND, renewal_notice.RenewalNotice(store, payments))
    registry.register(prefix + enforce.KIND, enforce.Enforce(store, settings.billing_beta_offer))
    registry.register(prefix + purge.KIND, purge.Purge(store))
    return registry


async def check_odoo(invoicing: Invoicing | None) -> None:
    """Odoo's setup (journals, taxes, products, rounding), logged at start-up: a wrong
    setup does not stop the process, it leaves invoices pending."""
    if invoicing is None:
        return
    for problem in await invoicing.setup_problems():
        log.event("odoo_setup_problem", logging.WARNING, error=problem[:200])


async def run(settings: BillingSettings) -> None:
    store = PostgresBillingProcessStore(settings.billing_database_url)
    await store.connect()
    invoicing: Invoicing | None = None
    try:
        payments = build_payments(settings)
        invoicing = build_invoicing(settings)
        await check_odoo(invoicing)
        worker = Worker(
            PostgresJobQueue(store.pool),
            build_registry(store, payments, settings, invoicing=invoicing),
            worker_id=settings.worker_id,
            poll_interval=settings.worker_poll_interval_seconds,
            job_timeout=settings.worker_job_timeout_seconds,
            lock_timeout=settings.worker_lock_timeout_seconds,
            daily=[DailyJob(kind, settings.billing_daily_hour_utc) for kind in DAILY_KINDS],
        )
        stop = asyncio.Event()
        install_stop_signals(stop)
        await worker.run(stop)
    finally:
        if isinstance(invoicing, OdooInvoicing):
            await invoicing.close()
        await store.close()


def main() -> int:
    log.configure()
    try:
        settings = BillingSettings()
        settings.check()
    except ValidationError as exc:
        names = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
        log.event("config_error", logging.ERROR, error=f"valores no válidos: {', '.join(names)}")
        return 2
    except (BillingConfigError, ValueError) as exc:
        log.event("config_error", logging.ERROR, error=str(exc))
        return 2
    try:
        asyncio.run(run(settings))
    except (OSError, asyncpg.PostgresError) as exc:
        log.event("startup_error", logging.ERROR, error=type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
