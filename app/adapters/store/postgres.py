"""PostgreSQL store. Explicit SQL, no ORM. The API role can only INSERT."""

import asyncpg

from app.ports.store import WaitlistSignup


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
