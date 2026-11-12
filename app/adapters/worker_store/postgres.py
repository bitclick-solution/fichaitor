"""Worker's view of the database: Google connections (read), profile summaries,
reviews, the actions it carries out with their audit trail, and the sentiment analysis
(the classification of each review, the analysis of each profile, its monthly history
and the usage of the model).

The worker role cannot create an action or change what one says (its text, its review,
its account): it only records progress and outcome. It can only add to the audit log.

It is also the only one that deletes: it makes a leave effective and runs the
retention task (docs/retencion.md).
"""

import json
from collections.abc import Mapping, Sequence
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
from app.domain.offboarding import SCOPE_ACCOUNT, RetentionPolicy
from app.domain.profile_summary import (
    LOW_RATING_MAX,
    STATUS_ERROR,
    STATUS_NEEDS_RECONNECT,
    STATUS_OK,
    ProfileSummary,
    Review,
    facts_to_json,
)
from app.domain.reviews import storable_reviews, storable_text
from app.domain.sentiment import (
    STATUS_ERROR as SENTIMENT_ERROR,
)
from app.domain.sentiment import (
    STATUS_OK as SENTIMENT_OK,
)
from app.domain.sentiment import (
    Analysis,
    LastAnalysis,
    SentimentReview,
    StoredClassification,
    Usage,
    month_of,
    topics_from_json,
    topics_to_json,
)
from app.ports.sentiment_store import SentimentContext
from app.ports.worker_store import (
    ClaimedAction,
    ConnectedLocation,
    EffectiveLeave,
    StoredConnection,
    TokenToRevoke,
)

# The review of an action, only if it is of the action's own Google user and profile:
# a forged action never borrows the Google id of somebody else's review. Without it the
# audit row still gets written, with an empty review id.
_AUDIT_FROM_ACTION = """
    INSERT INTO audit_log
        (event, account_id, action_id, action_type, location, review_id, gbp_review_id,
         text, confirmed_at, google_status, reason)
    SELECT $2, a.account_id, a.id, a.type, a.location, a.review_id,
           coalesce(r.gbp_review_id, ''), a.text, a.confirmed_at, $3, $4
    FROM action a
    LEFT JOIN review r
      ON r.id = a.review_id AND r.google_sub = a.google_sub AND r.location = a.location
    WHERE a.id = $1
"""


class PostgresWorkerStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def latest_connection(self, google_sub: str) -> StoredConnection | None:
        row = await self._pool.fetchrow(
            """
            SELECT id::text AS id, refresh_token_enc, key_version, locations::text AS locations
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
            connection_id=row["id"],
        )

    async def drop_replaced_connections(self, google_sub: str, connection_id: str) -> int:
        # Older in the same order latest_connection uses. A connection stored while the
        # sync ran is newer and stays.
        status = await self._pool.execute(
            """
            DELETE FROM google_connection c
            USING google_connection kept
            WHERE kept.id = $2::uuid AND kept.google_sub = $1 AND c.google_sub = $1
              AND (c.connected_at, c.id) < (kept.connected_at, kept.id)
            """,
            google_sub,
            connection_id,
        )
        return _deleted(status)

    async def save_summary(
        self, google_sub: str, location: str, summary: ProfileSummary, at: datetime
    ) -> None:
        await self._pool.execute(
            """
            INSERT INTO profile_summary
                (google_sub, location, status, status_at, synced_at, review_count,
                 average_rating, unanswered_count, unanswered_low_count, last_review_at,
                 has_regular_hours, upcoming_special_hours, google_changed,
                 google_changed_fields, facts, denied_count)
            VALUES ($1, $2, $3, $4, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14::jsonb, 0)
            ON CONFLICT (google_sub, location) DO UPDATE SET
                denied_count = 0,
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

    async def record_denied(
        self, google_sub: str, location: str, at: datetime, lost_after: int
    ) -> bool:
        async with self._pool.acquire() as conn, conn.transaction():
            denied = await conn.fetchval(
                """
                INSERT INTO profile_summary (google_sub, location, status, status_at, denied_count)
                VALUES ($1, $2, $3, $4, 1)
                ON CONFLICT (google_sub, location) DO UPDATE SET
                    status = EXCLUDED.status,
                    status_at = EXCLUDED.status_at,
                    denied_count = least(profile_summary.denied_count + 1, 1000)
                RETURNING denied_count
                """,
                google_sub,
                location,
                STATUS_ERROR,
                at,
            )
            if denied < lost_after:
                return False
            # The count starts again: after a reconnection it takes as many refusals.
            await conn.execute(
                """
                UPDATE profile_summary SET status = $3, denied_count = 0
                WHERE google_sub = $1 AND location = $2
                """,
                google_sub,
                location,
                STATUS_NEEDS_RECONNECT,
            )
            await conn.execute(
                """
                UPDATE location SET removed_at = $3
                WHERE gbp_location = $2 AND removed_at IS NULL
                  AND account_id IN (SELECT id FROM account WHERE google_sub = $1)
                """,
                google_sub,
                location,
                at,
            )
        return True

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
        # The API role can insert any action (M1 of docs/seguridad/revision-2026-11-12.md):
        # the same statement checks that the account, its Google identity, its profile
        # and the review agree. There is no foreign key for this on purpose: the action
        # outlives its review (12 months against 30 days).
        row = await self._pool.fetchrow(
            """
            UPDATE action a
            SET state = 'running', job_id = $2, attempts = a.attempts + 1, started_at = $3
            WHERE a.id = $1
              AND (a.state = 'pending' OR (a.state = 'running' AND a.job_id = $2))
            RETURNING a.id, a.google_sub, a.location, a.text, a.attempts,
                (SELECT r.gbp_review_id FROM review r
                 WHERE r.id = a.review_id AND r.google_sub = a.google_sub
                   AND r.location = a.location) AS gbp_review_id,
                EXISTS (SELECT 1 FROM account c JOIN location l ON l.account_id = c.id
                        WHERE c.id = a.account_id AND c.google_sub = a.google_sub
                          AND c.deleted_at IS NULL AND l.gbp_location = a.location
                          AND l.removed_at IS NULL) AS owned
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
            gbp_review_id=row["gbp_review_id"] or "",
            text=row["text"],
            attempts=row["attempts"],
            coherent=row["gbp_review_id"] is not None and row["owned"],
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
                RETURNING a.id, a.account_id, a.type, a.google_sub, a.location, a.review_id,
                          a.text, a.confirmed_at
            )
            INSERT INTO audit_log
                (event, account_id, action_id, action_type, location, review_id,
                 gbp_review_id, text, confirmed_at, reason)
            SELECT $3, c.account_id, c.id, c.type, c.location, c.review_id,
                   coalesce(r.gbp_review_id, ''), c.text, c.confirmed_at, $2
            FROM closed c
            LEFT JOIN review r
              ON r.id = c.review_id AND r.google_sub = c.google_sub AND r.location = c.location
            """,
            kind,
            ERROR_UNAVAILABLE,
            EVENT_FAILED,
        )
        return int(status.rsplit(" ", 1)[-1])

    # --- sentiment ----------------------------------------------------------------

    async def sentiment_context(self, google_sub: str, location: str) -> SentimentContext:
        row = await self._pool.fetchrow(
            """
            SELECT s.status, s.generated_at, s.based_on, s.formula_version,
                   (SELECT p.facts->>'category' FROM profile_summary p
                    WHERE p.google_sub = $1 AND p.location = $2) AS category
            FROM (SELECT 1) AS one
            LEFT JOIN sentiment_analysis s ON s.google_sub = $1 AND s.location = $2
            """,
            google_sub,
            location,
        )
        last = None
        if row["status"] is not None:
            last = LastAnalysis(
                row["status"], row["generated_at"], row["based_on"], row["formula_version"]
            )
        return SentimentContext(last, row["category"] or "")

    async def sentiment_reviews(self, google_sub: str, location: str) -> list[SentimentReview]:
        rows = await self._pool.fetch(
            """
            SELECT r.id, r.rating, r.comment, r.created_at,
                   r.reply_text IS NOT NULL AS replied,
                   c.source_hash, c.sentiment, c.topics::text AS topics, c.method, c.attempts
            FROM review r LEFT JOIN review_sentiment c ON c.review_id = r.id
            WHERE r.google_sub = $1 AND r.location = $2 AND r.removed_at IS NULL
            ORDER BY r.id
            """,
            google_sub,
            location,
        )
        reviews = []
        for row in rows:
            stored = None
            if row["source_hash"] is not None:
                stored = StoredClassification(
                    row["source_hash"],
                    row["sentiment"],
                    topics_from_json(json.loads(row["topics"])),
                    row["method"],
                    row["attempts"],
                )
            reviews.append(
                SentimentReview(
                    row["id"],
                    row["rating"],
                    row["comment"],
                    row["created_at"],
                    stored,
                    replied=row["replied"],
                )
            )
        return reviews

    async def save_classifications(
        self, classified: Mapping[str, StoredClassification], at: datetime
    ) -> None:
        ids = sorted(classified)
        if not ids:
            return
        rows = [classified[review_id] for review_id in ids]
        # Only for reviews that still exist: the join drops any other id.
        await self._pool.execute(
            """
            INSERT INTO review_sentiment
                (review_id, source_hash, sentiment, topics, method, attempts, classified_at)
            SELECT r.id, c.source_hash, c.sentiment, c.topics::jsonb, c.method, c.attempts, $7
            FROM unnest($1::text[], $2::text[], $3::text[], $4::text[], $5::text[],
                        $6::smallint[])
                 AS c (review_id, source_hash, sentiment, topics, method, attempts)
            JOIN review r ON r.id = c.review_id
            ORDER BY r.id
            ON CONFLICT (review_id) DO UPDATE SET
                source_hash = EXCLUDED.source_hash,
                sentiment = EXCLUDED.sentiment,
                topics = EXCLUDED.topics,
                method = EXCLUDED.method,
                attempts = EXCLUDED.attempts,
                classified_at = EXCLUDED.classified_at
            """,
            ids,
            [c.source_hash for c in rows],
            [c.sentiment for c in rows],
            [json.dumps(topics_to_json(c.topics), ensure_ascii=False) for c in rows],
            [c.method for c in rows],
            [c.attempts for c in rows],
            at,
        )

    async def save_analysis(
        self, google_sub: str, location: str, analysis: Analysis, usage: Usage
    ) -> None:
        figures = analysis.figures

        def counts(topics) -> str:
            listed = [{"topic": t.topic, "mentions": t.mentions} for t in topics]
            return json.dumps(listed, ensure_ascii=False)

        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO sentiment_analysis
                    (google_sub, location, status, generated_at, based_on, with_text,
                     provisional, period, score, overall, summary, suggestion,
                     summary_source, praised, complaints, trigger, formula_version)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                        $14::jsonb, $15::jsonb, $16, $17)
                ON CONFLICT (google_sub, location) DO UPDATE SET
                    status = EXCLUDED.status,
                    generated_at = EXCLUDED.generated_at,
                    based_on = EXCLUDED.based_on,
                    with_text = EXCLUDED.with_text,
                    provisional = EXCLUDED.provisional,
                    period = EXCLUDED.period,
                    score = EXCLUDED.score,
                    overall = EXCLUDED.overall,
                    summary = EXCLUDED.summary,
                    suggestion = EXCLUDED.suggestion,
                    summary_source = EXCLUDED.summary_source,
                    praised = EXCLUDED.praised,
                    complaints = EXCLUDED.complaints,
                    trigger = EXCLUDED.trigger,
                    formula_version = EXCLUDED.formula_version
                """,
                google_sub,
                location,
                figures.status,
                analysis.generated_at,
                figures.based_on,
                figures.with_text,
                figures.provisional,
                figures.period,
                figures.score,
                figures.overall,
                analysis.summary,
                analysis.suggestion,
                analysis.summary_source,
                counts(figures.praised),
                counts(figures.complaints),
                analysis.trigger,
                analysis.formula_version,
            )
            if figures.status == SENTIMENT_OK:
                # One snapshot per month: the current month follows the latest analysis.
                await conn.execute(
                    """
                    INSERT INTO sentiment_history
                        (google_sub, location, month, score, based_on, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (google_sub, location, month) DO UPDATE SET
                        score = EXCLUDED.score,
                        based_on = EXCLUDED.based_on,
                        updated_at = EXCLUDED.updated_at
                    """,
                    google_sub,
                    location,
                    month_of(analysis.generated_at),
                    figures.score,
                    figures.based_on,
                    analysis.generated_at,
                )
            # Counters only: no text of a review or of the summary.
            await conn.execute(
                """
                INSERT INTO sentiment_usage
                    (google_sub, location, trigger, classify_calls, summary_calls,
                     failed_calls, reviews_sent, input_tokens, output_tokens)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                google_sub,
                location,
                analysis.trigger,
                usage.classify_calls,
                usage.summary_calls,
                usage.failed_calls,
                usage.reviews_sent,
                usage.input_tokens,
                usage.output_tokens,
            )

    async def save_analysis_error(self, google_sub: str, location: str, at: datetime) -> None:
        # Only where there is nothing: a good analysis is never replaced by an error.
        await self._pool.execute(
            """
            INSERT INTO sentiment_analysis (google_sub, location, status, generated_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (google_sub, location) DO NOTHING
            """,
            google_sub,
            location,
            SENTIMENT_ERROR,
            at,
        )

    # --- leaving and retention ----------------------------------------------------
    # The only place in the product where rows are deleted. Every date comes from the
    # caller's clock, never from now() in SQL: what is deleted is exactly what is past
    # its term at that moment.

    async def is_suspended(self, google_sub: str) -> bool:
        return await self._pool.fetchval(
            """
            SELECT EXISTS (SELECT 1 FROM offboarding
                           WHERE google_sub = $1 AND restored_at IS NULL
                             AND effective_at IS NULL)
            """,
            google_sub,
        )

    async def purge_superseded(self, google_sub: str, now: datetime) -> dict[str, int]:
        ids = await self._pool.fetch(
            """
            SELECT o.id FROM offboarding o
            WHERE o.google_sub = $1 AND o.effective_at IS NOT NULL AND o.purged_at IS NULL
              AND EXISTS (SELECT 1 FROM google_connection c
                          WHERE c.google_sub = o.google_sub AND c.connected_at > o.effective_at)
            """,
            google_sub,
        )
        total: dict[str, int] = {}
        for row in ids:
            _add(total, await self._purge_leave(row["id"], now))
        return total

    async def begin_offboarding(self, offboarding_id: str, now: datetime) -> EffectiveLeave | None:
        async with self._pool.acquire() as conn, conn.transaction():
            # One statement decides it: a leave restored a moment ago is not touched,
            # and once this is written it can no longer be restored.
            begun = await conn.fetchrow(
                """
                UPDATE offboarding SET effective_at = $2, restore_token_hash = NULL
                WHERE id = $1::uuid AND restored_at IS NULL AND effective_at IS NULL
                  AND delete_at <= $2
                RETURNING account_id, scope
                """,
                offboarding_id,
                now,
            )
            if begun is not None:
                account_id = begun["account_id"]
                await conn.execute(
                    "UPDATE location SET removed_at = $2"
                    " WHERE account_id = $1 AND removed_at IS NULL",
                    account_id,
                    now,
                )
                if begun["scope"] == SCOPE_ACCOUNT:
                    await conn.execute(
                        "UPDATE session SET revoked_at = $2"
                        " WHERE account_id = $1 AND revoked_at IS NULL",
                        account_id,
                        now,
                    )
                    # The trigger empties the email and frees the Google identity.
                    await conn.execute(
                        """
                        UPDATE account
                        SET deleted_at = $2, disconnected_at = $2, selected_location_id = NULL
                        WHERE id = $1
                        """,
                        account_id,
                        now,
                    )
                else:
                    await conn.execute(
                        """
                        UPDATE account SET disconnected_at = $2, selected_location_id = NULL
                        WHERE id = $1
                        """,
                        account_id,
                        now,
                    )
            row = await conn.fetchrow(
                """
                SELECT id::text AS id, scope, google_sub FROM offboarding
                WHERE id = $1::uuid AND effective_at IS NOT NULL AND revoke_state IS NULL
                  AND google_sub IS NOT NULL
                """,
                offboarding_id,
            )
        return EffectiveLeave(row["id"], row["scope"], row["google_sub"]) if row else None

    async def leave_tokens(self, offboarding_id: str) -> tuple[list[TokenToRevoke], bool]:
        rows = await self._pool.fetch(
            """
            SELECT c.id::text AS id, c.refresh_token_enc, c.key_version,
                   c.connected_at > o.effective_at AS newer
            FROM offboarding o JOIN google_connection c ON c.google_sub = o.google_sub
            WHERE o.id = $1::uuid AND o.effective_at IS NOT NULL
            ORDER BY c.connected_at, c.id
            """,
            offboarding_id,
        )
        tokens = [
            TokenToRevoke(r["id"], r["refresh_token_enc"], r["key_version"])
            for r in rows
            if not r["newer"]
        ]
        return tokens, any(r["newer"] for r in rows)

    async def delete_connection(self, connection_id: str) -> None:
        await self._pool.execute("DELETE FROM google_connection WHERE id = $1::uuid", connection_id)

    async def finish_revocation(self, offboarding_id: str, state: str) -> None:
        await self._pool.execute(
            """
            UPDATE offboarding SET revoke_state = $2
            WHERE id = $1::uuid AND effective_at IS NOT NULL AND revoke_state IS NULL
            """,
            offboarding_id,
            state,
        )

    async def due_offboardings(self, now: datetime) -> list[tuple[str, bool]]:
        rows = await self._pool.fetch(
            """
            SELECT id::text AS id,
                   coalesce(effective_at <= $1::timestamptz - interval '1 day', false) AS give_up
            FROM offboarding
            WHERE restored_at IS NULL AND revoke_state IS NULL AND google_sub IS NOT NULL
              AND (effective_at <= $1 OR (effective_at IS NULL AND delete_at <= $1))
            ORDER BY delete_at, id
            """,
            now,
        )
        return [(r["id"], r["give_up"]) for r in rows]

    async def _purge_leave(self, offboarding_id: object, now: datetime) -> dict[str, int]:
        """Delete everything obtained from Google for the user of an effective leave.
        The row is locked: two workers never purge the same leave, and a second call
        finds it done."""
        counts: dict[str, int] = {}
        async with self._pool.acquire() as conn, conn.transaction():
            leave = await conn.fetchrow(
                """
                SELECT account_id, google_sub, effective_at FROM offboarding
                WHERE id = $1 AND effective_at IS NOT NULL AND purged_at IS NULL
                FOR UPDATE SKIP LOCKED
                """,
                offboarding_id,
            )
            if leave is None:
                return counts
            sub, effective_at = leave["google_sub"], leave["effective_at"]
            if sub is not None:
                counts["review_sentiment"] = await conn.fetchval(
                    """
                    SELECT count(*) FROM review_sentiment s JOIN review r ON r.id = s.review_id
                    WHERE r.google_sub = $1
                    """,
                    sub,
                )
                # review_sentiment and review_draft go with their review (ON DELETE CASCADE).
                for table in (
                    "review",
                    "profile_summary",
                    "sentiment_analysis",
                    "sentiment_history",
                    "sentiment_usage",
                ):
                    counts[table] = _deleted(
                        await conn.execute(
                            f"DELETE FROM {table} WHERE google_sub = $1",  # noqa: S608
                            sub,
                        )
                    )
                # Tokens are deleted when the leave becomes effective; this is what a
                # dead worker may have left behind. A newer connection is not touched.
                counts["google_connection"] = _deleted(
                    await conn.execute(
                        "DELETE FROM google_connection"
                        " WHERE google_sub = $1 AND connected_at <= $2",
                        sub,
                        effective_at,
                    )
                )
                # Jobs of before the leave carry the Google user and the profile.
                counts["job"] = _deleted(
                    await conn.execute(
                        """
                        DELETE FROM job
                        WHERE payload->>'google_sub' = $1 AND state <> 'running'
                          AND created_at <= $2
                        """,
                        sub,
                        effective_at,
                    )
                )
            # The profiles the leave retired. One the owner connected again is a new
            # read of Google and stays.
            counts["location"] = _deleted(
                await conn.execute(
                    "DELETE FROM location WHERE account_id = $1 AND removed_at IS NOT NULL",
                    leave["account_id"],
                )
            )
            await conn.execute(
                "UPDATE offboarding SET purged_at = $2, google_sub = NULL WHERE id = $1",
                offboarding_id,
                now,
            )
        return counts

    async def run_retention(self, now: datetime, policy: RetentionPolicy) -> dict[str, int]:
        counts: dict[str, int] = {}
        due = await self._pool.fetch(
            """
            SELECT id FROM offboarding
            WHERE effective_at IS NOT NULL AND purged_at IS NULL
              AND effective_at <= $1::timestamptz - make_interval(days => $2)
            ORDER BY effective_at, id
            """,
            now,
            policy.google_data_days,
        )
        for row in due:
            _add(counts, await self._purge_leave(row["id"], now))

        async with self._pool.acquire() as conn, conn.transaction():
            removed_before = "$1::timestamptz - make_interval(days => $2)"
            # Profiles Google stopped listing: what was read from them, then the profile.
            for table in (
                "review",
                "profile_summary",
                "sentiment_analysis",
                "sentiment_history",
                "sentiment_usage",
            ):
                _add(
                    counts,
                    {
                        table: _deleted(
                            await conn.execute(
                                f"""
                                DELETE FROM {table} t USING location l, account a
                                WHERE a.id = l.account_id AND l.removed_at <= {removed_before}
                                  AND t.google_sub = a.google_sub AND t.location = l.gbp_location
                                """,  # noqa: S608
                                now,
                                policy.removed_days,
                            )
                        )
                    },
                )
            # A retired profile may still be the selected one of its account (the
            # worker retires it when the access is lost): it stops being so first.
            await conn.execute(
                f"""
                UPDATE account SET selected_location_id = NULL
                WHERE selected_location_id IN (
                    SELECT id FROM location WHERE removed_at <= {removed_before})
                """,  # noqa: S608
                now,
                policy.removed_days,
            )
            _add(
                counts,
                {
                    "location": _deleted(
                        await conn.execute(
                            f"DELETE FROM location WHERE removed_at <= {removed_before}",  # noqa: S608
                            now,
                            policy.removed_days,
                        )
                    )
                },
            )
            # Reviews Google stopped returning.
            _add(
                counts,
                {
                    "review": _deleted(
                        await conn.execute(
                            f"DELETE FROM review WHERE removed_at <= {removed_before}",  # noqa: S608
                            now,
                            policy.removed_days,
                        )
                    )
                },
            )
            counts["session"] = _deleted(
                await conn.execute(
                    """
                    DELETE FROM session
                    WHERE revoked_at <= $1::timestamptz - make_interval(days => $2)
                       OR expires_at <= $1::timestamptz - make_interval(days => $2)
                    """,
                    now,
                    policy.session_days,
                )
            )
            counts["offboarding_code"] = _deleted(
                await conn.execute(
                    """
                    DELETE FROM offboarding_code
                    WHERE expires_at <= $1::timestamptz - make_interval(days => $2)
                    """,
                    now,
                    policy.code_days,
                )
            )
            # Counters of the caps on paid calls whose window has ended.
            counts["usage_counter"] = _deleted(
                await conn.execute("DELETE FROM usage_counter WHERE expires_at <= $1", now)
            )
            launched = policy.waitlist_launched_at
            counts["waitlist_entry"] = _deleted(
                await conn.execute(
                    """
                    DELETE FROM waitlist_entry
                    WHERE created_at <= $1::timestamptz - make_interval(months => $2)
                       OR created_at < $3::timestamptz
                    """,
                    now,
                    policy.waitlist_months,
                    launched if launched is not None and launched <= now else None,
                )
            )
            # The record of published replies: the actions with their text and the audit
            # trail of accounts that have been without Google for longer than the term.
            audit_before = await conn.fetchval(
                "SELECT $1::timestamptz - make_interval(months => $2)", now, policy.audit_months
            )
            counts["action"] = _deleted(
                await conn.execute(
                    """
                    DELETE FROM action x USING account a
                    WHERE a.id = x.account_id AND a.disconnected_at <= $1
                    """,
                    audit_before,
                )
            )
            # The worker cannot read audit_log, so this DELETE has no WHERE: the policy
            # of the table chooses the rows (accounts on leave for more than 12 months)
            # and this parameter can only narrow it further.
            await conn.execute(
                "SELECT set_config('fichaitor.audit_cutoff', $1, true)", audit_before.isoformat()
            )
            counts["audit_log"] = _deleted(await conn.execute("DELETE FROM audit_log"))
            await conn.execute(
                "INSERT INTO retention_run (deleted) VALUES ($1::jsonb)", json.dumps(counts)
            )
        return counts

    async def scrub_job(self, job_id: str) -> None:
        await self._pool.execute("UPDATE job SET payload = '{}'::jsonb WHERE id = $1::uuid", job_id)

    async def scrub_finished_jobs(self, kind: str) -> int:
        status = await self._pool.execute(
            """
            UPDATE job SET payload = '{}'::jsonb
            WHERE kind = $1 AND state IN ('done', 'failed') AND payload <> '{}'::jsonb
            """,
            kind,
        )
        return _deleted(status)

    async def set_code_mail(self, code_id: str, state: str) -> None:
        await self._pool.execute(
            "UPDATE offboarding_code SET mail_state = $2 WHERE id = $1::uuid", code_id, state
        )


def _deleted(status: str) -> int:
    """Rows affected, from the status of a statement ("DELETE 3")."""
    return int(status.rsplit(" ", 1)[-1])


def _add(total: dict[str, int], counts: dict[str, int]) -> None:
    for table, count in counts.items():
        total[table] = total.get(table, 0) + count
