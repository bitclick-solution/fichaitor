"""PostgreSQL store. Explicit SQL, no ORM.

The API role can only INSERT contacts, events and Google connections. On accounts,
profiles and sessions it can read, insert and update a few columns; it never deletes.
A leave is scheduled and restored here; the worker makes it effective and deletes.
Every statement on profiles is filtered by account_id: the isolation between accounts.
Of the worker's tables it can only INSERT jobs and SELECT profile summaries
(figures and facts), reviews, and the sentiment analysis with its monthly history
(never the classification of each review). It keeps the drafts of replies. It creates actions,
which it can read but never change, and it can only add to the audit log.
"""

import json
import secrets
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime

import asyncpg

from app.adapters.conversation_store.postgres import (
    PROPOSAL_COLUMNS,
    insert_link_code,
    proposal_from_row,
)
from app.adapters.job_queue.postgres import PostgresJobQueue
from app.domain.actions import (
    EVENT_CONFIRMED,
    EVENT_HOURS_CONFIRMED,
    REPLY_REVIEW,
    SET_SPECIAL_HOURS,
)
from app.domain.conversation import ChannelIdentity
from app.domain.jobs import DEFAULT_MAX_ATTEMPTS
from app.domain.key_dates import (
    ANSWERABLE_STATES,
    OUTCOME_DISMISSED,
    REFUSED_PROFILE,
    PanelRefused,
    panel_confirm_path,
    panel_dismiss_path,
    panel_event_id,
)
from app.domain.link_codes import LinkCode
from app.domain.offboarding import (
    APPLY_KIND,
    APPLY_MAX_ATTEMPTS,
    CODE_GONE,
    CODE_LOCKED,
    CODE_OK,
    CODE_WRONG,
    FLOOR_HOURS,
    MAIL_MAX_ATTEMPTS,
    SCOPE_ACCOUNT,
    SEND_MAIL_KIND,
    CodeCheck,
    PendingDeletion,
    same_hash,
)
from app.domain.plans import (
    PAID_PLANS,
    Entitlement,
    Plan,
    activation_allowed,
    parse_plan,
)
from app.domain.profile_summary import ProfileFacts, facts_from_json
from app.domain.proposals import (
    CANCELLED_BY_DEACTIVATION,
    CANCELLED_BY_LEAVE,
    OUTCOME_LOCATION_INACTIVE,
    OUTCOME_OFFBOARDING,
    ProposalState,
    ProposalType,
    advance,
)
from app.domain.quota import window
from app.domain.sentiment import Analysis, Figures, TopicCount, first_history_month
from app.domain.special_hours import JOB_MAX_ATTEMPTS as HOURS_MAX_ATTEMPTS
from app.domain.special_hours import SpecialHoursChange, describe
from app.ports.channel_links import LinkCodeTaken
from app.ports.google_oauth import ProfileLocation
from app.ports.key_dates import KeyDateNotFound, KeyDateView, SpecialHoursInProgress
from app.ports.quota import QuotaUnavailable
from app.ports.store import (
    Account,
    ActiveProfile,
    FreeTaken,
    GoogleConnection,
    Location,
    LocationInactive,
    LocationLimit,
    LocationNotFound,
    LocationOverview,
    LocationRemoved,
    LocationsOverview,
    NoLocationChosen,
    OffboardingPending,
    ReplyInProgress,
    ReviewDraft,
    ReviewPage,
    SelectedProfile,
    StoredAction,
    StoredReview,
    StoredSentiment,
    StoredSummary,
    WaitlistSignup,
    lost_access_summary,
)

REPLY_MAX_ATTEMPTS = 3  # 30 s and 60 s of waiting: the owner is looking at the screen

# A leave closes the access to what was obtained from Google at once, long before
# anything is deleted: while it is scheduled (it may be restored) and, once effective,
# until the worker has deleted the data. Looked up by the Google identity, so that an
# account created later for the same identity does not inherit what is left.
_NO_OPEN_LEAVE = """
    NOT EXISTS (SELECT 1 FROM offboarding o
                WHERE o.google_sub = a.google_sub
                  AND o.restored_at IS NULL AND o.purged_at IS NULL)
"""
_NO_PENDING_LEAVE = """
    NOT EXISTS (SELECT 1 FROM offboarding o
                WHERE o.account_id = a.id
                  AND o.restored_at IS NULL AND o.effective_at IS NULL)
"""


def _active(location: str) -> str:
    """One active profile of an account, unless the account has a scheduled leave
    (docs/multinegocio.md, 5.1). `location` is the SQL of the id of the profile: a
    parameter, or that parameter falling back to the preference; never input. The id is
    matched in the same statement as the account of the session, so a profile of
    another account and one that does not exist are the same nothing."""
    return f"""
    FROM account a
    JOIN location l
      ON l.id = {location} AND l.account_id = a.id AND l.active_since IS NOT NULL
     AND a.deleted_at IS NULL
     AND {_NO_PENDING_LEAVE}
"""


# The profile a request names ($2), or the preference of the panel when it names none.
_CHOSEN = "coalesce($2::text, a.selected_location_id)"

# Every active profile of an account, unless it has a scheduled leave.
_ANY_ACTIVE = f"""
    FROM account a
    JOIN location l ON l.account_id = a.id AND l.active_since IS NOT NULL
     AND a.deleted_at IS NULL
     AND {_NO_PENDING_LEAVE}
"""

# The reviews an account may see: those of its own active profiles, reached through
# its own Google identity. $1 is always the account of the session; a review id from
# the request only picks among them.
_OWN_REVIEWS = f"""
    {_ANY_ACTIVE}
    JOIN review r
      ON r.google_sub = a.google_sub AND r.location = l.gbp_location AND r.removed_at IS NULL
     AND {_NO_OPEN_LEAVE}
"""

