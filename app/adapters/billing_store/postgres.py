"""The API side of the billing store, in PostgreSQL, with the role fichaitor_api
(db/schema_billing.sql). Every statement is filtered by the account of the session.

A request and its job are written in the same transaction: either the billing process
gets it, or nothing was asked. A seat request takes a transaction lock per profile
before its checks, and the unique index `billing_request_one_seat_pending` is the last
barrier: two tabs at once never get two Checkouts for the same profile.
"""

import json
import uuid
from datetime import datetime

import asyncpg

from app.adapters.store.postgres import PostgresStore
from app.domain.billing import PriceKey, SeatStatus, TaxTreatment, Territory
from app.domain.plans import Plan
from app.ports.billing_store import (
    EVENT_JOB,
    PAY,
    PORTAL,
    REQUEST_JOB,
    SEAT,
    AlreadyActive,
    BillingRequest,
    FiscalProfile,
    InvoiceDocument,
    SeatPending,
    SeatRow,
)
from app.ports.store import LocationNotFound

REQUEST_MAX_ATTEMPTS = 5
EVENT_MAX_ATTEMPTS = 20  # Stripe objects are read again: a long outage is retried

_PROFILE_COLUMNS = """
    legal_name, tax_id_type, tax_id_kind, tax_id, address_line1, address_line2, postal_code,
    city, province, country, territory, tax_treatment, invoice_email
"""
_SEAT_COLUMNS = """
    id, account_id::text AS account_id, plan, status, created_at, location_id, collection,
    stripe_subscription_id, current_period_start, current_period_end, cancel_at_period_end,
    next_price_key, request_id::text AS request_id, review_reason, trial_ends_at
"""
# The latest end of the beta entitlements of an account: an end left empty counts as the
# end of the window (billing_setting.beta_ends_at; a century if there is no launch yet,
# capped anyway by billing.first_charge_at). No row: an account created from the launch.
BETA_UNTIL_SQL = """
    SELECT max(coalesce(e.ends_at,
                        (SELECT beta_ends_at FROM billing_setting LIMIT 1),
                        now() + interval '100 years'))
    FROM plan_entitlement e
    WHERE e.account_id = $1::uuid AND e.source = 'beta'
"""
_REQUEST_COLUMNS = """
    id::text AS id, account_id::text AS account_id, kind, state, created_at, location_id,
    plan, seat_id, url, code, detail, expires_at
"""


def profile_from_row(row: asyncpg.Record) -> FiscalProfile:
    return FiscalProfile(
        legal_name=row["legal_name"],
        tax_id=row["tax_id"],
        tax_id_type=row["tax_id_type"],
        tax_id_kind=row["tax_id_kind"],
        address_line1=row["address_line1"],
        address_line2=row["address_line2"],
        postal_code=row["postal_code"],
        city=row["city"],
        province=row["province"],
        country=row["country"],
        territory=Territory(row["territory"]),
        tax_treatment=TaxTreatment(row["tax_treatment"]),
        invoice_email=row["invoice_email"],
    )


def seat_from_row(row: asyncpg.Record) -> SeatRow:
    key = row["next_price_key"]
    return SeatRow(
        id=row["id"],
        account_id=row["account_id"],
        plan=Plan(row["plan"]),
        status=SeatStatus(row["status"]),
        created_at=row["created_at"],
        location_id=row["location_id"],
        collection=row["collection"],
        stripe_subscription_id=row["stripe_subscription_id"],
        current_period_start=row["current_period_start"],
        current_period_end=row["current_period_end"],
        cancel_at_period_end=row["cancel_at_period_end"],
        next_price_key=PriceKey(key) if key else None,
        request_id=row["request_id"],
        review_reason=row["review_reason"],
        trial_ends_at=row["trial_ends_at"],
    )


def request_from_row(row: asyncpg.Record) -> BillingRequest:
    return BillingRequest(
        id=row["id"],
        account_id=row["account_id"],
        kind=row["kind"],
        state=row["state"],
        created_at=row["created_at"],
        location_id=row["location_id"],
        plan=Plan(row["plan"]) if row["plan"] else None,
        seat_id=row["seat_id"],
        url=row["url"],
        code=row["code"],
        detail=row["detail"],
        expires_at=row["expires_at"],
    )


