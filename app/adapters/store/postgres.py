"""PostgreSQL store. Explicit SQL, no ORM.

The API role can only INSERT contacts, events and Google connections. On accounts,
profiles and sessions it can read, insert and update a few columns; it never deletes.
Every statement on profiles is filtered by account_id: the isolation between accounts.
Of the worker's tables it can only INSERT jobs and SELECT profile summaries
(figures and facts) and reviews. It keeps the drafts of replies. It creates actions,
which it can read but never change, and it can only add to the audit log.
"""

import json
from collections.abc import Sequence
from datetime import datetime

import asyncpg

from app.adapters.job_queue.postgres import PostgresJobQueue
from app.domain.actions import EVENT_CONFIRMED, REPLY_REVIEW
from app.domain.jobs import DEFAULT_MAX_ATTEMPTS
from app.domain.profile_summary import ProfileFacts, facts_from_json
from app.ports.google_oauth import ProfileLocation
from app.ports.store import (
    Account,
    GoogleConnection,
    Location,
    ReplyInProgress,
    ReviewDraft,
    ReviewPage,
    SelectedProfile,
    StoredAction,
    StoredReview,
    StoredSummary,
    WaitlistSignup,
)

REPLY_MAX_ATTEMPTS = 3  # 30 s and 60 s of waiting: the owner is looking at the screen

# The reviews an account may see: those of its own selected profile, reached through
# its own Google identity. $1 is always the account of the session; no identifier from
# the request takes part in choosing the profile.
_OWN_REVIEWS = """
    FROM account a
    JOIN location l
      ON l.id = a.selected_location_id AND l.account_id = a.id AND l.removed_at IS NULL
    JOIN review r
      ON r.google_sub = a.google_sub AND r.location = l.gbp_location AND r.removed_at IS NULL
"""


