"""Database access of the backoffice: only the admin_* functions of db/schema_admin.sql
(SECURITY DEFINER, owned by the owner of the tables) and the insertion of its own app_*
audit rows. The role fichaitor_admin reads nothing else the screens need: who may do what
(section 6.3), the fresh TOTP of the sensitive actions and the audit of each read and each
action are enforced by those functions, in the same transaction.

Errors of the functions become AdminError with a stable code for the screens.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import asyncpg


class AdminError(Exception):
    """An answer for the screens: HTTP status, stable code and a message in Spanish that
    never carries personal data."""

    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message


# The errors that admin_actor raises (insufficient_privilege), by their message.
_ACTOR_ERRORS = {
    "sesion_no_valida": (401, "session_invalid", "La sesión no vale: vuelve a entrar."),
    "rol_no_permitido": (403, "role_not_allowed", "Tu rol no permite hacer esto."),
    "totp_requerido": (403, "totp_required", "Confirma con un código TOTP nuevo."),
}


def translate(exc: asyncpg.PostgresError) -> AdminError:
    """The answer for an error of a function. A RAISE of our functions carries a message
    written for the screens; a constraint violation, only its kind (its DETAIL can carry
    data and is never shown)."""
    message = getattr(exc, "message", "") or ""
    if isinstance(exc, asyncpg.InsufficientPrivilegeError):
        status, code, text = _ACTOR_ERRORS.get(
            message, (403, "forbidden", "No permitido desde el backoffice.")
        )
        return AdminError(status, code, text)
    if isinstance(exc, asyncpg.NoDataFoundError):
        return AdminError(404, "not_found", message)
    if isinstance(exc, asyncpg.IntegrityConstraintViolationError):
        if getattr(exc, "constraint_name", None):
            return AdminError(422, "invalid", "Los datos no cumplen las reglas de la base.")
        return AdminError(422, "invalid", message)
    if isinstance(exc, asyncpg.DataError):
        return AdminError(422, "invalid", "Datos no válidos.")
    return AdminError(500, "database_error", "Error de la base de datos.")


async def _init(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog", format="text"
    )


class AdminStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, url: str) -> "AdminStore":
        pool = await asyncpg.create_pool(url, min_size=1, max_size=5, init=_init)
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    @asynccontextmanager
    async def _errors(self) -> AsyncIterator[None]:
        try:
            yield
        except asyncpg.PostgresError as exc:
            raise translate(exc) from None

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        async with self._errors():
            rows = await self.pool.fetch(sql, *args)
        return [dict(r) for r in rows]

    async def fetchrow(self, sql: str, *args: Any) -> dict | None:
        async with self._errors():
            row = await self.pool.fetchrow(sql, *args)
        return dict(row) if row is not None else None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        async with self._errors():
            return await self.pool.fetchval(sql, *args)

    # --- authentication -------------------------------------------------------------------

    async def login_allowed(self) -> bool:
        return bool(await self.fetchval("SELECT admin_login_allowed()"))

    async def login_lookup(self, username: str) -> dict | None:
        return await self.fetchrow(
            "SELECT admin_user_id, password_hash, totp_secret_enc, locked_until"
            " FROM admin_login_lookup($1)",
            username,
        )

    async def login_failed(self, username: str, stage: str) -> None:
        await self.fetchval("SELECT admin_login_failed($1, $2)", username, stage)

    async def session_open(self, username: str, token_hash: str) -> bool:
        return bool(await self.fetchval("SELECT admin_session_open($1, $2)", username, token_hash))

    async def rehash_password(self, token_hash: str, password_hash: str) -> bool:
        return bool(
            await self.fetchval("SELECT admin_rehash_password($1, $2)", token_hash, password_hash)
        )

    async def pending_user(self, token_hash: str) -> dict | None:
        return await self.fetchrow(
            "SELECT username, totp_secret_enc FROM admin_session_pending_user($1)", token_hash
        )

    async def session_totp(self, token_hash: str, step: int) -> bool:
        return bool(await self.fetchval("SELECT admin_session_totp($1, $2)", token_hash, step))

    async def recovery_candidates(self, token_hash: str) -> list[dict]:
        return await self.fetch(
            "SELECT code_id, code_hash FROM admin_recovery_candidates($1)", token_hash
        )

    async def session_recovery(self, token_hash: str, code_id: int) -> bool:
        return bool(
            await self.fetchval("SELECT admin_session_recovery($1, $2)", token_hash, code_id)
        )

    async def session_check(self, token_hash: str) -> dict | None:
        return await self.fetchrow(
            "SELECT admin_user_id::text AS admin_user_id, username, role, totp_verified_at"
            " FROM admin_session_check($1)",
            token_hash,
        )

    async def session_must_enroll(self, token_hash: str) -> bool:
        return bool(await self.fetchval("SELECT admin_session_must_enroll($1)", token_hash))

    async def session_username(self, token_hash: str) -> str | None:
        return await self.fetchval("SELECT admin_session_username($1)", token_hash)

    async def session_close(self, token_hash: str) -> None:
        await self.fetchval("SELECT admin_session_close($1)", token_hash)

    async def set_totp(
        self, token_hash: str, sealed: str, step: int, recovery_hashes: list[str]
    ) -> bool:
        return bool(
            await self.fetchval(
                "SELECT admin_set_totp($1, $2, $3, $4)", token_hash, sealed, step, recovery_hashes
            )
        )

    async def change_password(self, token_hash: str, password_hash: str) -> None:
        await self.fetchval("SELECT admin_change_password($1, $2)", token_hash, password_hash)

    async def enrollment_check(self, token_hash: str) -> dict | None:
        return await self.fetchrow(
            "SELECT username, role FROM admin_enrollment_check($1)", token_hash
        )

    async def enroll(
        self,
        token_hash: str,
        password_hash: str,
        sealed: str,
        step: int,
        recovery_hashes: list[str],
    ) -> bool:
        return bool(
            await self.fetchval(
                "SELECT admin_enroll($1, $2, $3, $4, $5)",
                token_hash,
                password_hash,
                sealed,
                step,
                recovery_hashes,
            )
        )

    # --- audit of the process (app_* actions only: the base refuses any other) ------------

    async def audit(
        self,
        admin_user: str | None,
        action: str,
        *,
        account_id: str | None = None,
        target: str | None = None,
        detail: dict | None = None,
    ) -> None:
        await self.fetchval(
            "INSERT INTO admin_audit_log (admin_user, action, account_id, target, detail)"
            " VALUES ($1::uuid, $2, $3::uuid, $4, $5::jsonb)",
            admin_user,
            action,
            account_id,
            target,
            detail or {},
        )

    # --- screens ----------------------------------------------------------------------------

    # Listings: `limit` rows (the routes ask for one more than they show) after the key
    # `after` = (key, id) of the last row of the page before (app/admin/pagination.py).

    async def find_accounts(
        self, session: str, q: str, limit: int = 21, after: tuple[str, str] | None = None
    ) -> list[dict]:
        key, ident = after or (None, None)
        return await self.fetch(
            "SELECT account_id::text AS account_id, email_masked, created_at, deleted_at,"
            " disconnected_at, locations, active_locations"
            " FROM admin_find_accounts($1, $2, $3, $4, $5)",
            session,
            q,
            limit,
            key,
            ident,
        )

    async def account_overview(self, session: str, account: str) -> dict:
        return await self.fetchval("SELECT admin_account_overview($1, $2::uuid)", session, account)

    async def account_detail(
        self,
        session: str,
        account: str,
        ticket: str,
        limit: int = 51,
        after: tuple[str, str] | None = None,
    ) -> list[dict]:
        key, ident = after or (None, None)
        return await self.fetch(
            "SELECT location_id, title, active_since, plan, deactivated_at, removed_at, verified"
            " FROM admin_account_detail($1, $2::uuid, $3, $4, $5, $6)",
            session,
            account,
            ticket,
            limit,
            key,
            ident,
        )

    async def sync_status(
        self,
        session: str,
        account: str | None,
        limit: int = 51,
        after: tuple[str, str] | None = None,
    ) -> list[dict]:
        key, ident = after or (None, None)
        return await self.fetch(
            "SELECT account_id::text AS account_id, location_id, status, status_at, synced_at"
            " FROM admin_sync_status($1, $2::uuid, $3, $4, $5)",
            session,
            account,
            limit,
            key,
            ident,
        )

    async def channel_links(
        self, session: str, account: str, limit: int = 51, after: tuple[str, str] | None = None
    ) -> list[dict]:
        key, ident = after or (None, None)
        return await self.fetch(
            "SELECT identity_id, channel, verified_at, last_inbound_at, unlinked_at"
            " FROM admin_channel_links($1, $2::uuid, $3, $4, $5)",
            session,
            account,
            limit,
            key,
            ident,
        )

    async def list_page(
        self,
        session: str,
        name: str,
        account: str | None,
        limit: int,
        after: tuple[str, str] | None = None,
    ) -> dict:
        """A page of a listing of an account or of the review queue (admin_list):
        {"items": [...], "next": {"key", "id"} or None}."""
        key, ident = after or (None, None)
        return await self.fetchval(
            "SELECT admin_list($1, $2, $3::uuid, $4, $5, $6)",
            session,
            name,
            account,
            limit,
            key,
            ident,
        )

    async def account_billing(self, session: str, account: str) -> dict:
        return await self.fetchval("SELECT admin_account_billing($1, $2::uuid)", session, account)

    async def billing_contact(self, session: str, account: str, ticket: str) -> dict:
        return await self.fetchval(
            "SELECT admin_billing_contact($1, $2::uuid, $3)", session, account, ticket
        )

    async def billing_review(self, session: str) -> dict:
        return await self.fetchval("SELECT admin_billing_review($1)", session)

    async def email_matches(
        self, session: str, account: str, sender: str, message_id_hash: str
    ) -> bool:
        return bool(
            await self.fetchval(
                "SELECT admin_email_matches($1, $2::uuid, $3, $4)",
                session,
                account,
                sender,
                message_id_hash,
            )
        )

    async def email_offboarding(
        self, session: str, account: str, scope: str, location: str | None, ticket: str
    ) -> dict:
        row = await self.fetchrow(
            "SELECT offboarding_id::text AS offboarding_id, delete_at"
            " FROM fichaitor_email_offboarding($1, $2::uuid, $3, $4, $5, false)",
            session,
            account,
            scope,
            location,
            ticket,
        )
        return row or {}

    async def health_summary(self, session: str) -> dict:
        return await self.fetchval("SELECT admin_health_summary($1)", session)

    async def job_stats(self, session: str) -> list[dict]:
        return await self.fetch(
            "SELECT kind, state, jobs, oldest_run_at, stuck FROM admin_job_stats($1)", session
        )

    async def failed_jobs(
        self, session: str, limit: int = 51, after: tuple[str, str] | None = None
    ) -> list[dict]:
        key, ident = after or (None, None)
        return await self.fetch(
            "SELECT job_id::text AS job_id, kind, attempts, finished_at, last_error"
            " FROM admin_failed_jobs($1, $2, $3, $4)",
            session,
            limit,
            key,
            ident,
        )

    async def retention_runs(
        self, session: str, limit: int = 11, after: tuple[str, str] | None = None
    ) -> list[dict]:
        key, ident = after or (None, None)
        return await self.fetch(
            "SELECT run_id, at, deleted FROM admin_retention_runs($1, $2, $3, $4)",
            session,
            limit,
            key,
            ident,
        )

    async def waitlist_totals(
        self, session: str, limit: int = 101, after: tuple[str, str] | None = None
    ) -> list[dict]:
        key, ident = after or (None, None)
        return await self.fetch(
            "SELECT week, channel, score_band, source, people"
            " FROM admin_waitlist_totals($1, $2, $3, $4)",
            session,
            limit,
            key,
            ident,
        )

    async def users(
        self, session: str, limit: int = 51, after: tuple[str, str] | None = None
    ) -> list[dict]:
        key, ident = after or (None, None)
        return await self.fetch(
            "SELECT admin_user_id::text AS admin_user_id, username, role, active, must_enroll,"
            " locked_until, created_at, password_changed_at FROM admin_users($1, $2, $3, $4)",
            session,
            limit,
            key,
            ident,
        )

    async def audit_read(self, session: str, limit: int, before: int | None) -> list[dict]:
        return await self.fetch(
            "SELECT id, at, written_by::text AS written_by, admin_user::text AS admin_user,"
            " username, role, action, account_id::text AS account_id, target, ticket_ref, detail"
            " FROM admin_audit_read($1, $2, $3)",
            session,
            limit,
            before,
        )

    async def grant_entitlement(
        self,
        session: str,
        account: str,
        plan: str,
        quantity: int,
        source: str,
        ends_at: datetime,
        note_code: str | None,
        ticket: str,
    ) -> str:
        value = await self.fetchval(
            "SELECT admin_grant_entitlement($1, $2::uuid, $3, $4, $5, $6, $7, $8)::text",
            session,
            account,
            plan,
            quantity,
            source,
            ends_at,
            note_code,
            ticket,
        )
        return str(value)

    async def trim_entitlement(
        self, session: str, entitlement: str, ends_at: datetime | None, ticket: str
    ) -> None:
        await self.fetchval(
            "SELECT admin_trim_entitlement($1, $2::uuid, $3, $4)",
            session,
            entitlement,
            ends_at,
            ticket,
        )

    async def close_block(self, session: str, block_id: int, ticket: str) -> str:
        return str(
            await self.fetchval(
                "SELECT admin_close_block($1, $2, $3)::text", session, block_id, ticket
            )
        )

    async def request_refund(
        self, session: str, invoice: str, amount: int | None, reason: str, ticket: str
    ) -> str:
        return str(
            await self.fetchval(
                "SELECT admin_request_refund($1, $2, $3, $4, $5)::text",
                session,
                invoice,
                amount,
                reason,
                ticket,
            )
        )

    async def metrics(self, session: str, since: datetime, until: datetime) -> dict:
        return await self.fetchval("SELECT admin_metrics($1, $2, $3)", session, since, until)

    # --- communications by email (docs/contrato-backoffice.md, 10) ------------------------

    async def communication_save(
        self, session: str, communication: str | None, fields: dict[str, Any]
    ) -> str:
        value = await self.fetchval(
            "SELECT admin_communication_save($1, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10,"
            " $11, $12::uuid)::text",
            session,
            communication,
            fields["kind"],
            fields["subject"],
            fields["preheader"],
            fields["headline"],
            list(fields["paragraphs"]),
            fields["button_label"],
            fields["button_url"],
            fields["audience"],
            fields["audience_plan"],
            fields["audience_account"],
        )
        return str(value)

    async def communication_get(self, session: str, communication: str) -> dict:
        return await self.fetchval(
            "SELECT admin_communication_get($1, $2::uuid)", session, communication
        )

    async def communication_audience(self, session: str, communication: str) -> dict:
        return await self.fetchval(
            "SELECT admin_communication_audience($1, $2::uuid)", session, communication
        )

    async def communication_test(self, session: str, communication: str) -> dict:
        return await self.fetchval(
            "SELECT admin_communication_test($1, $2::uuid)", session, communication
        )

    async def communication_confirm(
        self, session: str, communication: str, expected: int, per_minute: int
    ) -> dict:
        return await self.fetchval(
            "SELECT admin_communication_confirm($1, $2::uuid, $3, $4)",
            session,
            communication,
            expected,
            per_minute,
        )

    async def communications(
        self, session: str, limit: int = 51, after: tuple[str, str] | None = None
    ) -> list[dict]:
        key, ident = after or (None, None)
        return await self.fetch(
            "SELECT communication_id::text AS communication_id, kind, subject, audience,"
            " audience_plan, audience_account::text AS audience_account, state, created_at,"
            " created_by, test_sent_at, confirmed_at, confirmed_by, recipients, excluded, sent,"
            " failed, skipped, finished_at FROM admin_communications($1, $2, $3, $4)",
            session,
            limit,
            key,
            ident,
        )