async def _enqueue(conn: asyncpg.Connection, kind: str, payload: dict, key: str, attempts: int):
    # ON CONFLICT without a target and without RETURNING needs only INSERT privilege.
    await conn.execute(
        """
        INSERT INTO job (kind, payload, idempotency_key, max_attempts)
        VALUES ($1, $2::jsonb, $3, $4)
        ON CONFLICT DO NOTHING
        """,
        kind,
        json.dumps(payload),
        key,
        attempts,
    )


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


class PostgresBillingStore:
    def __init__(self, store: PostgresStore, *, job_prefix: str = "") -> None:
        # The pool of the API's store. `job_prefix`: tests use their own job kinds
        # ("pytest-..."), so that no process running on the same database takes them.
        self._store = store
        self._job_prefix = job_prefix

    def _pool(self) -> asyncpg.Pool:
        return self._store._require_pool()

    async def launch_at(self) -> datetime | None:
        return await self._pool().fetchval("SELECT launch_at FROM billing_setting LIMIT 1")

    async def beta_until(self, account_id: str) -> datetime | None:
        if not _is_uuid(account_id):
            return None
        return await self._pool().fetchval(BETA_UNTIL_SQL, account_id)

    async def profile(self, account_id: str) -> FiscalProfile | None:
        row = await self._pool().fetchrow(
            f"SELECT {_PROFILE_COLUMNS} FROM billing_customer WHERE account_id = $1::uuid",  # noqa: S608
            account_id,
        )
        return profile_from_row(row) if row is not None else None

    async def save_profile(self, account_id: str, profile: FiscalProfile) -> None:
        await self._pool().execute(
            """
            INSERT INTO billing_customer AS c
                (account_id, legal_name, tax_id_type, tax_id_kind, tax_id, address_line1,
                 address_line2, postal_code, city, province, country, territory, tax_treatment,
                 invoice_email)
            VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            ON CONFLICT (account_id) DO UPDATE SET
                legal_name = EXCLUDED.legal_name, tax_id_type = EXCLUDED.tax_id_type,
                tax_id_kind = EXCLUDED.tax_id_kind, tax_id = EXCLUDED.tax_id,
                address_line1 = EXCLUDED.address_line1, address_line2 = EXCLUDED.address_line2,
                postal_code = EXCLUDED.postal_code, city = EXCLUDED.city,
                province = EXCLUDED.province, country = EXCLUDED.country,
                territory = EXCLUDED.territory, tax_treatment = EXCLUDED.tax_treatment,
                invoice_email = EXCLUDED.invoice_email
            """,
            account_id,
            profile.legal_name,
            profile.tax_id_type,
            profile.tax_id_kind,
            profile.tax_id,
            profile.address_line1,
            profile.address_line2,
            profile.postal_code,
            profile.city,
            profile.province,
            profile.country,
            profile.territory.value,
            profile.tax_treatment.value,
            profile.invoice_email,
        )

    async def seats(self, account_id: str) -> list[SeatRow]:
        rows = await self._pool().fetch(
            f"SELECT {_SEAT_COLUMNS} FROM billing_seat WHERE account_id = $1::uuid"  # noqa: S608
            " ORDER BY created_at, id",
            account_id,
        )
        return [seat_from_row(r) for r in rows]

    async def request_seat(self, account_id: str, location_id: str, plan: Plan) -> str:
        async with self._pool().acquire() as conn, conn.transaction():
            # One at a time per profile: two tabs asking at once are counted in order.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('billing_seat:' || $1, 0))",
                location_id,
            )
            location = await conn.fetchrow(
                """
                SELECT l.plan FROM location l JOIN account a ON a.id = l.account_id
                WHERE l.id = $2 AND l.account_id = $1::uuid AND l.removed_at IS NULL
                  AND a.deleted_at IS NULL
                """,
                account_id,
                location_id,
            )
            if location is None:
                raise LocationNotFound
            pending = await conn.fetchval(
                """
                SELECT id::text FROM billing_request
                WHERE account_id = $1::uuid AND location_id = $2 AND kind = 'seat'
                  AND (state = 'pendiente' OR (state = 'lista' AND expires_at > now()))
                ORDER BY created_at DESC LIMIT 1
                """,
                account_id,
                location_id,
            )
            if pending is not None:
                raise SeatPending(pending)
            # A seat charged for this profile that has not activated it yet (the webhook
            # is on its way): paying again would charge twice.
            paid = await conn.fetchrow(
                """
                SELECT s.request_id::text AS request_id FROM billing_seat s
                WHERE s.account_id = $1::uuid AND s.location_id = $2 AND s.status = 'active'
                  AND s.current_period_end > now()
                  AND $3::text IS DISTINCT FROM s.plan
                ORDER BY s.created_at DESC LIMIT 1
                """,
                account_id,
                location_id,
                location["plan"],
            )
            if paid is not None:
                raise SeatPending(paid["request_id"])
            if location["plan"] == plan.value:
                raise AlreadyActive
            try:
                async with conn.transaction():
                    request_id = await conn.fetchval(
                        """
                        INSERT INTO billing_request (account_id, kind, location_id, plan)
                        VALUES ($1::uuid, 'seat', $2, $3) RETURNING id::text
                        """,
                        account_id,
                        location_id,
                        plan.value,
                    )
            except asyncpg.UniqueViolationError:
                raise SeatPending(None) from None
            await self._enqueue_request(conn, request_id)
        return request_id

    async def _enqueue_request(self, conn: asyncpg.Connection, request_id: str) -> None:
        kind = self._job_prefix + REQUEST_JOB
        await _enqueue(
            conn, kind, {"request_id": request_id}, f"{kind}:{request_id}", REQUEST_MAX_ATTEMPTS
        )

    async def request_for_seat(self, account_id: str, kind: str, seat_id: str) -> str | None:
        async with self._pool().acquire() as conn, conn.transaction():
            request_id = await conn.fetchval(
                """
                INSERT INTO billing_request (account_id, kind, seat_id)
                SELECT s.account_id, $3, s.id FROM billing_seat s
                WHERE s.id = $2 AND s.account_id = $1::uuid AND s.collection = 'auto'
                  AND s.stripe_subscription_id IS NOT NULL AND s.status <> 'canceled'
                RETURNING id::text
                """,
                account_id,
                seat_id,
                kind,
            )
            if request_id is not None:
                await self._enqueue_request(conn, request_id)
        return request_id

    async def request(self, account_id: str, kind: str) -> str:
        if kind not in (PAY, PORTAL):
            raise ValueError(kind)
        async with self._pool().acquire() as conn, conn.transaction():
            request_id = await conn.fetchval(
                """
                INSERT INTO billing_request (account_id, kind) VALUES ($1::uuid, $2)
                RETURNING id::text
                """,
                account_id,
                kind,
            )
            await self._enqueue_request(conn, request_id)
        return request_id

    async def get_request(self, account_id: str, request_id: str) -> BillingRequest | None:
        if not _is_uuid(request_id):
            return None
        row = await self._pool().fetchrow(
            f"SELECT {_REQUEST_COLUMNS} FROM billing_request"  # noqa: S608
            " WHERE id = $2::uuid AND account_id = $1::uuid",
            account_id,
            request_id,
        )
        return request_from_row(row) if row is not None else None

    async def record_stripe_event(
        self, event_id: str, type_: str, object_id: str | None, livemode: bool, created: datetime
    ) -> bool:
        async with self._pool().acquire() as conn, conn.transaction():
            status = await conn.execute(
                """
                INSERT INTO stripe_event (id, type, object_id, livemode, stripe_created)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT DO NOTHING
                """,
                event_id,
                type_,
                object_id,
                livemode,
                created,
            )
            if not status.endswith(" 1"):
                return False
            kind = self._job_prefix + EVENT_JOB
            await _enqueue(
                conn, kind, {"event_id": event_id}, f"{kind}:{event_id}", EVENT_MAX_ATTEMPTS
            )
        return True

    async def invoices(self, account_id: str) -> list[InvoiceDocument]:
        rows = await self._pool().fetch(
            """
            SELECT id::text AS id, kind, number, amount_total, issued_at FROM billing_document
            WHERE account_id = $1::uuid AND state = 'emitida'
            ORDER BY issued_at DESC, id
            """,
            account_id,
        )
        return [
            InvoiceDocument(r["id"], r["kind"], r["number"], r["amount_total"], r["issued_at"])
            for r in rows
        ]

    async def invoice_pdf(self, account_id: str, document_id: str) -> bytes | None:
        if not _is_uuid(document_id):
            return None
        return await self._pool().fetchval(
            """
            SELECT pdf FROM billing_document
            WHERE id = $2::uuid AND account_id = $1::uuid AND state = 'emitida'
            """,
            account_id,
            document_id,
        )


__all__ = ["PostgresBillingStore", "SEAT"]