class PostgresStore:
    def __init__(self, dsn: str, reply_job_kind: str = REPLY_REVIEW) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        # Kind of the job that carries out a confirmed reply. Tests use one of their
        # own, so that no worker running on the same database takes their jobs.
        self._reply_job_kind = reply_job_kind

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

    async def enqueue(
        self,
        kind: str,
        payload: dict,
        *,
        idempotency_key: str | None = None,
        run_at: datetime | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> bool:
        """JobEnqueuer on the API's own pool, which only exists once connected."""
        return await PostgresJobQueue(self._require_pool()).enqueue(
            kind, payload, idempotency_key=idempotency_key, run_at=run_at, max_attempts=max_attempts
        )

    async def selected_profile(self, account_id: str) -> SelectedProfile | None:
        # One statement: the summary is joined through the account's own google_sub and
        # its own selected profile, so no identifier from outside takes part in the lookup.
        row = await self._require_pool().fetchrow(
            """
            SELECT a.google_sub, l.gbp_location, l.title, l.place_id,
                   p.facts::text AS facts,
                   p.status, p.status_at, p.synced_at, p.review_count, p.average_rating,
                   p.unanswered_count, p.unanswered_low_count, p.last_review_at,
                   p.has_regular_hours, p.upcoming_special_hours, p.google_changed,
                   p.google_changed_fields, p.reviews_synced_at
            FROM account a
            JOIN location l
              ON l.id = a.selected_location_id AND l.account_id = a.id AND l.removed_at IS NULL
            LEFT JOIN profile_summary p
              ON p.google_sub = a.google_sub AND p.location = l.gbp_location
            WHERE a.id = $1::uuid
            """,
            account_id,
        )
        if row is None:
            return None
        summary = None
        if row["status"] is not None:
            rating = row["average_rating"]
            summary = StoredSummary(
                status=row["status"],
                status_at=row["status_at"],
                synced_at=row["synced_at"],
                review_count=row["review_count"],
                average_rating=float(rating) if rating is not None else None,
                unanswered_count=row["unanswered_count"],
                unanswered_low_count=row["unanswered_low_count"],
                last_review_at=row["last_review_at"],
                has_regular_hours=row["has_regular_hours"],
                upcoming_special_hours=row["upcoming_special_hours"],
                google_changed=row["google_changed"],
                google_changed_fields=tuple(row["google_changed_fields"] or ()),
                facts=_facts(row["facts"]),
                reviews_synced_at=row["reviews_synced_at"],
            )
        return SelectedProfile(
            row["google_sub"], row["gbp_location"], summary, row["title"], row["place_id"]
        )

    # --- reviews and replies ------------------------------------------------------

    async def list_reviews(
        self, account_id: str, *, unanswered_only: bool, limit: int
    ) -> ReviewPage:
        rows = await self._require_pool().fetch(
            f"""
            SELECT r.id, r.rating, r.comment, r.created_at, r.reply_text, r.reply_at,
                   count(*) OVER () AS total
            {_OWN_REVIEWS}
            WHERE a.id = $1::uuid AND (NOT $2::boolean OR r.reply_text IS NULL)
            ORDER BY (r.reply_text IS NOT NULL), r.rating ASC NULLS LAST,
                     r.created_at DESC NULLS LAST, r.id
            LIMIT $3
            """,  # noqa: S608
            account_id,
            unanswered_only,
            limit,
        )
        # limit is at least 1 (the route checks it): no rows means nothing matches.
        return ReviewPage(tuple(_review(r) for r in rows), rows[0]["total"] if rows else 0)

    async def get_review(self, account_id: str, review_id: str) -> StoredReview | None:
        row = await self._require_pool().fetchrow(
            f"""
            SELECT r.id, r.rating, r.comment, r.created_at, r.reply_text, r.reply_at,
                   d.draft, d.source_hash
            {_OWN_REVIEWS}
            LEFT JOIN review_draft d ON d.review_id = r.id
            WHERE a.id = $1::uuid AND r.id = $2
            """,  # noqa: S608
            account_id,
            review_id,
        )
        if row is None:
            return None
        draft = ReviewDraft(row["draft"], row["source_hash"]) if row["draft"] is not None else None
        return _review(row, draft)

    async def save_draft(
        self, account_id: str, review_id: str, source_hash: str, text: str, now: datetime
    ) -> None:
        # The ownership check is part of the statement, not a previous read.
        await self._require_pool().execute(
            f"""
            INSERT INTO review_draft (review_id, source_hash, draft, created_at)
            SELECT r.id, $3, $4, $5
            {_OWN_REVIEWS}
            WHERE a.id = $1::uuid AND r.id = $2
            ON CONFLICT (review_id) DO UPDATE SET
                source_hash = EXCLUDED.source_hash,
                draft = EXCLUDED.draft,
                created_at = EXCLUDED.created_at
            """,  # noqa: S608
            account_id,
            review_id,
            source_hash,
            text,
            now,
        )

    async def create_reply_action(
        self, account_id: str, review_id: str, text: str, now: datetime
    ) -> str | None:
        async with self._require_pool().acquire() as conn, conn.transaction():
            owned = await conn.fetchval(
                f"SELECT 1 {_OWN_REVIEWS} WHERE a.id = $1::uuid AND r.id = $2",  # noqa: S608
                account_id,
                review_id,
            )
            if owned is None:
                return None
            # The account, the profile and the review of the action come from the same
            # join as every read: the ownership check is part of the insert itself.
            # The unique index on live actions makes a second confirmation a no-op.
            action_id = await conn.fetchval(
                f"""
                INSERT INTO action
                    (account_id, type, google_sub, location, review_id, text, confirmed_at)
                SELECT a.id, $5, a.google_sub, l.gbp_location, r.id, $3, $4
                {_OWN_REVIEWS}
                WHERE a.id = $1::uuid AND r.id = $2
                ON CONFLICT DO NOTHING
                RETURNING id
                """,  # noqa: S608
                account_id,
                review_id,
                text,
                now,
                REPLY_REVIEW,
            )
            if action_id is None:
                raise ReplyInProgress
            await conn.execute(
                """
                INSERT INTO audit_log
                    (event, account_id, action_id, action_type, location, review_id,
                     gbp_review_id, text, confirmed_at)
                SELECT $2, a.account_id, a.id, a.type, a.location, a.review_id,
                       r.gbp_review_id, a.text, a.confirmed_at
                FROM action a JOIN review r ON r.id = a.review_id
                WHERE a.id = $1
                """,
                action_id,
                EVENT_CONFIRMED,
            )
            # The job carries the id of the action and nothing else: what is sent to
            # Google is read from the action, which nobody can change.
            await conn.execute(
                """
                INSERT INTO job (kind, payload, idempotency_key, max_attempts)
                VALUES ($1, $2::jsonb, $3, $4)
                """,
                self._reply_job_kind,
                json.dumps({"action_id": action_id}),
                f"{self._reply_job_kind}:{action_id}",
                REPLY_MAX_ATTEMPTS,
            )
        return action_id

    async def get_action(self, account_id: str, action_id: str) -> StoredAction | None:
        row = await self._require_pool().fetchrow(
            """
            SELECT id, type, review_id, state, error_code, confirmed_at, finished_at
            FROM action WHERE id = $2 AND account_id = $1::uuid
            """,
            account_id,
            action_id,
        )
        if row is None:
            return None
        return StoredAction(
            row["id"],
            row["type"],
            row["review_id"],
            row["state"],
            row["error_code"],
            row["confirmed_at"],
            row["finished_at"],
        )


def _review(row: asyncpg.Record, draft: ReviewDraft | None = None) -> StoredReview:
    return StoredReview(
        row["id"],
        row["rating"],
        row["comment"],
        row["created_at"],
        row["reply_text"],
        row["reply_at"],
        draft,
    )


def _facts(raw: str | None) -> ProfileFacts | None:
    if raw is None:
        return None
    try:
        return facts_from_json(json.loads(raw))
    except ValueError:
        return None
