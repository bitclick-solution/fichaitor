"""The billing process side of the billing store, in PostgreSQL, with the role
fichaitor_billing (db/schema_billing.sql).

`apply_seat` is the only writer of seats, charges, paid entitlements and blocks. It runs
in one transaction that starts with a lock per account: the events of an account are
applied one after the other, whatever their order, and each is idempotent (the
entitlement of an invoice is keyed by the invoice id). In that same transaction it
enqueues what follows (step 9): the legal invoice of a paid charge, the email of a block
just opened and the one of an account just unblocked. A job is keyed by what it is about,
so an event repeated never enqueues it twice.

The rest is for the legal invoices and the daily jobs (`BillingJobsStore`): documents,
refunds, the renewal notice, the end of the beta and the purge of what is past its six
years (`billing_purge()`, a function of the owner: this role deletes nothing itself).
"""

import json
import logging
import uuid
from datetime import datetime

import asyncpg

from app.adapters.billing_store.postgres import (
    _PROFILE_COLUMNS,
    _REQUEST_COLUMNS,
    _SEAT_COLUMNS,
    BETA_UNTIL_SQL,
    profile_from_row,
    request_from_row,
    seat_from_row,
)
from app.domain import billing_mail
from app.domain.billing import PriceKey
from app.domain.jobs import DEFAULT_MAX_ATTEMPTS
from app.domain.plans import PAID_PLANS, Plan
from app.ports.billing_store import (
    CREDIT_NOTE,
    CREDIT_NOTE_JOB,
    DOC_ISSUED,
    DOC_PENDING,
    DOC_REVIEW,
    DOCUMENT_MAX_ATTEMPTS,
    INVOICE,
    ISSUE_INVOICE_JOB,
    MAIL_JOB,
    REFUND_DONE,
    REFUND_FAILED,
    REFUND_PENDING,
    ActiveProfile,
    BetaSetting,
    BillingRequest,
    ChargeContext,
    CreditNoteContext,
    Customer,
    DocumentRow,
    LocationState,
    RefundRequestRow,
    SeatApplied,
    SeatRow,
    SeatSync,
    StripeEventRow,
)

log = logging.getLogger("fichaitor.billing.store")

DUPLICATE_PAYMENT = "duplicate_payment"
ACTIVATION_REFUSED = "activation_refused"
_FIRST_PURCHASE = "subscription_create"

_DOCUMENT_COLUMNS = """
    id::text AS id, kind, state, account_id::text AS account_id, created_at, stripe_invoice_id,
    stripe_refund_id, number, amount_total, odoo_move_id, review_reason
"""
_REFUND_COLUMNS = """
    id::text AS id, account_id::text AS account_id, stripe_invoice_id, reason, state, amount,
    stripe_refund_id
"""


def _document(row: asyncpg.Record) -> DocumentRow:
    return DocumentRow(
        id=row["id"],
        kind=row["kind"],
        state=row["state"],
        account_id=row["account_id"],
        created_at=row["created_at"],
        stripe_invoice_id=row["stripe_invoice_id"],
        stripe_refund_id=row["stripe_refund_id"],
        number=row["number"],
        amount_total=row["amount_total"],
        odoo_move_id=row["odoo_move_id"],
        review_reason=row["review_reason"],
    )


def _refund(row: asyncpg.Record) -> RefundRequestRow:
    return RefundRequestRow(
        id=row["id"],
        account_id=row["account_id"],
        stripe_invoice_id=row["stripe_invoice_id"],
        reason=row["reason"],
        state=row["state"],
        amount=row["amount"],
        stripe_refund_id=row["stripe_refund_id"],
    )


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


