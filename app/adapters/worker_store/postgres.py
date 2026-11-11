"""Worker's view of the database: Google connections (read) and profile summaries."""

import json
from datetime import datetime

import asyncpg

from app.adapters.job_queue.postgres import LOST_WORKER_ERROR
from app.domain.profile_summary import STATUS_ERROR, STATUS_OK, ProfileSummary
from app.ports.worker_store import ConnectedLocation, StoredConnection


class PostgresWorkerStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def latest_connection(self, google_sub: str) -> StoredConnection | None:
        row = await self._pool.fetchrow(
            """
            SELECT refresh_token_enc, key_version, locations::text AS locations
            FROM google_connection
            WHERE google_sub = $1
            ORDER BY connected_at DESC, id DESC
            LIMIT 1
            """,
            google_sub,
        )
        if row is None:
            return None
        raw = json.loads(row["locations"])
        locations = tuple(
            ConnectedLocation(account=item["account"], location=item["location"])
            for item in (raw if isinstance(raw, list) else [])
            if isinstance(item, dict)
            and isinstance(item.get("account"), str)
            and isinstance(item.get("location"), str)
        )
        return StoredConnection(
            refresh_token_enc=row["refresh_token_enc"],
            key_version=row["key_version"],
            locations=locations,
        )

    async def save_summary(
        self, google_sub: str, location: str, summary: ProfileSummary, at: datetime
    ) -> None:
        await self._pool.execute(
            """
            INSERT INTO profile_summary
                (google_sub, location, status, status_at, synced_at, review_count,
                 average_rating, unanswered_count, unanswered_low_count, last_review_at,
                 has_regular_hours, upcoming_special_hours, google_changed,
                 google_changed_fields)
            VALUES ($1, $2, $3, $4, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            ON CONFLICT (google_sub, location) DO UPDATE SET
                status = EXCLUDED.status,
                status_at = EXCLUDED.status_at,
                synced_at = EXCLUDED.synced_at,
                review_count = EXCLUDED.review_count,
                average_rating = EXCLUDED.average_rating,
                unanswered_count = EXCLUDED.unanswered_count,
                unanswered_low_count = EXCLUDED.unanswered_low_count,
                last_review_at = EXCLUDED.last_review_at,
                has_regular_hours = EXCLUDED.has_regular_hours,
                upcoming_special_hours = EXCLUDED.upcoming_special_hours,
                google_changed = EXCLUDED.google_changed,
                google_changed_fields = EXCLUDED.google_changed_fields
            """,
            google_sub,
            location,
            STATUS_OK,
            at,
            summary.review_count,
            summary.average_rating,
            summary.unanswered_count,
            summary.unanswered_low_count,
            summary.last_review_at,
            summary.has_regular_hours,
            summary.upcoming_special_hours,
            summary.google_changed,
            list(summary.google_changed_fields),
        )

    async def save_status(self, google_sub: str, location: str, status: str, at: datetime) -> None:
        await self._pool.execute(
            """
            INSERT INTO profile_summary (google_sub, location, status, status_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (google_sub, location) DO UPDATE SET
                status = EXCLUDED.status, status_at = EXCLUDED.status_at
            """,
            google_sub,
            location,
            status,
            at,
        )

    async def mark_lost_syncs(self, kind: str) -> int:
        # The status takes the time the job was closed and is written only over an
        # older one: a later sync is never hidden and repeating this changes nothing.
        # One day back is plenty (it runs every minute) and keeps old jobs out.
        status = await self._pool.execute(
            """
            INSERT INTO profile_summary (google_sub, location, status, status_at)
            SELECT DISTINCT ON (google_sub, location) google_sub, location, $3, finished_at
            FROM (
                SELECT payload->>'google_sub' AS google_sub, payload->>'location' AS location,
                       finished_at
                FROM job
                WHERE kind = $1 AND state = 'failed' AND last_error = $2
                  AND finished_at > now() - interval '1 day'
                  AND jsonb_typeof(payload->'google_sub') = 'string'
                  AND jsonb_typeof(payload->'location') = 'string'
            ) AS lost
            WHERE length(google_sub) BETWEEN 1 AND 255 AND length(location) BETWEEN 1 AND 200
            ORDER BY google_sub, location, finished_at DESC
            ON CONFLICT (google_sub, location) DO UPDATE SET
                status = EXCLUDED.status, status_at = EXCLUDED.status_at
            WHERE profile_summary.status_at < EXCLUDED.status_at
            """,
            kind,
            LOST_WORKER_ERROR,
            STATUS_ERROR,
        )
        return int(status.rsplit(" ", 1)[-1])
