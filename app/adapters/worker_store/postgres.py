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
from datetime import datetime, timedelta

import asyncpg

from app.adapters.job_queue.postgres import LOST_WORKER_ERROR
from app.domain.actions import (
    AUDIT_EVENTS,
    DONE,
    ERROR_UNAVAILABLE,
    EVENT_FAILED,
    EVENT_HOURS_FAILED,
    FAILED,
    REPLY_REVIEW,
    SET_SPECIAL_HOURS,
)
from app.domain.offboarding import (
    FLOOR_HOURS,
    REASON_EMAIL_REQUEST,
    SCOPE_ACCOUNT,
    RetentionPolicy,
)
from app.domain.profile_summary import (
    LOW_RATING_MAX,
    STATUS_ERROR,
    STATUS_NEEDS_RECONNECT,
    STATUS_OK,
    ProfileSummary,
    Review,
    facts_to_json,
)
from app.domain.quota import ALLOWANCE_RETENTION_YEARS
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
    NewReview,
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
    SELECT $2, a.account_id, a.id, a.type, a.location, coalesce(a.review_id, ''),
           coalesce(r.gbp_review_id, ''), a.text, a.confirmed_at, $3, $4
    FROM action a
    LEFT JOIN review r
      ON r.id = a.review_id AND r.google_sub = a.google_sub AND r.location = a.location
    WHERE a.id = $1
"""


# A leave as the API writes it (M5 of docs/seguridad/revision-2026-11-12-b.md): of the
# Google user of that very account, still not deleted, and scheduled with the floor of
# hours the database imposes. Or one support processed for an email request
# (scripts/baja_manual.py): only the owner of the database can write that reason
# (offboarding_guard and the column privileges). It is scheduled 48 hours like one of the
# panel (B15), or effective at once with --inmediata, which is why the floor does not
# apply to it.
# `o` is the leave.
_COHERENT_LEAVE = f"""
    EXISTS (SELECT 1 FROM account a
            WHERE a.id = o.account_id AND a.google_sub = o.google_sub
              AND a.deleted_at IS NULL)
    AND (o.delete_at >= o.confirmed_at + interval '{FLOOR_HOURS} hours'
         OR o.reason = '{REASON_EMAIL_REQUEST}')