async def _enqueue(
    conn: asyncpg.Connection,
    kind: str,
    payload: dict,
    key: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> bool:
    """A job, once per key. Without a conflict target: INSERT is the only privilege used."""
    status = await conn.execute(
        """
        INSERT INTO job (kind, payload, idempotency_key, max_attempts)
        VALUES ($1, $2::jsonb, $3, $4)
        ON CONFLICT DO NOTHING
        """,
        kind,
        json.dumps(payload),
        key,
        max_attempts,
    )
    return status.endswith(" 1")


async def _mail_context(
    conn: asyncpg.Connection, account_id: str, location_id: str | None
) -> tuple[str | None, str | None]:
    """(invoice email, profile name) of an account and a seat's profile."""
    email = await conn.fetchval(
        "SELECT invoice_email FROM billing_customer WHERE account_id = $1::uuid", account_id
    )
    title = None
    if location_id is not None:
        title = await conn.fetchval(
            "SELECT title FROM location WHERE account_id = $1::uuid AND id = $2",
            account_id,
            location_id,
        )
    return email, title


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

    async def beta_until(self, account_id: str) -> datetime | None:
        if not _is_uuid(account_id):
            return None
        return await self.pool.fetchval(BETA_UNTIL_SQL, account_id)

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
                    trial_ends_at = $8, updated_at = now()
                WHERE id = $1
                """,
                seat_id,
                sync.subscription_id,
                sync.status.value,
                sync.current_period_start,
                sync.current_period_end,
                sync.cancel_at_period_end,
                sync.price_key.value if sync.price_key else None,
                sync.trial_ends_at,
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
            opened = closed = False
            if invoice is not None and sync.block is not None:
                status = await conn.execute(
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
                opened = status.endswith(" 1")
            if invoice is not None and sync.unblock:
                status = await conn.execute(
                    """
                    UPDATE account_billing_block SET unblocked_at = now(), unblocked_by = 'payment'
                    WHERE account_id = $1::uuid AND stripe_invoice_id = $2 AND unblocked_at IS NULL
                    """,
                    account_id,
                    invoice.id,
                )
                closed = not status.endswith(" 0")
            follow = await self._follow_up(conn, seat, sync, granted, opened, closed)
            if not (sync.activate and granted):
                return SeatApplied(granted=granted, **follow)
            applied = await self._activate(conn, seat, granted)
            return SeatApplied(
                granted=applied.granted,
                activated=applied.activated,
                duplicate=applied.duplicate,
                activation_refused=applied.activation_refused,
                **follow,
            )

    @staticmethod
    async def _follow_up(
        conn: asyncpg.Connection,
        seat: asyncpg.Record,
        sync: SeatSync,
        granted: bool,
        opened: bool,
        closed: bool,
    ) -> dict[str, bool]:
        """What a change of a seat sets off (step 9), in its transaction: the legal invoice
        of a paid charge, the funnel's `paid` on a first purchase, and the emails of a
        block opened or of the account unblocked (only when no other block is left)."""
        invoice = sync.invoice
        account_id, plan = seat["account_id"], Plan(seat["plan"])
        done = {"invoice_queued": False, "blocked_mail": False, "unblocked_mail": False}
        if invoice is None:
            return done
        if invoice.status == "paid" and invoice.amount_paid > 0:
            done["invoice_queued"] = await _enqueue(
                conn,
                ISSUE_INVOICE_JOB,
                {"stripe_invoice_id": invoice.id},
                f"{ISSUE_INVOICE_JOB}:{invoice.id}",
                DOCUMENT_MAX_ATTEMPTS,
            )
        if granted and invoice.billing_reason == _FIRST_PURCHASE:
            await conn.execute(
                "INSERT INTO funnel_event (kind, source) VALUES ('paid', $1)", plan.value
            )
        if not (opened or closed):
            return done
        email, title = await _mail_context(conn, account_id, seat["location_id"])
        if email is None:
            return done
        if opened and sync.block is not None:
            mail = billing_mail.blocked_mail(
                email, plan=plan, title=title, total=invoice.total, reason=sync.block.value
            )
            done["blocked_mail"] = await _enqueue(
                conn, MAIL_JOB, mail, f"{MAIL_JOB}:billing_blocked:{invoice.id}"
            )
        if closed and not await conn.fetchval(
            "SELECT fichaitor_account_blocked($1::uuid)", account_id
        ):
            mail = billing_mail.unblocked_mail(email, plan=plan, title=title)
            done["unblocked_mail"] = await _enqueue(
                conn, MAIL_JOB, mail, f"{MAIL_JOB}:billing_unblocked:{invoice.id}"
            )
        return done

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

    async def record_funnel(self, kind: str, source: str | None = None) -> None:
        await self.pool.execute(
            "INSERT INTO funnel_event (kind, source) VALUES ($1, $2)", kind, source
        )

    async def enqueue(
        self, kind: str, payload: dict, key: str, max_attempts: int = DEFAULT_MAX_ATTEMPTS
    ) -> bool:
        async with self.pool.acquire() as conn:
            return await _enqueue(conn, kind, payload, key, max_attempts)

    # --- documents (step 9) --------------------------------------------------------

    async def charge_context(self, stripe_invoice_id: str) -> ChargeContext | None:
        row = await self.pool.fetchrow(
            """
            SELECT c.stripe_invoice_id, c.account_id::text AS account_id, c.status, c.total,
                   c.amount_paid, c.amount_refunded, c.price_key, c.period_start, c.period_end,
                   c.paid_at, c.seat_id, s.plan, s.stripe_subscription_id, l.title,
                   coalesce(c.charge_id, c.payment_intent_id) AS payment_reference
            FROM billing_charge c
            LEFT JOIN billing_seat s ON s.id = c.seat_id
            LEFT JOIN location l ON l.account_id = s.account_id AND l.id = s.location_id
            WHERE c.stripe_invoice_id = $1
            """,
            stripe_invoice_id,
        )
        if row is None:
            return None
        key = row["price_key"]
        return ChargeContext(
            stripe_invoice_id=row["stripe_invoice_id"],
            account_id=row["account_id"],
            status=row["status"],
            total=row["total"],
            amount_paid=row["amount_paid"],
            amount_refunded=row["amount_refunded"],
            price_key=PriceKey(key) if key in set(PriceKey) else None,
            period_start=row["period_start"],
            period_end=row["period_end"],
            paid_at=row["paid_at"],
            seat_id=row["seat_id"],
            plan=Plan(row["plan"]) if row["plan"] else None,
            subscription_id=row["stripe_subscription_id"],
            location_title=row["title"],
            payment_reference=row["payment_reference"],
            customer=await self.customer(row["account_id"]),
        )

    async def open_document(
        self, kind: str, account_id: str, stripe_invoice_id: str, stripe_refund_id: str | None
    ) -> DocumentRow:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO billing_document (kind, account_id, stripe_invoice_id, stripe_refund_id)
                VALUES ($1, $2::uuid, $3, $4)
                ON CONFLICT DO NOTHING
                """,
                kind,
                account_id,
                stripe_invoice_id,
                stripe_refund_id,
            )
            found = await self._document_of(
                conn, kind, stripe_refund_id if kind == CREDIT_NOTE else stripe_invoice_id
            )
        assert found is not None
        return found

    @staticmethod
    async def _document_of(
        conn: asyncpg.Connection | asyncpg.Pool, kind: str, stripe_id: str | None
    ) -> DocumentRow | None:
        column = "stripe_refund_id" if kind == CREDIT_NOTE else "stripe_invoice_id"
        row = await conn.fetchrow(
            f"SELECT {_DOCUMENT_COLUMNS} FROM billing_document"  # noqa: S608
            f" WHERE kind = $1 AND {column} = $2",
            kind,
            stripe_id,
        )
        return _document(row) if row is not None else None

    async def document_of(self, kind: str, stripe_id: str) -> DocumentRow | None:
        return await self._document_of(self.pool, kind, stripe_id)

    async def issue_document(
        self,
        document_id: str,
        number: str | None,
        move_id: int,
        amounts: tuple[int, int, int],
        verifactu_state: str | None,
        pdf: bytes,
        mail: dict,
    ) -> bool:
        untaxed, tax, total = amounts
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE billing_document SET
                    state = $2, number = $3, odoo_move_id = $4, amount_untaxed = $5,
                    amount_tax = $6, amount_total = $7, verifactu_state = $8, pdf = $9,
                    issued_at = now(), review_reason = NULL
                WHERE id = $1::uuid AND state = $10
                RETURNING kind
                """,
                document_id,
                DOC_ISSUED,
                number,
                move_id,
                untaxed,
                tax,
                total,
                verifactu_state,
                pdf,
                DOC_PENDING,
            )
            if row is None:
                return False
            await _enqueue(
                conn,
                MAIL_JOB,
                {**mail, "document_id": document_id},
                f"{MAIL_JOB}:{row['kind']}:{document_id}",
            )
        return True

    async def review_document(
        self,
        document_id: str,
        reason: str,
        number: str | None = None,
        move_id: int | None = None,
        amount_total: int | None = None,
    ) -> None:
        await self.pool.execute(
            """
            UPDATE billing_document SET
                state = $2, review_reason = $3, number = coalesce($4, number),
                odoo_move_id = coalesce($5, odoo_move_id),
                amount_total = coalesce($6, amount_total)
            WHERE id = $1::uuid AND state = $7
            """,
            document_id,
            DOC_REVIEW,
            reason[:40],
            number,
            move_id,
            amount_total,
            DOC_PENDING,
        )

    async def flag_document(self, document_id: str, reason: str) -> None:
        await self.pool.execute(
            "UPDATE billing_document SET review_reason = nullif($2, '') WHERE id = $1::uuid",
            document_id,
            reason[:40],
        )

    async def documents_since(self, since: datetime) -> list[DocumentRow]:
        rows = await self.pool.fetch(
            f"SELECT {_DOCUMENT_COLUMNS} FROM billing_document"  # noqa: S608
            " WHERE created_at >= $1 ORDER BY created_at, id",
            since,
        )
        return [_document(r) for r in rows]

    async def charge_paid_known(self, stripe_invoice_id: str) -> bool:
        return bool(
            await self.pool.fetchval(
                "SELECT EXISTS (SELECT 1 FROM billing_charge"
                " WHERE stripe_invoice_id = $1 AND status = 'paid')",
                stripe_invoice_id,
            )
        )

    async def live_subscriptions(self) -> list[str]:
        rows = await self.pool.fetch(
            """
            SELECT stripe_subscription_id FROM billing_seat
            WHERE stripe_subscription_id IS NOT NULL AND status <> 'canceled'
            ORDER BY created_at, id
            """
        )
        return [r["stripe_subscription_id"] for r in rows]

    # --- refunds -------------------------------------------------------------------

    async def refund_request(self, request_id: str) -> RefundRequestRow | None:
        if not _is_uuid(request_id):
            return None
        row = await self.pool.fetchrow(
            f"SELECT {_REFUND_COLUMNS} FROM billing_refund_request WHERE id = $1::uuid",  # noqa: S608
            request_id,
        )
        return _refund(row) if row is not None else None

    async def refund_request_of(self, stripe_refund_id: str) -> RefundRequestRow | None:
        row = await self.pool.fetchrow(
            f"SELECT {_REFUND_COLUMNS} FROM billing_refund_request"  # noqa: S608
            " WHERE stripe_refund_id = $1",
            stripe_refund_id,
        )
        return _refund(row) if row is not None else None

    async def pending_refund_requests(self) -> list[str]:
        rows = await self.pool.fetch(
            "SELECT id::text AS id FROM billing_refund_request WHERE state = $1"
            " ORDER BY created_at",
            REFUND_PENDING,
        )
        return [r["id"] for r in rows]

    async def finish_refund(
        self,
        request_id: str,
        stripe_refund_id: str,
        stripe_invoice_id: str,
        amount_refunded: int,
        whole: bool,
    ) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            account_id = await conn.fetchval(
                """
                UPDATE billing_refund_request
                SET state = $2, stripe_refund_id = $3, finished_at = now()
                WHERE id = $1::uuid AND state = $4
                RETURNING account_id::text
                """,
                request_id,
                REFUND_DONE,
                stripe_refund_id,
                REFUND_PENDING,
            )
            if account_id is None:
                return
            await conn.execute(
                """
                UPDATE billing_charge SET amount_refunded = greatest(amount_refunded, $2),
                                          updated_at = now()
                WHERE stripe_invoice_id = $1
                """,
                stripe_invoice_id,
                amount_refunded,
            )
            if whole:
                # Nothing of that charge is kept: its right ends now (never later).
                await conn.execute(
                    """
                    UPDATE plan_entitlement SET ends_at = now()
                    WHERE source = 'pago' AND external_ref = $1 AND starts_at < now()
                      AND (ends_at IS NULL OR ends_at > now())
                    """,
                    stripe_invoice_id,
                )
            await conn.execute(
                """
                INSERT INTO billing_document (kind, account_id, stripe_invoice_id, stripe_refund_id)
                VALUES ($1, $2::uuid, $3, $4)
                ON CONFLICT DO NOTHING
                """,
                CREDIT_NOTE,
                account_id,
                stripe_invoice_id,
                stripe_refund_id,
            )
            await _enqueue(
                conn,
                CREDIT_NOTE_JOB,
                {"stripe_refund_id": stripe_refund_id},
                f"{CREDIT_NOTE_JOB}:{stripe_refund_id}",
                DOCUMENT_MAX_ATTEMPTS,
            )

    async def fail_refund(self, request_id: str) -> None:
        await self.pool.execute(
            """
            UPDATE billing_refund_request SET state = $2, finished_at = now()
            WHERE id = $1::uuid AND state = $3
            """,
            request_id,
            REFUND_FAILED,
            REFUND_PENDING,
        )

    async def credit_note_context(self, stripe_refund_id: str) -> CreditNoteContext | None:
        document = await self.document_of(CREDIT_NOTE, stripe_refund_id)
        if document is None or document.stripe_invoice_id is None:
            return None
        original = await self.document_of(INVOICE, document.stripe_invoice_id)
        email = await self.pool.fetchval(
            "SELECT invoice_email FROM billing_customer WHERE account_id = $1::uuid",
            document.account_id,
        )
        return CreditNoteContext(
            document=document,
            refund=await self.refund_request_of(stripe_refund_id),
            original=original,
            invoice_email=email,
        )

    # --- renewal notice ------------------------------------------------------------

    async def seats_renewing_by(self, until: datetime) -> list[SeatRow]:
        rows = await self.pool.fetch(
            f"""
            SELECT {_SEAT_COLUMNS} FROM billing_seat
            WHERE collection = 'auto' AND stripe_subscription_id IS NOT NULL
              AND status = 'active' AND NOT cancel_at_period_end
              AND current_period_end > now() AND current_period_end <= $1
            ORDER BY current_period_end, id
            """,  # noqa: S608
            until,
        )
        return [seat_from_row(r) for r in rows]

    async def renewal_noticed(self, seat_id: str) -> datetime | None:
        return await self.pool.fetchval(
            "SELECT renewal_notice_for FROM billing_seat WHERE id = $1", seat_id
        )

    async def location_state(self, account_id: str, location_id: str) -> LocationState:
        row = await self.pool.fetchrow(
            """
            SELECT l.active_since IS NOT NULL AND l.removed_at IS NULL AS active, l.title
            FROM location l WHERE l.account_id = $1::uuid AND l.id = $2
            """,
            account_id,
            location_id,
        )
        if row is None:
            return LocationState(False)
        return LocationState(bool(row["active"]), row["title"])

    async def mark_renewal_notice(
        self, seat_id: str, period_end: datetime, price_key: PriceKey, mail: dict, key: str
    ) -> bool:
        async with self.pool.acquire() as conn, conn.transaction():
            status = await conn.execute(
                """
                UPDATE billing_seat SET renewal_notice_for = $2, next_price_key = $3,
                                        updated_at = now()
                WHERE id = $1 AND renewal_notice_for IS DISTINCT FROM $2
                """,
                seat_id,
                period_end,
                price_key.value,
            )
            if status.endswith(" 0"):
                return False
            await _enqueue(conn, MAIL_JOB, mail, key)
        return True

    async def mark_trial_notice(
        self, seat_id: str, trial_ends_at: datetime, mail: dict, key: str
    ) -> bool:
        async with self.pool.acquire() as conn, conn.transaction():
            status = await conn.execute(
                """
                UPDATE billing_seat SET trial_notice_for = $2, updated_at = now()
                WHERE id = $1 AND trial_notice_for IS DISTINCT FROM $2
                """,
                seat_id,
                trial_ends_at,
            )
            if status.endswith(" 0"):
                return False
            await _enqueue(conn, MAIL_JOB, mail, key)
        return True

    # --- end of the beta -----------------------------------------------------------

    async def beta_setting(self) -> BetaSetting:
        row = await self.pool.fetchrow(
            "SELECT launch_at, beta_ends_at, beta_notice_for FROM billing_setting LIMIT 1"
        )
        if row is None:
            return BetaSetting(None, None, None)
        return BetaSetting(row["launch_at"], row["beta_ends_at"], row["beta_notice_for"])

    async def set_beta_notice(self, beta_ends_at: datetime) -> None:
        await self.pool.execute("UPDATE billing_setting SET beta_notice_for = $1", beta_ends_at)

    async def beta_recipients(self) -> list[tuple[str, str]]:
        rows = await self.pool.fetch(
            """
            SELECT a.id::text AS id, a.email FROM account a
            WHERE a.deleted_at IS NULL
              AND EXISTS (SELECT 1 FROM location l
                          WHERE l.account_id = a.id AND l.removed_at IS NULL
                            AND l.active_since IS NOT NULL AND l.plan = ANY($1::text[]))
              AND NOT EXISTS (SELECT 1 FROM offboarding o
                              WHERE o.account_id = a.id AND o.restored_at IS NULL
                                AND o.effective_at IS NULL)
            ORDER BY a.id
            """,
            [p.value for p in PAID_PLANS],
        )
        return [(r["id"], r["email"]) for r in rows]

    async def accounts_with_paid_profiles(self) -> list[str]:
        rows = await self.pool.fetch(
            """
            SELECT DISTINCT l.account_id::text AS id FROM location l
            JOIN account a ON a.id = l.account_id AND a.deleted_at IS NULL
            WHERE l.removed_at IS NULL AND l.active_since IS NOT NULL
              AND l.plan = ANY($1::text[])
            ORDER BY 1
            """,
            [p.value for p in PAID_PLANS],
        )
        return [r["id"] for r in rows]

    async def active_profiles(
        self, account_id: str, now: datetime
    ) -> tuple[list[ActiveProfile], dict[Plan, int]]:
        rows = await self.pool.fetch(
            """
            SELECT l.id, l.plan, l.active_since, l.gbp_location,
                   EXISTS (SELECT 1 FROM billing_seat s
                           WHERE s.account_id = l.account_id AND s.location_id = l.id
                             AND s.plan = l.plan AND s.status = 'active'
                             AND s.current_period_end > $2) AS has_seat
            FROM location l
            WHERE l.account_id = $1::uuid AND l.removed_at IS NULL
              AND l.active_since IS NOT NULL AND l.plan IS NOT NULL
            ORDER BY l.active_since, l.id
            """,
            account_id,
            now,
        )
        granted = await self.pool.fetch(
            """
            SELECT plan, sum(quantity)::int AS n FROM plan_entitlement
            WHERE account_id = $1::uuid AND source IN ('manual', 'cortesia', 'recomendacion')
              AND starts_at <= $2 AND (ends_at IS NULL OR ends_at > $2)
            GROUP BY plan
            """,
            account_id,
            now,
        )
        profiles = [
            ActiveProfile(
                r["id"], Plan(r["plan"]), r["active_since"], r["gbp_location"], r["has_seat"]
            )
            for r in rows
        ]
        return profiles, {Plan(r["plan"]): r["n"] for r in granted if r["plan"]}

    async def gratis_elsewhere(self, account_id: str, gbp_locations: list[str]) -> set[str]:
        if not gbp_locations:
            return set()
        rows = await self.pool.fetch(
            """
            SELECT DISTINCT gbp_location FROM location
            WHERE gbp_location = ANY($2::text[]) AND account_id <> $1::uuid AND plan = 'gratis'
            """,
            account_id,
            gbp_locations,
        )
        return {r["gbp_location"] for r in rows}

    async def move_to_gratis(self, account_id: str, location_id: str) -> bool:
        return await self._location_change(
            """
            UPDATE location SET plan = 'gratis'
            WHERE account_id = $1::uuid AND id = $2 AND removed_at IS NULL
              AND active_since IS NOT NULL AND plan = ANY($3::text[])
            """,
            account_id,
            location_id,
            [p.value for p in PAID_PLANS],
        )

    async def deactivate(self, account_id: str, location_id: str) -> bool:
        return await self._location_change(
            """
            UPDATE location SET active_since = NULL
            WHERE account_id = $1::uuid AND id = $2 AND active_since IS NOT NULL
            """,
            account_id,
            location_id,
        )

    async def _location_change(self, sql: str, *args: object) -> bool:
        """True if the profile changed. The trigger location_active_guard decides; if it
        refuses (a leave pending, the free profile taken...), nothing changes."""
        try:
            status = await self.pool.execute(sql, *args)
        except asyncpg.PostgresError as exc:
            log.warning("cambio de ficha rechazado por la base: %s", type(exc).__name__)
            return False
        return status.endswith(" 1")

    async def purge(self) -> dict[str, int]:
        counts = await self.pool.fetchval("SELECT billing_purge()")
        if isinstance(counts, str):
            counts = json.loads(counts)
        return {str(k): int(v) for k, v in (counts or {}).items()}
