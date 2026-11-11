"""PostgreSQL store. Explicit SQL, no ORM.

The API role can only INSERT contacts, events and Google connections. On accounts,
profiles and sessions it can read, insert and update a few columns; it never deletes.
Every statement on profiles is filtered by account_id: the isolation between accounts.
"""

import json
from collections.abc import Sequence
from datetime import datetime

import asyncpg

from app.ports.google_oauth import ProfileLocation
from app.ports.store import Account, GoogleConnection, Location, WaitlistSignup


class PostgresStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("PostgresStore sin conectar")
        return self._pool

    async def add_signup(self, signup: WaitlistSignup) -> None:
        # ON CONFLICT without a target needs only INSERT privilege.
        await self._require_pool().execute(
            """
            INSERT INTO waitlist_entry
                (channel, contact, place_id, business_name, score, source, consent_version)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT DO NOTHING
            """,
            signup.channel,
            signup.contact,
            signup.place_id,
            signup.business_name,
            signup.score,
            signup.source,
            signup.consent_version,
        )

    async def add_google_connection(self, connection: GoogleConnection) -> None:
        await self._require_pool().execute(
            """
            INSERT INTO google_connection
                (google_sub, email, refresh_token_enc, key_version, scopes, locations)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            """,
            connection.google_sub,
            connection.email,
            connection.refresh_token_enc,
            connection.key_version,
            list(connection.scopes),
            json.dumps(list(connection.locations)),
        )

    async def record_event(
        self,
        kind: str,
        source: str | None = None,
        place_id: str | None = None,
        score: int | None = None,
    ) -> None:
        await self._require_pool().execute(
            "INSERT INTO funnel_event (kind, source, place_id, score) VALUES ($1, $2, $3, $4)",
            kind,
            source,
            place_id,
            score,
        )

    async def upsert_account(self, google_sub: str, email: str) -> str:
        return await self._require_pool().fetchval(
            """
            INSERT INTO account (google_sub, email) VALUES ($1, $2)
            ON CONFLICT (google_sub) DO UPDATE SET email = EXCLUDED.email
            RETURNING id::text
            """,
            google_sub,
            email,
        )

    async def sync_locations(
        self, account_id: str, locations: Sequence[ProfileLocation], now: datetime
    ) -> None:
        async with self._require_pool().acquire() as conn, conn.transaction():
            await conn.executemany(
                """
                INSERT INTO location
                    (account_id, gbp_account, gbp_location, title, place_id, verified, updated_at)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (account_id, gbp_location) DO UPDATE SET
                    gbp_account = EXCLUDED.gbp_account,
                    title = EXCLUDED.title,
                    place_id = EXCLUDED.place_id,
                    verified = EXCLUDED.verified,
                    updated_at = EXCLUDED.updated_at,
                    removed_at = NULL
                """,
                [
                    (
                        account_id,
                        p.account,
                        p.location,
                        p.title,
                        p.place_id or None,
                        p.verified,
                        now,
                    )
                    for p in locations
                ],
            )
            await conn.execute(
                """
                UPDATE location SET removed_at = $3
                WHERE account_id = $1::uuid AND removed_at IS NULL
                  AND gbp_location <> ALL($2::text[])
                """,
                account_id,
                [p.location for p in locations],
                now,
            )
            await conn.execute(
                """
                UPDATE account SET selected_location_id = NULL
                WHERE id = $1::uuid AND selected_location_id IN (
                    SELECT id FROM location WHERE account_id = $1::uuid AND removed_at IS NOT NULL
                )
                """,
                account_id,
            )

    async def create_session(
        self, account_id: str, token_hash: str, now: datetime, expires_at: datetime
    ) -> None:
        await self._require_pool().execute(
            """
            INSERT INTO session (account_id, token_hash, created_at, expires_at)
            VALUES ($1::uuid, $2, $3, $4)
            """,
            account_id,
            token_hash,
            now,
            expires_at,
        )

    async def account_for_session(self, token_hash: str, now: datetime) -> Account | None:
        row = await self._require_pool().fetchrow(
            """
            SELECT a.id::text AS id, a.email, a.selected_location_id
            FROM session s JOIN account a ON a.id = s.account_id
            WHERE s.token_hash = $1 AND s.revoked_at IS NULL AND s.expires_at > $2
            """,
            token_hash,
            now,
        )
        return Account(row["id"], row["email"], row["selected_location_id"]) if row else None

    async def revoke_session(self, token_hash: str, now: datetime) -> None:
        await self._require_pool().execute(
            "UPDATE session SET revoked_at = $2 WHERE token_hash = $1 AND revoked_at IS NULL",
            token_hash,
            now,
        )

    async def list_locations(self, account_id: str) -> list[Location]:
        rows = await self._require_pool().fetch(
            """
            SELECT id, title, place_id, verified FROM location
            WHERE account_id = $1::uuid AND removed_at IS NULL
            ORDER BY title COLLATE "C", id
            """,
            account_id,
        )
        return [Location(r["id"], r["title"], r["place_id"], r["verified"]) for r in rows]

    async def select_location(self, account_id: str, location_id: str) -> bool:
        # The ownership check is part of the statement, not a previous read.
        status = await self._require_pool().execute(
            """
            UPDATE account SET selected_location_id = $2
            WHERE id = $1::uuid AND EXISTS (
                SELECT 1 FROM location
                WHERE id = $2 AND account_id = $1::uuid AND removed_at IS NULL
            )
            """,
            account_id,
            location_id,
        )
        return status == "UPDATE 1"