# What location_active_guard raises (db/schema.sql), as the domain names it.
_ACTIVATION_REFUSALS: dict[str, type[Exception]] = {
    "limite_fichas": LocationLimit,
    "gratis_ocupada": FreeTaken,
    "baja_programada": OffboardingPending,
    "ficha_retirada": LocationRemoved,
    "cuenta_de_baja": LocationNotFound,
}
_FREE_ONE_ACCOUNT = "location_gratis_one_account"  # unique index: the last barrier


def _activation_error(exc: asyncpg.PostgresError) -> Exception | None:
    """The domain error behind a refusal of the database to activate, or None."""
    if isinstance(exc, asyncpg.UniqueViolationError):
        return FreeTaken() if exc.constraint_name == _FREE_ONE_ACCOUNT else None
    if isinstance(exc, asyncpg.CheckViolationError):
        message = getattr(exc, "message", None) or (str(exc.args[0]) if exc.args else "")
        kind = _ACTIVATION_REFUSALS.get(message)
        return kind() if kind is not None else None
    return None


class PostgresStore:
    def __init__(
        self,
        dsn: str,
        reply_job_kind: str = REPLY_REVIEW,
        mail_job_kind: str = SEND_MAIL_KIND,
        apply_job_kind: str = APPLY_KIND,
        hours_job_kind: str = SET_SPECIAL_HOURS,
    ) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        # Kinds of the jobs that carry out a confirmed reply, send an email and make a
        # leave effective. Tests use their own, so that no worker running on the same
        # database takes their jobs.
        self._reply_job_kind = reply_job_kind
        self._mail_job_kind = mail_job_kind
        self._apply_job_kind = apply_job_kind
        self._hours_job_kind = hours_job_kind

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("PostgresStore sin conectar")
        return self._pool

    # --- caps on paid calls (app/ports/quota.py) ---------------------------------
    # The API can count and read the count, nothing else: it cannot delete a counter,
    # move it to another window or set it back. The worker deletes past windows.

    async def take_quota(self, scope: str, limit: int, window_seconds: int, now: datetime) -> bool:
        if limit <= 0:
            return False
        start, end = window(now, window_seconds)
        try:
            # One statement: the first use creates the counter, the next ones add one
            # while below the limit. Two processes at once never go over it.
            hits = await self._require_pool().fetchval(
                """
                INSERT INTO usage_counter AS u (scope, window_start, expires_at, hits)
                VALUES ($1, $2, $3, 1)
                ON CONFLICT (scope, window_start) DO UPDATE SET hits = u.hits + 1
                WHERE u.hits < $4
                RETURNING hits
                """,
                scope,
                start,
                end,
                limit,
            )
        except Exception as exc:
            raise QuotaUnavailable(type(exc).__name__) from None
        return hits is not None

    async def quota_left(self, scope: str, limit: int, window_seconds: int, now: datetime) -> bool:
        if limit <= 0:
            return False
        try:
            hits = await self._require_pool().fetchval(
                "SELECT hits FROM usage_counter WHERE scope = $1 AND window_start = $2",
                scope,
                window(now, window_seconds)[0],
            )
        except Exception as exc:
            raise QuotaUnavailable(type(exc).__name__) from None
        return (hits or 0) < limit

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
            ON CONFLICT (google_sub) DO UPDATE
                SET email = EXCLUDED.email, disconnected_at = NULL
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
            SELECT a.id::text AS id, a.email, a.selected_location_id, s.id::text AS session_id,
                   o.scope, o.delete_at, a.disconnected_at IS NULL AS connected
            FROM session s JOIN account a ON a.id = s.account_id
            LEFT JOIN offboarding o
              ON o.account_id = a.id AND o.restored_at IS NULL AND o.effective_at IS NULL
            WHERE s.token_hash = $1 AND s.revoked_at IS NULL AND s.expires_at > $2
              AND a.deleted_at IS NULL
            """,
            token_hash,
            now,
        )
        if row is None:
            return None
        pending = PendingDeletion(row["scope"], row["delete_at"]) if row["scope"] else None
        return Account(
            row["id"],
            row["email"],
            row["selected_location_id"],
            row["session_id"],
            pending,
            row["connected"],
        )

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
        async with self._require_pool().acquire() as conn, conn.transaction():
            row = await self._own_location(conn, account_id, location_id)
            if row is None or row["removed"]:
                return False
            # Until step 9 (docs/multinegocio.md, 5.5): choosing an inactive profile
            # activates it, as the web that only knows "choose" expects.
            if row["plan"] is None:
                await self._activate(conn, account_id, location_id, None, row)
            # The ownership check is part of the statement, not a previous read.
            status = await conn.execute(
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

    # --- profiles of the account, active or not (docs/multinegocio.md) -------------

    @staticmethod
    async def _own_location(
        conn: asyncpg.Connection, account_id: str, location_id: str
    ) -> asyncpg.Record | None:
        return await conn.fetchrow(
            """
            SELECT l.plan, l.removed_at IS NOT NULL AS removed, now() AS now
            FROM location l JOIN account a ON a.id = l.account_id AND a.deleted_at IS NULL
            WHERE l.id = $2 AND l.account_id = $1::uuid
            """,
            account_id,
            location_id,
        )

    @staticmethod
    async def _default_plans(
        conn: asyncpg.Connection, account_id: str, location_id: str, now: datetime
    ) -> list[Plan]:
        """The plans an activation without a plan tries, in order: those with room in
        the account now, leaving that profile out (app/domain/plans.py)."""
        counts = await conn.fetch(
            """
            SELECT plan, count(*) AS n FROM location
            WHERE account_id = $1::uuid AND id <> $2 AND active_since IS NOT NULL
            GROUP BY plan
            """,
            account_id,
            location_id,
        )
        granted = await _entitlements(conn, account_id)
        active = {Plan(r["plan"]): r["n"] for r in counts}
        return [
            plan
            for plan in (Plan.GRATIS, *PAID_PLANS)
            if activation_allowed(active, granted, plan, now)
        ]

    async def _activate(
        self,
        conn: asyncpg.Connection,
        account_id: str,
        location_id: str,
        plan: Plan | None,
        row: asyncpg.Record,
    ) -> Plan:
        """Inside a transaction, with the row of _own_location. The database decides;
        each try runs in a savepoint, so that a refused one can be followed by the next."""
        current = parse_plan(row["plan"])
        if current is not None and plan in (None, current):
            return current
        if plan is not None:
            candidates = [plan]
        else:
            candidates = await self._default_plans(conn, account_id, location_id, row["now"])
        if not candidates:
            raise LocationLimit
        for index, candidate in enumerate(candidates):
            try:
                async with conn.transaction():
                    # A change of plan keeps active_since: the oldest profile is the one
                    # that pays in full (app/domain/plans.py).
                    await conn.execute(
                        """
                        UPDATE location
                        SET active_since = coalesce(active_since, now()), plan = $3
                        WHERE id = $2 AND account_id = $1::uuid
                        """,
                        account_id,
                        location_id,
                        candidate.value,
                    )
            except asyncpg.PostgresError as exc:
                error = _activation_error(exc)
                if error is None:
                    raise
                # Without a plan, a free profile taken by another account (or a slot
                # taken a moment ago) moves on to the next plan with room.
                last = index + 1 == len(candidates)
                if last or not isinstance(error, (FreeTaken, LocationLimit)):
                    raise error from None
                continue
            return candidate
        raise LocationLimit  # not reached: the last candidate returns or raises

    async def activate(self, account_id: str, location_id: str, plan: Plan | None = None) -> Plan:
        async with self._require_pool().acquire() as conn, conn.transaction():
            row = await self._own_location(conn, account_id, location_id)
            if row is None:
                raise LocationNotFound
            if row["removed"]:
                raise LocationRemoved
            return await self._activate(conn, account_id, location_id, plan, row)

    async def deactivate(self, account_id: str, location_id: str) -> bool:
        async with self._require_pool().acquire() as conn, conn.transaction():
            # The thread first, as the conversation and the key dates of the panel do:
            # nothing in the chat moves a proposal of this profile meanwhile.
            await conn.execute(
                "SELECT 1 FROM conversation WHERE account_id = $1::uuid FOR UPDATE", account_id
            )
            # The trigger empties plan, sets deactivated_at, writes the log and moves the
            # preference to the oldest active profile if it was this one.
            found = await conn.fetchval(
                """
                UPDATE location SET active_since = NULL, plan = NULL
                WHERE id = $2 AND account_id = $1::uuid AND removed_at IS NULL
                RETURNING id
                """,
                account_id,
                location_id,
            )
            if found is None:
                return False
            # A key date of a proposal written before it had a column of its own names
            # its profile in its payload.
            mine = """
                coalesce(location_id,
                         CASE WHEN type = 'key_date' THEN payload->>'location_id' END) = $2
            """
            await conn.execute(
                f"""
                UPDATE proposal
                SET state = $3, outcome_reason = $4, message_refs = '{{}}'::jsonb,
                    step_token = encode(gen_random_bytes(3), 'hex')
                WHERE account_id = $1::uuid AND state = ANY($5::text[]) AND {mine}
                """,  # noqa: S608
                account_id,
                location_id,
                ProposalState.CANCELLED.value,
                OUTCOME_LOCATION_INACTIVE,
                [state.value for state in CANCELLED_BY_DEACTIVATION],
            )
            await conn.execute(
                """
                UPDATE conversation_lane SET active_proposal_id = NULL, updated_at = now()
                WHERE account_id = $1::uuid AND location_id = $2
                  AND active_proposal_id IS NOT NULL
                """,
                account_id,
                location_id,
            )
            await conn.execute(
                f"""
                UPDATE conversation SET active_proposal_id = NULL, updated_at = now()
                WHERE account_id = $1::uuid AND active_proposal_id IN (
                    SELECT id FROM proposal WHERE account_id = $1::uuid AND {mine})
                """,  # noqa: S608
                account_id,
                location_id,
            )
        return True

    async def resolve_location(self, account_id: str, location_id: str | None) -> str:
        row = await self._require_pool().fetchrow(
            f"""
            SELECT l.id, l.active_since IS NOT NULL AS active,
                   l.removed_at IS NOT NULL AS removed,
                   l.id = a.selected_location_id AS preferred
            FROM account a
            JOIN location l ON l.id = {_CHOSEN} AND l.account_id = a.id
            WHERE a.id = $1::uuid AND a.deleted_at IS NULL
            """,  # noqa: S608
            account_id,
            location_id,
        )
        if row is None:
            raise NoLocationChosen if location_id is None else LocationNotFound
        if row["removed"]:
            # The preference keeps a profile retired for lack of access, to say so.
            if row["preferred"]:
                return row["id"]
            raise LocationNotFound
        if not row["active"]:
            raise LocationInactive
        return row["id"]

    async def locations_overview(self, account_id: str, today: date) -> LocationsOverview:
        pool = self._require_pool()
        rows = await pool.fetch(
            f"""
            SELECT l.id, l.title, l.place_id, l.verified, l.plan, l.active_since,
                   p.status, p.unanswered_count,
                   (SELECT count(*) FROM proposal pr
                    WHERE pr.account_id = a.id AND pr.type = 'key_date'
                      AND coalesce(pr.location_id, pr.payload->>'location_id') = l.id
                      AND pr.state = ANY($2::text[]) AND pr.payload->>'date' >= $3
                   ) AS key_dates
            FROM account a
            JOIN location l ON l.account_id = a.id AND l.removed_at IS NULL
            LEFT JOIN profile_summary p
              ON l.active_since IS NOT NULL
             AND p.google_sub = a.google_sub AND p.location = l.gbp_location
             AND {_NO_OPEN_LEAVE}
            WHERE a.id = $1::uuid AND a.deleted_at IS NULL
            ORDER BY l.title COLLATE "C", l.id
            """,  # noqa: S608
            account_id,
            [state.value for state in ANSWERABLE_STATES],
            today.isoformat(),
        )
        async with pool.acquire() as conn:
            granted = await _entitlements(conn, account_id)
        locations = []
        for r in rows:
            plan = parse_plan(r["plan"])
            active = plan is not None
            locations.append(
                LocationOverview(
                    id=r["id"],
                    title=r["title"],
                    place_id=r["place_id"],
                    verified=r["verified"],
                    plan=plan,
                    active_since=r["active_since"],
                    summary_status=r["status"] if active else None,
                    pending_reviews=(r["unanswered_count"] or 0) if active else None,
                    pending_key_dates=r["key_dates"] if active else None,
                )
            )
        return LocationsOverview(tuple(locations), tuple(granted))

    async def active_profiles(self, account_id: str) -> list[ActiveProfile]:
        rows = await self._require_pool().fetch(
            f"""
            SELECT l.id, a.google_sub, l.gbp_location
            {_ANY_ACTIVE}
            WHERE a.id = $1::uuid
            ORDER BY l.active_since, l.id
            """,  # noqa: S608
            account_id,
        )
        return [ActiveProfile(r["id"], r["google_sub"], r["gbp_location"]) for r in rows]

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
        return await self.profile(account_id)

    async def profile(
        self, account_id: str, location_id: str | None = None
    ) -> SelectedProfile | None:
        # One statement: the summary is joined through the account's own google_sub and
        # its own active profile, so an identifier from outside only picks among them.
        row = await self._require_pool().fetchrow(
            f"""
            SELECT l.id, a.google_sub, l.gbp_location, l.title, l.place_id,
                   p.facts::text AS facts,
                   p.status, p.status_at, p.synced_at, p.review_count, p.average_rating,
                   p.unanswered_count, p.unanswered_low_count, p.last_review_at,
                   p.has_regular_hours, p.upcoming_special_hours, p.google_changed,
                   p.google_changed_fields, p.reviews_synced_at
            {_active(_CHOSEN)}
            LEFT JOIN profile_summary p
              ON p.google_sub = a.google_sub AND p.location = l.gbp_location
             AND {_NO_OPEN_LEAVE}
            WHERE a.id = $1::uuid
            """,  # noqa: S608
            account_id,
            location_id,
        )
        if row is None:
            return await self._lost_profile(account_id, location_id)
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
            row["google_sub"],
            row["gbp_location"],
            summary,
            row["title"],
            row["place_id"],
            location_id=row["id"],
        )

    async def _lost_profile(
        self, account_id: str, location_id: str | None
    ) -> SelectedProfile | None:
        """The preferred profile when the worker retired it for lack of access. Only its
        Google name, for the account's own Google identity: nothing read from Google."""
        row = await self._require_pool().fetchrow(
            f"""
            SELECT l.id, a.google_sub, l.gbp_location, l.removed_at
            FROM account a
            JOIN location l ON l.id = a.selected_location_id AND l.account_id = a.id
            WHERE a.id = $1::uuid AND a.deleted_at IS NULL AND l.removed_at IS NOT NULL
              AND l.id = {_CHOSEN}
              AND {_NO_PENDING_LEAVE}
            """,  # noqa: S608
            account_id,
            location_id,
        )
        if row is None:
            return None
        return SelectedProfile(
            row["google_sub"],
            row["gbp_location"],
            lost_access_summary(row["removed_at"]),
            access_lost=True,
            location_id=row["id"],
        )

    # --- reviews and replies ------------------------------------------------------

    async def list_reviews(
        self,
        account_id: str,
        *,
        unanswered_only: bool,
        limit: int,
        location_id: str | None = None,
    ) -> ReviewPage:
        rows = await self._require_pool().fetch(
            f"""
            SELECT r.id, r.rating, r.comment, r.created_at, r.reply_text, r.reply_at,
                   count(*) OVER () AS total
            {_OWN_REVIEWS}
            WHERE a.id = $1::uuid AND l.id = {_CHOSEN}
              AND (NOT $3::boolean OR r.reply_text IS NULL)
            ORDER BY (r.reply_text IS NOT NULL), r.rating ASC NULLS LAST,
                     r.created_at DESC NULLS LAST, r.id
            LIMIT $4
            """,  # noqa: S608
            account_id,
            location_id,
            unanswered_only,
            limit,
        )
        # limit is at least 1 (the route checks it): no rows means nothing matches.
        return ReviewPage(tuple(_review(r) for r in rows), rows[0]["total"] if rows else 0)

    async def get_review(self, account_id: str, review_id: str) -> StoredReview | None:
        row = await self._require_pool().fetchrow(
            f"""
            SELECT r.id, r.rating, r.comment, r.created_at, r.reply_text, r.reply_at,
                   d.draft, d.source_hash, l.id AS location_id
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
        return replace(_review(row, draft), location_id=row["location_id"])

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
            SELECT id, type, review_id, state, error_code, confirmed_at, finished_at,
                   special_date
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
            row["special_date"],
        )

    # --- key dates: the hours of one day (app/ports/key_dates.py) -----------------

    async def _insert_hours_action(
        self,
        conn: asyncpg.Connection,
        account_id: str,
        location_id: str,
        change: SpecialHoursChange,
        now: datetime,
    ) -> str | None:
        """The action, its audit row and its job, on a connection inside a transaction.
        The account, its Google identity and the profile come from the same join as every
        read: only an active profile of that account, without a leave."""
        action_id = await conn.fetchval(
            f"""
            INSERT INTO action
                (account_id, type, google_sub, location, special_date, text, confirmed_at)
            SELECT a.id, $3, a.google_sub, l.gbp_location, $4, $5, $6
            {_active("$2")}
            WHERE a.id = $1::uuid AND {_NO_OPEN_LEAVE}
            ON CONFLICT DO NOTHING
            RETURNING id
            """,  # noqa: S608
            account_id,
            location_id,
            SET_SPECIAL_HOURS,
            change.day,
            change.to_text(),
            now,
        )
        if action_id is None:
            owned = await conn.fetchval(
                f"SELECT 1 {_active('$2')} WHERE a.id = $1::uuid AND {_NO_OPEN_LEAVE}",  # noqa: S608
                account_id,
                location_id,
            )
            if owned is None:
                return None
            raise SpecialHoursInProgress
        await conn.execute(
            """
            INSERT INTO audit_log
                (event, account_id, action_id, action_type, location, review_id,
                 gbp_review_id, text, confirmed_at)
            SELECT $2, a.account_id, a.id, a.type, a.location, '', '', a.text, a.confirmed_at
            FROM action a WHERE a.id = $1
            """,
            action_id,
            EVENT_HOURS_CONFIRMED,
        )
        # The job carries the id of the action and nothing else, as for a reply.
        await conn.execute(
            """
            INSERT INTO job (kind, payload, idempotency_key, max_attempts)
            VALUES ($1, $2::jsonb, $3, $4)
            """,
            self._hours_job_kind,
            json.dumps({"action_id": action_id}),
            f"{self._hours_job_kind}:{action_id}",
            HOURS_MAX_ATTEMPTS,
        )
        return action_id

    async def create_special_hours_action(
        self, account_id: str, location_id: str, change: SpecialHoursChange, now: datetime
    ) -> str | None:
        async with self._require_pool().acquire() as conn, conn.transaction():
            return await self._insert_hours_action(conn, account_id, location_id, change, now)

    @staticmethod
    def _key_dates_sql(condition: str, profiles: str) -> str:
        """Key date proposals of account $1 whose profile is one of `profiles` (a FROM
        of account a and location l, matched with $1), with their action. `condition`
        narrows the proposals (pr) further. Neither is ever built from input. A proposal
        written before it had a column of its own names its profile in its payload."""
        return f"""
            SELECT p.*, x.id AS x_id, x.type AS x_type, x.state AS x_state,
                   x.error_code AS x_error, x.confirmed_at AS x_confirmed_at,
                   x.finished_at AS x_finished_at, x.special_date AS x_special_date
            FROM (
                SELECT {PROPOSAL_COLUMNS} FROM proposal pr
                WHERE pr.account_id = $1::uuid AND pr.type = 'key_date'
                  AND coalesce(pr.location_id, pr.payload->>'location_id') IN (
                      SELECT l.id {profiles} WHERE a.id = $1::uuid)
                  AND {condition}
            ) p
            LEFT JOIN action x ON x.id = p.action_id AND x.account_id = $1::uuid
            ORDER BY p.payload::jsonb->>'date', p.id
        """  # noqa: S608

    async def list_key_dates(
        self, account_id: str, today: date, location_id: str | None = None
    ) -> list[KeyDateView]:
        rows = await self._require_pool().fetch(
            self._key_dates_sql("pr.payload->>'date' >= $3", _active(_CHOSEN)),
            account_id,
            location_id,
            today.isoformat(),
        )
        return [_key_date(r) for r in rows]

    async def get_key_date(self, account_id: str, proposal_id: str) -> KeyDateView | None:
        row = await self._require_pool().fetchrow(
            self._key_dates_sql("pr.id = $2", _ANY_ACTIVE), account_id, proposal_id
        )
        return _key_date(row) if row is not None else None

    async def _locked_key_date(self, conn: asyncpg.Connection, account_id: str, proposal_id: str):
        # The thread of the conversation first, as the conversation itself does: a press
        # in the chat and an answer in the panel are handled one after the other.
        await conn.execute(
            "SELECT 1 FROM conversation WHERE account_id = $1::uuid FOR UPDATE", account_id
        )
        row = await conn.fetchrow(
            f"""
            SELECT {PROPOSAL_COLUMNS} FROM proposal
            WHERE id = $2 AND account_id = $1::uuid AND type = $3
            FOR UPDATE
            """,  # noqa: S608
            account_id,
            proposal_id,
            ProposalType.KEY_DATE.value,
        )
        if row is None:
            raise KeyDateNotFound
        return proposal_from_row(row)

    @staticmethod
    async def _release(conn: asyncpg.Connection, account_id: str, proposal_id: str) -> None:
        """The thread and the lane of its profile let that proposal go."""
        await conn.execute(
            """
            UPDATE conversation SET active_proposal_id = NULL, updated_at = now()
            WHERE account_id = $1::uuid AND active_proposal_id = $2
            """,
            account_id,
            proposal_id,
        )
        await conn.execute(
            """
            UPDATE conversation_lane SET active_proposal_id = NULL, updated_at = now()
            WHERE account_id = $1::uuid AND active_proposal_id = $2
            """,
            account_id,
            proposal_id,
        )

    async def answer_key_date(
        self,
        account_id: str,
        proposal_id: str,
        change: SpecialHoursChange,
        now: datetime,
        today: date,
    ) -> str:
        async with self._require_pool().acquire() as conn, conn.transaction():
            proposal = await self._locked_key_date(conn, account_id, proposal_id)
            if proposal.payload.get("date") != change.day.isoformat():
                raise KeyDateNotFound
            path = panel_confirm_path(proposal.state, change.day, today)
            action_id = await self._insert_hours_action(
                conn, account_id, str(proposal.payload.get("location_id", "")), change, now
            )
            if action_id is None:
                raise PanelRefused(REFUSED_PROFILE)
            confirmed = {"kind": change.kind, "periods": [list(p) for p in change.periods]}
            await conn.execute(
                """
                UPDATE proposal SET
                    state = $2, step_token = $3, message_refs = '{}'::jsonb,
                    slots = slots || $4::jsonb, confirmed_at = $5, confirmed_event_id = $6,
                    confirmed_summary = $7, action_id = $8, outcome_reason = NULL
                WHERE id = $1 AND account_id = $9::uuid AND type = 'key_date'
                """,
                proposal_id,
                advance(proposal.state, *path).value,
                secrets.token_hex(3),
                json.dumps({"confirmed": confirmed, "answered_in": "panel"}),
                now,
                panel_event_id(now, secrets.token_hex(4)),
                f"{change.day.isoformat()}: {describe(change)}",
                action_id,
                account_id,
            )
            await self._release(conn, account_id, proposal_id)
        return action_id

    async def dismiss_key_date(self, account_id: str, proposal_id: str, now: datetime) -> None:
        async with self._require_pool().acquire() as conn, conn.transaction():
            proposal = await self._locked_key_date(conn, account_id, proposal_id)
            path = panel_dismiss_path(proposal.state)
            await conn.execute(
                """
                UPDATE proposal SET state = $2, step_token = $3, message_refs = '{}'::jsonb,
                                    outcome_reason = $4
                WHERE id = $1 AND account_id = $5::uuid AND type = 'key_date'
                """,
                proposal_id,
                advance(proposal.state, *path).value,
                secrets.token_hex(3),
                OUTCOME_DISMISSED,
                account_id,
            )
            await self._release(conn, account_id, proposal_id)

    async def sentiment(
        self, account_id: str, now: datetime, location_id: str | None = None
    ) -> StoredSentiment | None:
        # Same join as the summary: the analysis is reached through the account's own
        # google_sub and its own active profile. An id from the request only picks one.
        pool = self._require_pool()
        row = await pool.fetchrow(
            f"""
            SELECT s.google_sub, s.location, s.status, s.generated_at, s.based_on,
                   s.with_text, s.provisional, s.period, s.score, s.overall, s.summary,
                   s.suggestion, s.summary_source, s.praised::text AS praised,
                   s.complaints::text AS complaints, s.trigger
            {_active(_CHOSEN)}
            JOIN sentiment_analysis s
              ON s.google_sub = a.google_sub AND s.location = l.gbp_location
             AND {_NO_OPEN_LEAVE}
            WHERE a.id = $1::uuid
            """,  # noqa: S608
            account_id,
            location_id,
        )
        if row is None:
            return None
        history = await pool.fetch(
            f"""
            SELECT h.month, h.score
            {_active(_CHOSEN)}
            JOIN sentiment_history h
              ON h.google_sub = a.google_sub AND h.location = l.gbp_location
             AND {_NO_OPEN_LEAVE}
            WHERE a.id = $1::uuid AND h.month >= $3
            ORDER BY h.month
            """,  # noqa: S608
            account_id,
            location_id,
            first_history_month(now),
        )
        figures = Figures(
            status=row["status"],
            based_on=row["based_on"],
            with_text=row["with_text"],
            provisional=row["provisional"],
            period=row["period"],
            score=row["score"],
            overall=row["overall"],
            praised=_topic_counts(row["praised"]),
            complaints=_topic_counts(row["complaints"]),
        )
        analysis = Analysis(
            figures,
            row["summary"],
            row["suggestion"],
            row["summary_source"],
            row["trigger"],
            row["generated_at"],
        )
        return StoredSentiment(analysis, tuple((h["month"], h["score"]) for h in history))

    # --- leaving: code, scheduled leave and restore -------------------------------

    async def create_offboarding_code(
        self,
        account_id: str,
        session_id: str,
        scope: str,
        code_hash: str,
        now: datetime,
        expires_at: datetime,
        mail: dict,
    ) -> None:
        async with self._require_pool().acquire() as conn, conn.transaction():
            # A new code voids every earlier one of the account.
            await conn.execute(
                """
                UPDATE offboarding_code SET invalidated_at = $2
                WHERE account_id = $1::uuid AND used_at IS NULL AND invalidated_at IS NULL
                """,
                account_id,
                now,
            )
            code_id = await conn.fetchval(
                """
                INSERT INTO offboarding_code
                    (account_id, session_id, scope, code_hash, created_at, expires_at)
                VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6)
                RETURNING id::text
                """,
                account_id,
                session_id,
                scope,
                code_hash,
                now,
                expires_at,
            )
            # The code travels in the job and nowhere else; the worker empties the
            # payload as soon as the email is out.
            await conn.execute(
                "INSERT INTO job (kind, payload, max_attempts) VALUES ($1, $2::jsonb, $3)",
                self._mail_job_kind,
                json.dumps({**mail, "code_id": code_id}),
                MAIL_MAX_ATTEMPTS,
            )

    async def offboarding_code_mail(self, account_id: str) -> str | None:
        return await self._require_pool().fetchval(
            """
            SELECT mail_state FROM offboarding_code
            WHERE account_id = $1::uuid AND used_at IS NULL AND invalidated_at IS NULL
            ORDER BY created_at DESC, id LIMIT 1
            """,
            account_id,
        )

    async def confirm_offboarding(
        self,
        account_id: str,
        candidate_hash: str,
        scope: str,
        now: datetime,
        *,
        max_attempts: int,
        delete_at: datetime,
        restore_hash: str,
        mail: dict,
    ) -> CodeCheck:
        async with self._require_pool().acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT id, code_hash, attempts, expires_at, used_at, invalidated_at
                FROM offboarding_code
                WHERE account_id = $1::uuid
                ORDER BY created_at DESC, id LIMIT 1
                FOR UPDATE
                """,
                account_id,
            )
            if (
                row is None
                or row["used_at"] is not None
                or row["invalidated_at"] is not None
                or row["expires_at"] <= now
            ):
                return CodeCheck(CODE_GONE)
            if row["attempts"] >= max_attempts:
                await conn.execute(
                    "UPDATE offboarding_code SET invalidated_at = $2 WHERE id = $1", row["id"], now
                )
                return CodeCheck(CODE_LOCKED)
            attempts = row["attempts"] + 1
            # A wrong code, a code for another scope and a code from another session
            # all end here: the fingerprint covers the three.
            if not same_hash(row["code_hash"], candidate_hash):
                locked = attempts >= max_attempts
                await conn.execute(
                    """
                    UPDATE offboarding_code
                    SET attempts = $2,
                        invalidated_at = CASE WHEN $3::boolean THEN $4::timestamptz END
                    WHERE id = $1
                    """,
                    row["id"],
                    attempts,
                    locked,
                    now,
                )
                return CodeCheck(CODE_LOCKED if locked else CODE_WRONG, max_attempts - attempts)
            await conn.execute(
                "UPDATE offboarding_code SET attempts = $2, used_at = $3 WHERE id = $1",
                row["id"],
                attempts,
                now,
            )
            # A leave already scheduled is replaced by this one: its link stops working.
            await conn.execute(
                """
                UPDATE offboarding SET restored_at = $2, restore_token_hash = NULL
                WHERE account_id = $1::uuid AND restored_at IS NULL AND effective_at IS NULL
                """,
                account_id,
                now,
            )
            # The database refuses a leave shorter than its floor (47 hours, M5); it is
            # asked for the configured grace, less the hour between the two clocks.
            await conn.execute(
                "SELECT set_config('fichaitor.offboarding_min_hours', $1, true)",
                str(max(FLOOR_HOURS, (delete_at - now).total_seconds() / 3600 - 1)),
            )
            offboarding_id = await conn.fetchval(
                """
                INSERT INTO offboarding
                    (account_id, scope, google_sub, confirmed_at, delete_at, restore_token_hash)
                SELECT a.id, $2, a.google_sub, $3, $4, $5 FROM account a
                WHERE a.id = $1::uuid AND a.deleted_at IS NULL
                RETURNING id::text
                """,
                account_id,
                scope,
                now,
                delete_at,
                restore_hash,
            )
            if offboarding_id is None:
                return CodeCheck(CODE_GONE)
            # What waited in the chat is not sent any more (B12): the proposals not yet
            # confirmed end here, and the thread stops showing one.
            await conn.execute(
                """
                UPDATE proposal SET state = $3, outcome_reason = $4, step_token = $5,
                                    message_refs = '{}'::jsonb
                WHERE account_id = $1::uuid AND state = ANY($2::text[])
                """,
                account_id,
                [state.value for state in CANCELLED_BY_LEAVE],
                ProposalState.CANCELLED.value,
                OUTCOME_OFFBOARDING,
                secrets.token_hex(3),
            )
            await conn.execute(
                """
                UPDATE conversation SET active_proposal_id = NULL, updated_at = now()
                WHERE account_id = $1::uuid AND active_proposal_id IS NOT NULL
                """,
                account_id,
            )
            if scope == SCOPE_ACCOUNT:
                await conn.execute(
                    """
                    UPDATE session SET revoked_at = $2
                    WHERE account_id = $1::uuid AND revoked_at IS NULL
                    """,
                    account_id,
                    now,
                )
                # The messaging channels too, at once: nothing more is sent to them and
                # what they send is not attended (docs/canales.md). Their rows go when
                # the leave becomes effective.
                await self._unlink_channels(conn, account_id, now)
            await conn.execute(
                "INSERT INTO job (kind, payload, max_attempts) VALUES ($1, $2::jsonb, $3)",
                self._mail_job_kind,
                json.dumps(mail),
                MAIL_MAX_ATTEMPTS,
            )
            # The job that makes it effective waits until the date. It decides again
            # when it runs: a restored leave is left alone.
            await conn.execute(
                """
                INSERT INTO job (kind, payload, run_at, idempotency_key, max_attempts)
                VALUES ($1, $2::jsonb, $3, $4, $5)
                """,
                self._apply_job_kind,
                json.dumps({"offboarding_id": offboarding_id}),
                delete_at,
                f"{self._apply_job_kind}:{offboarding_id}",
                APPLY_MAX_ATTEMPTS,
            )
        return CodeCheck(CODE_OK, delete_at=delete_at)

    async def restore_offboarding(self, account_id: str, now: datetime, *, mail: dict) -> bool:
        async with self._require_pool().acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE offboarding SET restored_at = $2, restore_token_hash = NULL
                WHERE account_id = $1::uuid AND restored_at IS NULL AND effective_at IS NULL
                RETURNING account_id::text, scope
                """,
                account_id,
                now,
            )
            if row is None:
                return False
            await self._enqueue_restored_mail(conn, row["account_id"], row["scope"], mail)
        return True

    async def restore_offboarding_by_token(
        self, token_hash: str, now: datetime, *, mail: dict
    ) -> str | None:
        # One transaction: the token is spent by the same write that restores and
        # enqueues the email. Any error rolls the three back and the link still works.
        async with self._require_pool().acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE offboarding SET restored_at = $2, restore_token_hash = NULL
                WHERE restore_token_hash = $1 AND restored_at IS NULL AND effective_at IS NULL
                  AND delete_at > $2
                RETURNING account_id::text, scope
                """,
                token_hash,
                now,
            )
            if row is None:
                return None
            await self._enqueue_restored_mail(conn, row["account_id"], row["scope"], mail)
        return row["account_id"]

    # --- messaging channels (app/ports/channel_links.py, docs/canales.md) -------------

    async def save_link_code(self, code: LinkCode) -> None:
        async with self._require_pool().acquire() as conn:
            try:
                await insert_link_code(conn, code)
            except asyncpg.UniqueViolationError:
                raise LinkCodeTaken from None

    async def list_channels(self, account_id: str) -> list[ChannelIdentity]:
        rows = await self._require_pool().fetch(
            """
            SELECT id, account_id::text AS account_id, channel, address, verified_at,
                   last_inbound_at
            FROM channel_identity
            WHERE account_id = $1::uuid AND unlinked_at IS NULL
            ORDER BY verified_at, id
            """,
            account_id,
        )
        return [
            ChannelIdentity(
                r["id"],
                r["account_id"],
                r["channel"],
                r["address"],
                r["verified_at"],
                r["last_inbound_at"],
            )
            for r in rows
        ]

    async def unlink_channel(self, account_id: str, identity_id: str, now: datetime) -> bool:
        status = await self._require_pool().execute(
            """
            UPDATE channel_identity SET unlinked_at = $3
            WHERE id = $2 AND account_id = $1::uuid AND unlinked_at IS NULL
            """,
            account_id,
            identity_id,
            now,
        )
        return status == "UPDATE 1"

    @staticmethod
    async def _unlink_channels(conn: asyncpg.Connection, account_id: str, now: datetime) -> None:
        await conn.execute(
            """
            UPDATE channel_identity SET unlinked_at = $2
            WHERE account_id = $1::uuid AND unlinked_at IS NULL
            """,
            account_id,
            now,
        )
        await conn.execute(
            "UPDATE link_code SET used_at = $2 WHERE account_id = $1::uuid AND used_at IS NULL",
            account_id,
            now,
        )

    async def _enqueue_restored_mail(
        self, conn: asyncpg.Connection, account_id: str, scope: str, mail: dict
    ) -> None:
        # The address is read now, in the transaction: the one the account has today
        # (signing in again may have changed it), and none if it was already emptied.
        to = await conn.fetchval(
            "SELECT email FROM account WHERE id = $1::uuid AND deleted_at IS NULL",
            account_id,
        )
        if not to:
            return
        await conn.execute(
            "INSERT INTO job (kind, payload, max_attempts) VALUES ($1, $2::jsonb, $3)",
            self._mail_job_kind,
            json.dumps({**mail, "to": to, "scope": scope}),
            MAIL_MAX_ATTEMPTS,
        )


