"""The billing process side of the billing store, in PostgreSQL, with the role
fichaitor_billing (db/schema_billing.sql).

`apply_seat` is the only writer of seats, charges, paid entitlements and blocks. It runs
in one transaction that starts with a lock per account: the events of an account are
applied one after the other, whatever their order, and each is idempotent (the
entitlement of an invoice is keyed by the invoice id).
"""

import logging
from datetime import datetime

import asyncpg

from app.adapters.billing_store.postgres import (
    _PROFILE_COLUMNS,
    _REQUEST_COLUMNS,
    _SEAT_COLUMNS,
    profile_from_row,
    request_from_row,
    seat_from_row,
)
from app.domain.plans import Plan
from app.ports.billing_store import (
    BillingRequest,
    Customer,
    SeatApplied,
    SeatRow,
    SeatSync,
    StripeEventRow,
)

log = logging.getLogger("fichaitor.billing.store")

DUPLICATE_PAYMENT = "duplicate_payment"
ACTIVATION_REFUSED = "activation_refused"


class PostgresBillingProcessStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=4)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("PostgresBillingProcessStore sin conectar")
        return self._pool

    async def launch_at(self) -> datetime | None:
        return await self.pool.fetchval("SELECT launch_at FROM billing_setting LIMIT 1")

    # --- requests ------------------------------------------------------------------

    async def get_request_by_id(self, request_id: str) -> BillingRequest | None:
        row = await self.pool.fetchrow(
            f"SELECT {_REQUEST_COLUMNS} FROM billing_request WHERE id = $1::uuid",  # noqa: S608
            request_id,
        )
        return request_from_row(row) if row is not None else None

    async def finish_request(
        self,
        request_id: str,
        state: str,
        *,
        url: str | None = None,
        code: str | None = None,
        detail: str | None = None,
        expires_at: datetime | None = None,
    ) -> None:
        await self.pool.execute(
            """
            UPDATE billing_request SET state = $2, url = $3, code = $4, detail = $5,
                                       expires_at = $6
            WHERE id = $1::uuid AND state = 'pendiente'
            """,
            request_id,
            state,
            url,
            code,
            detail,
            expires_at,
        )

    # --- customers -----------------------------------------------------------------

    async def customer(self, account_id: str) -> Customer | None:
        row = await self.pool.fetchrow(
            f"""
            SELECT {_PROFILE_COLUMNS}, stripe_customer_id, updated_at
            FROM billing_customer WHERE account_id = $1::uuid
            """,  # noqa: S608
            account_id,
        )
        if row is None:
            return None
        return Customer(
            account_id, profile_from_row(row), row["stripe_customer_id"], row["updated_at"]
        )

    async def account_of_customer(self, customer_id: str) -> str | None:
        return await self.pool.fetchval(
            "SELECT account_id::text FROM billing_customer WHERE stripe_customer_id = $1",
            customer_id,
        )

    async def set_stripe_customer(self, account_id: str, customer_id: str) -> None:
        await self.pool.execute(
            """
            UPDATE billing_customer SET stripe_customer_id = $2
            WHERE account_id = $1::uuid AND stripe_customer_id IS NULL
            """,
            account_id,
            customer_id,
        )

    async def location_owned(self, account_id: str, location_id: str) -> bool:
        return bool(
            await self.pool.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM location l JOIN account a ON a.id = l.account_id
                    WHERE l.id = $2 AND l.account_id = $1::uuid AND l.removed_at IS NULL
                      AND a.deleted_at IS NULL)
                """,
                account_id,
                location_id,
            )
        )

    # --- seats ---------------------------------------------------------------------

    async def seats_of(self, account_id: str) -> list[SeatRow]:
        rows = await self.pool.fetch(
            f"SELECT {_SEAT_COLUMNS} FROM billing_seat WHERE account_id = $1::uuid"  # noqa: S608
            " ORDER BY created_at, id",
            account_id,
        )
        return [seat_from_row(r) for r in rows]

    async def seat(self, seat_id: str) -> SeatRow | None:
        row = await self.pool.fetchrow(
            f"SELECT {_SEAT_COLUMNS} FROM billing_seat WHERE id = $1",  # noqa: S608
            seat_id,
        )
        return seat_from_row(row) if row is not None else None

    async def seat_by_subscription(self, subscription_id: str) -> SeatRow | None:
        row = await self.pool.fetchrow(
            f"SELECT {_SEAT_COLUMNS} FROM billing_seat WHERE stripe_subscription_id = $1",  # noqa: S608
            subscription_id,
        )
        return seat_from_row(row) if row is not None else None

    async def create_seat(
        self, seat_id: str, account_id: str, plan: Plan, location_id: str, request_id: str
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO billing_seat (id, account_id, plan, location_id, request_id)
            VALUES ($1, $2::uuid, $3, $4, $5::uuid)
            ON CONFLICT (id) DO NOTHING
            """,
            seat_id,
            account_id,
            plan.value,
            location_id,
            request_id,
        )

    async def set_cancel_at_period_end(self, seat_id: str, cancel: bool) -> None:
        await self.pool.execute(
            "UPDATE billing_seat SET cancel_at_period_end = $2, updated_at = now() WHERE id = $1",
            seat_id,
            cancel,
        )

    async def seat_of_payment_intent(self, payment_intent_id: str) -> tuple[str, str] | None:
        row = await self.pool.fetchrow(
            """
            SELECT seat_id, stripe_invoice_id FROM billing_charge
            WHERE payment_intent_id = $1 AND seat_id IS NOT NULL
            ORDER BY created_at DESC LIMIT 1
            """,
            payment_intent_id,
        )
        return (row["seat_id"], row["stripe_invoice_id"]) if row is not None else None

    async def apply_seat(self, seat_id: str, sync: SeatSync) -> SeatApplied:
        async with self.pool.acquire() as conn, conn.transaction():
            seat = await conn.fetchrow(
                f"SELECT {_SEAT_COLUMNS} FROM billing_seat WHERE id = $1 FOR UPDATE",  # noqa: S608
                seat_id,
            )
            if seat is None:
                return SeatApplied()
            account_id = seat["account_id"]
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('billing_account:' || $1, 0))",
                account_id,
            )
            await conn.execute(
                """
                UPDATE billing_seat SET
                    stripe_subscription_id = coalesce(stripe_subscription_id, $2),
                    status = $3, current_period_start = $4, current_period_end = $5,
                    cancel_at_period_end = $6, next_price_key = coalesce($7, next_price_key),
                    updated_at = now()
                WHERE id = $1
                """,
                seat_id,
                sync.subscription_id,
                sync.status.value,
                sync.current_period_start,
                sync.current_period_end,
                sync.cancel_at_period_end,
                sync.price_key.value if sync.price_key else None,
            )
            invoice = sync.invoice
            if invoice is not None:
                await self._copy_charge(conn, account_id, seat_id, invoice)
            granted = False
            if invoice is not None and sync.entitled_until is not None:
                granted = await self._grant(conn, account_id, seat, invoice.id, sync.entitled_until)
            if sync.end_rights_at is not None:
                await conn.execute(
                    """
                    UPDATE plan_entitlement SET ends_at = $2
                    WHERE seat_id = $1 AND source = 'pago' AND starts_at < $2
                      AND (ends_at IS NULL OR ends_at > $2)
                    """,
                    seat_id,
                    sync.end_rights_at,
                )
            if invoice is not None and sync.block is not None:
                await conn.execute(
                    """
                    INSERT INTO account_billing_block
                        (account_id, reason, stripe_invoice_id, seat_id)
                    SELECT $1::uuid, $2, $3, $4
                    WHERE NOT EXISTS (
                        SELECT 1 FROM account_billing_block
                        WHERE account_id = $1::uuid AND stripe_invoice_id = $3
                          AND unblocked_at IS NULL)
                    """,
                    account_id,
                    sync.block.value,
                    invoice.id,
                    seat_id,
                )
            if invoice is not None and sync.unblock:
                await conn.execute(
                    """
                    UPDATE account_billing_block SET unblocked_at = now(), unblocked_by = 'payment'
                    WHERE account_id = $1::uuid AND stripe_invoice_id = $2 AND unblocked_at IS NULL
                    """,
                    account_id,
                    invoice.id,
                )
            if not (sync.activate and granted):
                return SeatApplied(granted=granted)
            return await self._activate(conn, seat, granted)

    @staticmethod
    async def _copy_charge(
        conn: asyncpg.Connection, account_id: str, seat_id: str, invoice
    ) -> None:
        await conn.execute(
            """
            INSERT INTO billing_charge AS c
                (stripe_invoice_id, account_id, seat_id, status, billing_reason, currency,
                 subtotal, tax, total, amount_paid, amount_refunded, price_key, period_start,
                 period_end, paid_at, payment_intent_id, charge_id, disputed)
            VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
                    $17, $18)
            ON CONFLICT (stripe_invoice_id) DO UPDATE SET
                status = EXCLUDED.status, amount_paid = EXCLUDED.amount_paid,
                amount_refunded = EXCLUDED.amount_refunded, paid_at = EXCLUDED.paid_at,
                payment_intent_id = coalesce(EXCLUDED.payment_intent_id, c.payment_intent_id),
                charge_id = coalesce(EXCLUDED.charge_id, c.charge_id),
                disputed = EXCLUDED.disputed, updated_at = now()
            """,
            invoice.id,
            account_id,
            seat_id,
            invoice.status,
            invoice.billing_reason,
            invoice.currency,
            invoice.subtotal,
            invoice.tax,
            invoice.total,
            invoice.amount_paid,
            invoice.amount_refunded,
            invoice.price_key.value if invoice.price_key else None,
            invoice.period_start,
            invoice.period_end,
            invoice.paid_at,
            invoice.payment_intent_id,
            invoice.charge_id,
            invoice.disputed,
        )

    @staticmethod
    async def _grant(
        conn: asyncpg.Connection, account_id: str, seat: asyncpg.Record, invoice_id: str, until
    ) -> bool:
        """One entitlement per paid invoice; the previous ones of the seat end now, so a
        renewal never counts the seat twice."""
        status = await conn.execute(
            """
            INSERT INTO plan_entitlement
                (account_id, plan, quantity, starts_at, ends_at, source, external_ref, seat_id)
            VALUES ($1::uuid, $2, 1, now(), $3, 'pago', $4, $5)
            ON CONFLICT (external_ref) WHERE source = 'pago' DO NOTHING
            """,
            account_id,
            seat["plan"],
            until,
            invoice_id,
            seat["id"],
        )
        if not status.endswith(" 1"):
            return False
        await conn.execute(
            """
            UPDATE plan_entitlement SET ends_at = now()
            WHERE seat_id = $1 AND source = 'pago' AND external_ref <> $2
              AND starts_at < now() AND (ends_at IS NULL OR ends_at > now())
            """,
            seat["id"],
            invoice_id,
        )
        return True

    @staticmethod
    async def _activate(
        conn: asyncpg.Connection, seat: asyncpg.Record, granted: bool
    ) -> SeatApplied:
        """The profile of a seat just bought becomes active in its plan. If another seat
        already holds that profile (two payments for the same profile), this one is left
        free in the account, for review: never two seats for one profile, and no
        automatic refund."""
        location_id = seat["location_id"]
        if location_id is None:
            return SeatApplied(granted=granted)
        other = await conn.fetchval(
            """
            SELECT id FROM billing_seat
            WHERE account_id = $1::uuid AND location_id = $2 AND id <> $3
              AND status = 'active' AND current_period_end > now()
            LIMIT 1
            """,
            seat["account_id"],
            location_id,
            seat["id"],
        )
        if other is not None:
            await conn.execute(
                "UPDATE billing_seat SET location_id = NULL, review_reason = $2 WHERE id = $1",
                seat["id"],
                DUPLICATE_PAYMENT,
            )
            log.warning("plaza duplicada para una ficha: queda libre para revisión")
            return SeatApplied(granted=granted, duplicate=True)
        try:
            async with conn.transaction():
                done = await conn.execute(
                    """
                    UPDATE location SET active_since = coalesce(active_since, now()), plan = $3
                    WHERE id = $2 AND account_id = $1::uuid AND removed_at IS NULL
                      AND plan IS DISTINCT FROM $3
                    """,
                    seat["account_id"],
                    location_id,
                    seat["plan"],
                )
        except asyncpg.PostgresError as exc:
            # The database refused (a leave pending, the account deleted...): the seat is
            # paid and stays, for review; the owner can assign it later.
            reason = getattr(exc, "message", None) or type(exc).__name__
            await conn.execute(
                "UPDATE billing_seat SET review_reason = $2 WHERE id = $1",
                seat["id"],
                ACTIVATION_REFUSED,
            )
            log.warning("la ficha de una plaza pagada no se ha podido activar: %s", reason[:60])
            return SeatApplied(granted=granted, activation_refused=reason[:60])
        return SeatApplied(granted=granted, activated=done.endswith(" 1"))

    # --- events --------------------------------------------------------------------

    async def stripe_event(self, event_id: str) -> StripeEventRow | None:
        row = await self.pool.fetchrow(
            "SELECT id, type, object_id, livemode, processed_at FROM stripe_event WHERE id = $1",
            event_id,
        )
        if row is None:
            return None
        return StripeEventRow(
            row["id"], row["type"], row["object_id"], row["livemode"], row["processed_at"]
        )

    async def finish_event(self, event_id: str, outcome: str) -> None:
        await self.pool.execute(
            "UPDATE stripe_event SET processed_at = now(), outcome = $2 WHERE id = $1",
            event_id,
            outcome,
        )
