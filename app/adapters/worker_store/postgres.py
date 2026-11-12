"""Worker's view of the database: Google connections (read), profile summaries,
reviews, and the actions it carries out with their audit trail.

The worker role cannot create an action or change what one says (its text, its review,
its account): it only records progress and outcome. It can only add to the audit log.
"""

import json
from collections.abc import Sequence
from datetime import datetime

import asyncpg

from app.adapters.job_queue.postgres import LOST_WORKER_ERROR
from app.domain.actions import (
    DONE,
    ERROR_UNAVAILABLE,
    EVENT_FAILED,
    EVENT_SENT,
    FAILED,
)
from app.domain.profile_summary import (
    LOW_RATING_MAX,
    STATUS_ERROR,
    STATUS_OK,
    ProfileSummary,
    Review,
    facts_to_json,
)
from app.domain.reviews import storable_reviews, storable_text
from app.ports.worker_store import ClaimedAction, ConnectedLocation, StoredConnection

_AUDIT_FROM_ACTION = """
    INSERT INTO audit_log
        (event, account_id, action_id, action_type, location, review_id, gbp_review_id,
         text, confirmed_at, google_status, reason)
    SELECT $2, a.account_id, a.id, a.type, a.location, a.review_id, r.gbp_review_id,
           a.text, a.confirmed_at, $3, $4
    FROM action a JOIN review r ON r.id = a.review_id
    WHERE a.id = $1
"""


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
                 google_changed_fields, facts)
            VALUES ($1, $2, $3, $4, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14::jsonb)
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
                google_changed_fields = EXCLUDED.google_changed_fields,
                facts = EXCLUDED.facts
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
            json.dumps(facts_to_json(summary.facts)) if summary.facts is not None else None,
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

    async def save_reviews(
        self, google_sub: str, location: str, reviews: Sequence[Review], at: datetime
    ) -> None:
        rows = sorted(storable_reviews(reviews), key=lambda r: r.review_id)
        ids = [r.review_id for r in rows]
        async with self._pool.acquire() as conn, conn.transaction():
            # No column for the author exists.
            await conn.execute(
                """
                INSERT INTO review
                    (google_sub, location, gbp_review_id, rating, comment, created_at,
                     reply_text, reply_at, first_seen_at, last_seen_at)
                SELECT $1, $2, r.gbp_review_id, r.rating, r.comment, r.created_at,
                       r.reply_text, r.reply_at, $3, $3
                FROM unnest($4::text[], $5::smallint[], $6::text[], $7::timestamptz[],
                            $8::text[], $9::timestamptz[])
                     AS r (gbp_review_id, rating, comment, created_at, reply_text, reply_at)
                ORDER BY r.gbp_review_id
                ON CONFLICT (google_sub, location, gbp_review_id) DO UPDATE SET
                    rating = EXCLUDED.rating,
                    comment = EXCLUDED.comment,
                    created_at = EXCLUDED.created_at,
                    last_seen_at = EXCLUDED.last_seen_at,
                    removed_at = NULL,
                    reply_text = EXCLUDED.reply_text,
                    reply_at = EXCLUDED.reply_at
                """,
                google_sub,
                location,
                at,
                ids,
                [r.rating for r in rows],
                [storable_text(r.text) for r in rows],
                [r.created_at for r in rows],
                [storable_text(r.reply_text) if r.reply_text is not None else None for r in rows],
                [r.reply_at for r in rows],
            )
            # What Google no longer returns is marked, never deleted.
            await conn.execute(
                """
                UPDATE review SET removed_at = $3
                WHERE google_sub = $1 AND location = $2 AND removed_at IS NULL
                  AND gbp_review_id <> ALL($4::text[])
                """,
                google_sub,
                location,
                at,
                ids,
            )
            await conn.execute(
                """
                UPDATE profile_summary SET reviews_synced_at = $3
                WHERE google_sub = $1 AND location = $2
                """,
                google_sub,
                location,
                at,
            )

    async def recent_replies(
        self, google_sub: str, location: str, since: datetime
    ) -> dict[str, tuple[str, datetime | None]]:
        rows = await self._pool.fetch(
            """
            SELECT gbp_review_id, reply_text, reply_at FROM review
            WHERE google_sub = $1 AND location = $2 AND removed_at IS NULL
              AND sent_at > $3 AND reply_text IS NOT NULL
            """,
            google_sub,
            location,
            since,
        )
        return {r["gbp_review_id"]: (r["reply_text"], r["reply_at"]) for r in rows}

    async def claim_action(self, action_id: str, job_id: str, at: datetime) -> ClaimedAction | None:
        # One statement: a pending action is taken by this job, and a running one only
        # by the job that already holds it. Two jobs can never both get it.
        row = await self._pool.fetchrow(
            """
            UPDATE action a
            SET state = 'running', job_id = $2, attempts = a.attempts + 1, started_at = $3
            FROM review r
            WHERE a.id = $1 AND r.id = a.review_id
              AND (a.state = 'pending' OR (a.state = 'running' AND a.job_id = $2))
            RETURNING a.id, a.google_sub, a.location, r.gbp_review_id, a.text, a.attempts
            """,
            action_id,
            job_id,
            at,
        )
        if row is None:
            return None
        return ClaimedAction(
            id=row["id"],
            google_sub=row["google_sub"],
            location=row["location"],
            gbp_review_id=row["gbp_review_id"],
            text=row["text"],
            attempts=row["attempts"],
        )

    async def finish_action(
        self,
        action_id: str,
        job_id: str,
        *,
        done: bool,
        at: datetime,
        google_status: int | None = None,
        reason: str | None = None,
        reply_at: datetime | None = None,
    ) -> bool:
        async with self._pool.acquire() as conn, conn.transaction():
            status = await conn.execute(
                """
                UPDATE action
                SET state = $3, finished_at = $4, google_status = $5, error_code = $6
                WHERE id = $1 AND state = 'running' AND job_id = $2
                """,
                action_id,
                job_id,
                DONE if done else FAILED,
                at,
                google_status,
                None if done else reason,
            )
            if not status.endswith(" 1"):
                return False
            # The audit row copies the text from the action inside the database.
            await conn.execute(
                _AUDIT_FROM_ACTION,
                action_id,
                EVENT_SENT if done else EVENT_FAILED,
                google_status,
                reason,
            )
            if done:
                # The stored review becomes answered with the text of the action, copied
                # inside the database. If it was unanswered, the summary goes down by
                # one; the next sync brings Google's own figures.
                review = await conn.fetchrow(
                    """
                    SELECT r.id, r.google_sub, r.location, r.rating,
                           r.reply_text IS NULL AS was_pending
                    FROM review r JOIN action a ON a.review_id = r.id
                    WHERE a.id = $1
                    FOR UPDATE OF r
                    """,
                    action_id,
                )
                await conn.execute(
                    """
                    UPDATE review r
                    SET reply_text = a.text, reply_at = $2, sent_at = $3
                    FROM action a
                    WHERE a.id = $1 AND r.id = a.review_id
                    """,
                    action_id,
                    reply_at or at,
                    at,
                )
                if review is not None and review["was_pending"]:
                    low = review["rating"] is not None and review["rating"] <= LOW_RATING_MAX
                    await conn.execute(
                        """
                        UPDATE profile_summary
                        SET unanswered_count = greatest(unanswered_count - 1, 0),
                            unanswered_low_count = greatest(unanswered_low_count - $3, 0)
                        WHERE google_sub = $1 AND location = $2
                        """,
                        review["google_sub"],
                        review["location"],
                        int(low),
                    )
        return True

    async def mark_lost_actions(self, kind: str) -> int:
        # A failed job whose action is still live: nobody closed it (the worker died
        # during the last attempt). Closed here with its audit row, in one statement.
        status = await self._pool.execute(
            """
            WITH closed AS (
                UPDATE action a
                SET state = 'failed', finished_at = j.finished_at, error_code = $2
                FROM job j
                WHERE j.kind = $1 AND j.state = 'failed'
                  AND j.finished_at > now() - interval '1 day'
                  AND j.payload->>'action_id' = a.id
                  AND a.state IN ('pending', 'running')
                RETURNING a.id, a.account_id, a.type, a.location, a.review_id, a.text,
                          a.confirmed_at
            )
            INSERT INTO audit_log
                (event, account_id, action_id, action_type, location, review_id,
                 gbp_review_id, text, confirmed_at, reason)
            SELECT $3, c.account_id, c.id, c.type, c.location, c.review_id, r.gbp_review_id,
                   c.text, c.confirmed_at, $2
            FROM closed c JOIN review r ON r.id = c.review_id
            """,
            kind,
            ERROR_UNAVAILABLE,
            EVENT_FAILED,
        )
        return int(status.rsplit(" ", 1)[-1])