def _topic_counts(raw: str | None) -> tuple[TopicCount, ...]:
    """Stored counts per topic. Anything unexpected is left out."""
    try:
        items = json.loads(raw) if raw else []
    except ValueError:
        return ()
    return tuple(
        TopicCount(item["topic"], item["mentions"])
        for item in (items if isinstance(items, list) else [])
        if isinstance(item, dict)
        and isinstance(item.get("topic"), str)
        and isinstance(item.get("mentions"), int)
    )


def _key_date(row: asyncpg.Record) -> KeyDateView:
    proposal = proposal_from_row(row)
    action = None
    if row["x_id"] is not None:
        action = StoredAction(
            row["x_id"],
            row["x_type"],
            None,
            row["x_state"],
            row["x_error"],
            row["x_confirmed_at"],
            row["x_finished_at"],
            row["x_special_date"],
        )
    payload = proposal.payload
    return KeyDateView(
        id=proposal.id,
        day=date.fromisoformat(payload["date"]),
        name=str(payload.get("name", "")),
        kind=str(payload.get("kind", "")),
        state=proposal.state,
        location_id=str(payload.get("location_id", "")),
        expires_at=proposal.expires_at,
        confirmed=dict(proposal.slots.get("confirmed") or {}),
        action=action,
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


async def _entitlements(conn: asyncpg.Connection, account_id: str) -> list[Entitlement]:
    rows = await conn.fetch(
        "SELECT plan, quantity, starts_at, ends_at FROM plan_entitlement"
        " WHERE account_id = $1::uuid ORDER BY starts_at, id",
        account_id,
    )
    return [Entitlement(Plan(r["plan"]), r["quantity"], r["starts_at"], r["ends_at"]) for r in rows]