"""

# A profile in use in its account (multinegocio, docs/multinegocio.md): an active one.
# The preference of the panel (account.selected_location_id) decides nothing here since
# step 9. `l` is the location.
_IN_USE = "(l.removed_at IS NULL AND l.active_since IS NOT NULL)"


class PostgresWorkerStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def latest_connection(self, google_sub: str) -> StoredConnection | None:
        row = await self._pool.fetchrow(
            """
            SELECT id::text AS id, refresh_token_enc, key_version, locations::text AS locations
            FROM google_connection
            WHERE google_sub = $1 AND revoked_at IS NULL
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

    async def revoke_connection(self, google_sub: str, connection_id: str, at: datetime) -> bool:
        async with self._pool.acquire() as conn, conn.transaction():
            # The row stays, without its token, as the mark of when the access went: the
            # countdown counts from it. The trigger google_connection_guard lets the
            # worker do this and nothing else to a connection.
            await conn.execute(
                """
                UPDATE google_connection SET revoked_at = $3, refresh_token_enc = NULL
                WHERE id = $2::uuid AND google_sub = $1 AND revoked_at IS NULL
                """,
                google_sub,
                connection_id,
                at,
            )
            # Only this one (B14 of docs/seguridad/revision-2026-11-12-c.md). The older
            # ones are not deleted here: the API can store a connection with a junk token
            # for anybody, and its invalid_grant must not take the real one with it. Each
            # older one falls by its own invalid_grant, if it is dead too, and
            # latest_connection moves on to the next valid one. Older ones go only after
            # a newer one has worked (drop_replaced_connections).
            return not await conn.fetchval(
                """
                SELECT EXISTS (SELECT 1 FROM google_connection
                               WHERE google_sub = $1 AND revoked_at IS NULL)
                """,
                google_sub,
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
    ) -> list[NewReview]:
        rows = sorted(storable_reviews(reviews), key=lambda r: r.review_id)
        ids = [r.review_id for r in rows]
        async with self._pool.acquire() as conn, conn.transaction():
            # No column for the author exists. xmax = 0: the row was inserted now, not
            # updated (what the messaging channels may propose).
            saved = await conn.fetch(
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
                RETURNING id, created_at, reply_text IS NULL AS unanswered,
                          (xmax = 0) AS inserted
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
        return [
            NewReview(r["id"], r["created_at"]) for r in saved if r["inserted"] and r["unanswered"]
        ]

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
        # Multinegocio (docs/multinegocio.md, section 4): and that its profile is still in
        # use in the account. One deactivated after the owner confirmed is not sent.
        # Cobro (step 7 of docs/admin-facturacion.md): and that its account is not blocked
        # for non-payment. One of a blocked account fails as billing_blocked.
        row = await self._pool.fetchrow(
            f"""
            UPDATE action a
            SET state = 'running', job_id = $2, attempts = a.attempts + 1, started_at = $3
            WHERE a.id = $1
              AND (a.state = 'pending' OR (a.state = 'running' AND a.job_id = $2))
            RETURNING a.id, a.type, a.google_sub, a.location, a.text, a.attempts,
                a.special_date,
                (SELECT r.gbp_review_id FROM review r
                 WHERE r.id = a.review_id AND r.google_sub = a.google_sub
                   AND r.location = a.location) AS gbp_review_id,
                EXISTS (SELECT 1 FROM account c JOIN location l ON l.account_id = c.id
                        WHERE c.id = a.account_id AND c.google_sub = a.google_sub
                          AND c.deleted_at IS NULL AND l.gbp_location = a.location
                          AND l.removed_at IS NULL) AS owned,
                EXISTS (SELECT 1 FROM account c JOIN location l ON l.account_id = c.id
                        WHERE c.id = a.account_id AND l.gbp_location = a.location
                          AND {_IN_USE}) AS active,
                fichaitor_account_blocked(a.account_id) AS blocked
            """,  # noqa: S608
            action_id,
            job_id,
            at,
        )
        if row is None:
            return None
        if row["type"] == SET_SPECIAL_HOURS:
            target = row["special_date"] is not None
        else:
            target = row["type"] == REPLY_REVIEW and row["gbp_review_id"] is not None
        return ClaimedAction(
            id=row["id"],
            google_sub=row["google_sub"],
            location=row["location"],
            gbp_review_id=row["gbp_review_id"] or "",
            text=row["text"],
            attempts=row["attempts"],
            coherent=target and row["owned"],
            type=row["type"],
            special_date=row["special_date"],
            active=row["active"],
            blocked=row["blocked"],
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
            closed = await conn.fetchrow(
                """
                UPDATE action
                SET state = $3, finished_at = $4, google_status = $5, error_code = $6
                WHERE id = $1 AND state = 'running' AND job_id = $2
                RETURNING type, special_date
                """,
                action_id,
                job_id,
                DONE if done else FAILED,
                at,
                google_status,
                None if done else reason,
            )
            if closed is None:
                return False
            events = AUDIT_EVENTS.get(closed["type"], AUDIT_EVENTS[REPLY_REVIEW])
            # The audit row copies the text from the action inside the database.
            await conn.execute(
                _AUDIT_FROM_ACTION,
                action_id,
                events[1] if done else events[2],
                google_status,
                reason,
            )
            if done and closed["type"] == SET_SPECIAL_HOURS:
                # The day now has special hours: the planner and the diagnosis see it
                # before the next sync, which brings Google's own list anyway.
                await conn.execute(
                    """
                    UPDATE profile_summary p
                    SET facts = jsonb_set(p.facts, '{special_dates}', (
                        SELECT coalesce(jsonb_agg(d ORDER BY d), '[]'::jsonb) FROM (
                            SELECT jsonb_array_elements_text(p.facts->'special_dates') AS d
                            UNION SELECT $2::text) AS s))
                    FROM action a
                    WHERE a.id = $1 AND p.google_sub = a.google_sub AND p.location = a.location
                      AND jsonb_typeof(p.facts->'special_dates') = 'array'
                    """,
                    action_id,
                    closed["special_date"].isoformat(),
                )
            if done and closed["type"] == REPLY_REVIEW:
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
            SELECT CASE c.type WHEN $4 THEN $5 ELSE $3 END, c.account_id, c.id, c.type,
                   c.location, coalesce(c.review_id, ''),
                   coalesce(r.gbp_review_id, ''), c.text, c.confirmed_at, $2
            FROM closed c
            LEFT JOIN review r
              ON r.id = c.review_id AND r.google_sub = c.google_sub AND r.location = c.location
            """,
            kind,
            ERROR_UNAVAILABLE,
            EVENT_FAILED,
            SET_SPECIAL_HOURS,
            EVENT_HOURS_FAILED,
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

    async def billing_blocked(self, google_sub: str) -> bool:
        # The same condition as the API and the conversation (db/schema_billing.sql). One
        # Google user has one account (account.google_sub is unique).
        return await self._pool.fetchval(
            """
            SELECT EXISTS (SELECT 1 FROM account c
                           WHERE c.google_sub = $1 AND fichaitor_account_blocked(c.id))
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
            # And only a leave that is what the API writes (M5): the Google user of that
            # very account, scheduled with the floor of hours. One written by other means
            # is never made effective: nothing is revoked or deleted for it.
            begun = await conn.fetchrow(
                f"""
                UPDATE offboarding o SET effective_at = $2, restore_token_hash = NULL
                WHERE o.id = $1::uuid AND o.restored_at IS NULL AND o.effective_at IS NULL
                  AND o.delete_at <= $2 AND {_COHERENT_LEAVE}
                RETURNING o.account_id, o.scope
                """,  # noqa: S608
                offboarding_id,
                now,
            )
            if begun is not None:
                account_id = begun["account_id"]
                # The messaging channels, in the same transaction: the conversation and
                # its messages (they quote reviews) and, when the account goes, its
                # linked addresses and codes (docs/retencion.md).
                await _purge_channels(conn, account_id, begun["scope"] == SCOPE_ACCOUNT)
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
        # A connection the owner revoked at Google has no token left: there is nothing
        # to revoke. Its row goes with the rest of the data (_purge_leave).
        tokens = [
            TokenToRevoke(r["id"], r["refresh_token_enc"], r["key_version"])
            for r in rows
            if not r["newer"] and r["refresh_token_enc"] is not None
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
            f"""
            SELECT o.id::text AS id,
                   coalesce(o.effective_at <= $1::timestamptz - interval '1 day', false)
                       AS give_up
            FROM offboarding o
            WHERE o.restored_at IS NULL AND o.revoke_state IS NULL AND o.google_sub IS NOT NULL
              AND (o.effective_at <= $1
                   OR (o.effective_at IS NULL AND o.delete_at <= $1 AND {_COHERENT_LEAVE}))
            ORDER BY o.delete_at, o.id
            """,  # noqa: S608
            now,
        )
        return [(r["id"], r["give_up"]) for r in rows]

    async def last_retention_at(self) -> datetime | None:
        return await self._pool.fetchval("SELECT max(at) FROM retention_run")

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
                # Tokens are deleted when the leave becomes effective; the connections
                # that go here are what a dead worker may have left behind, or ones the
                # owner revoked at Google (without token). A newer one is not touched.
                counts.update(await _purge_google_data(conn, sub, effective_at))
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

    async def _purge_revoked(self, google_sub: str, now: datetime, days: int) -> dict[str, int]:
        """Delete what was obtained from Google of a user who removed the access at
        Google `days` ago and has not connected again: the same tables and rules as an
        effective leave of the profile (begin_offboarding and _purge_leave together).
        Decided again under lock: a connection stored in the meantime cancels it.

        The lock is the row of the account (I17 of docs/seguridad/revision-2026-11-12-c.md):
        `FOR UPDATE` on the connections does not stop a new one from being inserted. The
        OAuth callback stores the connection and then writes the account
        (`upsert_account`, which waits for this lock); the connections are read after
        taking it, in a statement of their own, so they include one stored before."""
        counts: dict[str, int] = {}
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT 1 FROM account WHERE google_sub = $1 AND deleted_at IS NULL FOR UPDATE",
                google_sub,
            )
            rows = await conn.fetch(
                "SELECT revoked_at FROM google_connection WHERE google_sub = $1 FOR UPDATE",
                google_sub,
            )
            if not rows or any(r["revoked_at"] is None for r in rows):
                return counts
            if max(r["revoked_at"] for r in rows) > now - timedelta(days=days):
                return counts
            account_id = await conn.fetchval(
                "SELECT id FROM account WHERE google_sub = $1 AND deleted_at IS NULL", google_sub
            )
            if account_id is not None:
                # As when a leave of the profile becomes effective: the conversation
                # (it quotes reviews) goes, the linked chats stay; the account is left
                # without profiles and says it is not connected; its sessions stay.
                await _purge_channels(conn, account_id, False)
                await conn.execute(
                    """
                    UPDATE account
                    SET disconnected_at = coalesce(disconnected_at, $2),
                        selected_location_id = NULL
                    WHERE id = $1
                    """,
                    account_id,
                    now,
                )
                await conn.execute(
                    "UPDATE location SET removed_at = $2"
                    " WHERE account_id = $1 AND removed_at IS NULL",
                    account_id,
                    now,
                )
            counts.update(await _purge_google_data(conn, google_sub, now, only_revoked=True))
            if account_id is not None:
                counts["location"] = _deleted(
                    await conn.execute(
                        "DELETE FROM location WHERE account_id = $1 AND removed_at IS NOT NULL",
                        account_id,
                    )
                )
            counts["revoked_identity"] = 1
        return counts

    async def run_retention(self, now: datetime, policy: RetentionPolicy) -> dict[str, int]:
        counts: dict[str, int] = {}
        # Revoked at Google (docs/retencion.md): every connection of the user revoked,
        # the last one long enough ago. A valid connection cancels the countdown.
        revoked = await self._pool.fetch(
            """
            SELECT google_sub FROM google_connection
            GROUP BY google_sub
            HAVING bool_and(revoked_at IS NOT NULL)
               AND max(revoked_at) <= $1::timestamptz - make_interval(days => $2)
            ORDER BY google_sub
            """,
            now,
            policy.google_data_days,
        )
        for row in revoked:
            _add(
                counts,
                await self._purge_revoked(row["google_sub"], now, policy.google_data_days),
            )
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
            # Profiles the owner deactivated (multinegocio) and did not activate again:
            # what was read from them goes, with the same statements. A retired profile
            # is not one of them: its term counts from removed_at (above) or from the
            # leave, even if the retirement deactivated it. The profile row
            # stays while Google lists it, so it can be activated again (and read from
            # scratch). Activating it empties deactivated_at (trigger of location), so a
            # reactivated profile never matches; repeating this deletes nothing more.
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
                                DELETE FROM {table} t USING location l, account c
                                WHERE c.id = l.account_id AND l.active_since IS NULL
                                  AND l.removed_at IS NULL AND l.deactivated_at
                                      <= $1::timestamptz - make_interval(days => $2)
                                  AND t.google_sub = c.google_sub AND t.location = l.gbp_location
                                """,  # noqa: S608
                                now,
                                policy.inactive_days,
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
            # Messaging channels (docs/canales.md): past their term.
            _add(counts, await _expire_channels(conn, now, policy.channel_days))
            # Counters of the caps on paid calls whose window has ended.
            counts["usage_counter"] = _deleted(
                await conn.execute("DELETE FROM usage_counter WHERE expires_at <= $1", now)
            )
            # The free allowance of replies drafted by the model of each Google profile,
            # 3 years after its last use (docs/retencion.md). The database refuses sooner.
            counts["free_allowance"] = _deleted(
                await conn.execute(
                    "DELETE FROM free_allowance"
                    " WHERE last_used_at <= $1::timestamptz - make_interval(years => $2)",
                    now,
                    ALLOWANCE_RETENTION_YEARS,
                )
            )
            # Billing (docs/admin-facturacion.md, 4.8): what the web asked of the billing
            # process, after 30 days; the ids of the Stripe events processed, after 90.
            counts["billing_request"] = _deleted(
                await conn.execute(
                    "DELETE FROM billing_request"
                    " WHERE created_at <= $1::timestamptz - interval '30 days'",
                    now,
                )
            )
            counts["stripe_event"] = _deleted(
                await conn.execute(
                    "DELETE FROM stripe_event WHERE processed_at IS NOT NULL"
                    " AND received_at <= $1::timestamptz - interval '90 days'",
                    now,
                )
            )
            # Finished jobs (I12): their payload may name the Google user and the
            # profile. Kept: the job that carried out an action, while the action is
            # kept (the trail of who ran it). The daily keys of the system are younger
            # than the term (at least 2 days).
            counts["job_finished"] = _deleted(
                await conn.execute(
                    """
                    DELETE FROM job j
                    WHERE j.state IN ('done', 'failed')
                      AND coalesce(j.finished_at, j.created_at)
                          <= $1::timestamptz - make_interval(days => $2)
                      AND NOT EXISTS (SELECT 1 FROM action x WHERE x.job_id = j.id::text)
                    """,
                    now,
                    policy.job_days,
                )
            )
            # Communications of the backoffice (docs/admin-facturacion.md, annex): 12 months
            # after they were written. The worker cannot read the table: a function of the
            # owner deletes and counts. Not there if db/schema_admin.sql was not applied.
            if await conn.fetchval(
                "SELECT to_regprocedure('public.fichaitor_communication_purge()') IS NOT NULL"
            ):
                counts["admin_communication"] = await conn.fetchval(
                    "SELECT fichaitor_communication_purge()"
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
            # Multinegocio: the activation log and the plan entitlements (no personal
            # data; the billing will need them) go with the same term as the record of
            # replies, from the leave of their account. The worker only reads their
            # account_id.
            for table in ("location_activation_log", "plan_entitlement"):
                counts[table] = _deleted(
                    await conn.execute(
                        f"""
                        DELETE FROM {table} t USING account a
                        WHERE a.id = t.account_id AND a.disconnected_at <= $1
                        """,  # noqa: S608
                        audit_before,
                    )
                )
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


async def _purge_google_data(
    conn: asyncpg.Connection, google_sub: str, before: datetime, *, only_revoked: bool = False
) -> dict[str, int]:
    """Everything obtained from Google for that user, in every profile: the reviews (their
    classification and draft go with them), the summaries and facts, the sentiment
    analysis, its history and usage, the stored connections made up to `before`, and the
    jobs created up to `before` that are not running (they carry the user and the
    profile). The same for an effective leave and for an access revoked at Google; in
    the second, `only_revoked`: a connection stored in the meantime is not touched."""
    counts: dict[str, int] = {}
    counts["review_sentiment"] = await conn.fetchval(
        """
        SELECT count(*) FROM review_sentiment s JOIN review r ON r.id = s.review_id
        WHERE r.google_sub = $1
        """,
        google_sub,
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
                google_sub,
            )
        )
    counts["google_connection"] = _deleted(
        await conn.execute(
            """
            DELETE FROM google_connection
            WHERE google_sub = $1 AND connected_at <= $2
              AND (revoked_at IS NOT NULL OR NOT $3::boolean)
            """,
            google_sub,
            before,
            only_revoked,
        )
    )
    counts["job"] = _deleted(
        await conn.execute(
            """
            DELETE FROM job
            WHERE payload->>'google_sub' = $1 AND state <> 'running' AND created_at <= $2
            """,
            google_sub,
            before,
        )
    )
    return counts


async def _purge_channels(conn: asyncpg.Connection, account_id: object, whole: bool) -> None:
    """Delete the conversation of an account whose leave became effective: its messages,
    its thread and its proposals; with `whole` (the account goes), also its addresses,
    linked or not, and its link codes. In this order, for the foreign keys."""
    await conn.execute(
        """
        DELETE FROM channel_message m
        WHERE m.conversation_id IN (SELECT id FROM conversation WHERE account_id = $1)
           OR m.identity_id IN (SELECT id FROM channel_identity WHERE account_id = $1)
           OR m.proposal_id IN (SELECT id FROM proposal WHERE account_id = $1)
        """,
        account_id,
    )
    await conn.execute("DELETE FROM conversation WHERE account_id = $1", account_id)
    # The lanes before the proposals: a lane's key to its proposal has no ON DELETE.
    await conn.execute("DELETE FROM conversation_lane WHERE account_id = $1", account_id)
    await conn.execute("DELETE FROM proposal WHERE account_id = $1", account_id)
    if whole:
        await conn.execute("DELETE FROM channel_identity WHERE account_id = $1", account_id)
        await conn.execute("DELETE FROM link_code WHERE account_id = $1", account_id)


async def _expire_channels(conn: asyncpg.Connection, now: datetime, days: int) -> dict[str, int]:
    """The daily pass on the tables of the channels. Nothing here names an account:
    every row is chosen by its age."""
    before = await conn.fetchval("SELECT $1::timestamptz - make_interval(days => $2)", now, days)
    counts = {
        "channel_message": _deleted(
            await conn.execute("DELETE FROM channel_message WHERE at <= $1", before)
        ),
        "channel_inbound_event": _deleted(
            await conn.execute("DELETE FROM channel_inbound_event WHERE received_at <= $1", before)
        ),
    }
    # Finished proposals (they quote the review) once nothing points at them.
    counts["proposal"] = _deleted(
        await conn.execute(
            """
            DELETE FROM proposal p
            WHERE p.created_at <= $1
              AND p.state NOT IN ('programada', 'enviada', 'recordada', 'recopilando',
                                  'por_confirmar', 'ejecutando')
              AND NOT EXISTS (SELECT 1 FROM conversation_lane l
                              WHERE l.active_proposal_id = p.id)
              AND NOT EXISTS (SELECT 1 FROM channel_message m WHERE m.proposal_id = p.id)
            """,
            before,
        )
    )
    counts["channel_identity"] = _deleted(
        await conn.execute(
            """
            DELETE FROM channel_identity i
            WHERE i.unlinked_at <= $1
              AND NOT EXISTS (SELECT 1 FROM channel_message m WHERE m.identity_id = i.id)
            """,
            before,
        )
    )
    # Codes: a day after they expired; nothing else needs their hash.
    counts["link_code"] = _deleted(
        await conn.execute(
            "DELETE FROM link_code WHERE expires_at <= $1::timestamptz - interval '1 day'", now
        )
    )
    counts["channel_sender"] = _deleted(
        await conn.execute(
            """
            DELETE FROM channel_sender
            WHERE updated_at <= $1 AND (blocked_until IS NULL OR blocked_until <= $2)
            """,
            before,
            now,
        )
    )
    counts["link_failure_window"] = _deleted(
        await conn.execute(
            """
            DELETE FROM link_failure_window
            WHERE window_start <= $1::timestamptz - interval '1 day'
            """,
            now,
        )
    )
    return counts


def _deleted(status: str) -> int:
    """Rows affected, from the status of a statement ("DELETE 3")."""
    return int(status.rsplit(" ", 1)[-1])


def _add(total: dict[str, int], counts: dict[str, int]) -> None:
    for table, count in counts.items():
        total[table] = total.get(table, 0) + count
