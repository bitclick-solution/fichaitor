"""Billing process (`python -m app.billing`): the only one that talks to Stripe
(docs/admin-facturacion.md, 4.1). Its own database role (fichaitor_billing) and its own
secrets: the restricted Stripe key. It runs the jobs `billing_request` (what the web asks)
and `stripe_event` (what Stripe tells), with the same queue and loop as the worker.

Out of this first step (docs/contrato-web-api.md, «Cobro y paywall»): the notice and the
frozen price at T-30, the daily reconciliation, the end of the beta, the refunds and the
legal invoice in Odoo (step 9).
"""

import asyncio
import logging
import sys

import asyncpg
from pydantic import ValidationError

from app.adapters.billing_store.postgres_billing import PostgresBillingProcessStore
from app.adapters.job_queue.postgres import PostgresJobQueue
from app.adapters.stripe.fake import FakePayments
from app.adapters.stripe.real import StripePayments, StripeSettings
from app.billing import events, requests
from app.billing.config import BillingConfigError, BillingSettings
from app.ports.billing_store import BillingProcessStore
from app.ports.payments import Payments
from app.worker import log
from app.worker.__main__ import install_stop_signals
from app.worker.loop import Worker
from app.worker.registry import HandlerRegistry


def build_payments(settings: BillingSettings) -> Payments:
    if settings.fake_stripe:
        log.event("fake_stripe_enabled", logging.WARNING)
        return FakePayments()
    return StripePayments(
        StripeSettings(settings.stripe_secret_key.get_secret_value(), settings.stripe_livemode)
    )


def build_registry(
    store: BillingProcessStore, payments: Payments, settings: BillingSettings, prefix: str = ""
) -> HandlerRegistry:
    """`prefix`: tests register their own job kinds ("pytest-...")."""
    registry = HandlerRegistry()
    registry.register(
        prefix + requests.KIND,
        requests.BillingRequests(store, payments, settings.public_base_url),
    )
    registry.register(
        prefix + events.KIND,
        events.StripeEvents(store, payments, livemode=settings.stripe_livemode),
    )
    return registry


async def run(settings: BillingSettings) -> None:
    store = PostgresBillingProcessStore(settings.billing_database_url)
    await store.connect()
    try:
        payments = build_payments(settings)
        worker = Worker(
            PostgresJobQueue(store.pool),
            build_registry(store, payments, settings),
            worker_id=settings.worker_id,
            poll_interval=settings.worker_poll_interval_seconds,
            job_timeout=settings.worker_job_timeout_seconds,
            lock_timeout=settings.worker_lock_timeout_seconds,
        )
        stop = asyncio.Event()
        install_stop_signals(stop)
        await worker.run(stop)
    finally:
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
