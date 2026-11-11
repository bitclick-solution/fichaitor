"""Job queue on PostgreSQL. Explicit SQL, no ORM.

enqueue() needs only INSERT on job, so the API role can use it.
The rest needs the worker role.
"""

import json
from collections.abc import Sequence
from datetime import datetime

import asyncpg

from app.domain.jobs import DEFAULT_MAX_ATTEMPTS, ERROR_LIMIT, Job

LOST_WORKER_ERROR = "el worker que lo ejecutaba dejó de responder"


class PostgresJobQueue:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def enqueue(
        self,
        kind: str,
        payload: dict,
        *,
        idempotency_key: str | None = None,
        run_at: datetime | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> bool:
        # ON CONFLICT without a target and without RETURNING needs only INSERT privilege.
        status = await self._pool.execute(
            """
            INSERT INTO job (kind, payload, run_at, idempotency_key, max_attempts)
            VALUES ($1, $2::jsonb, coalesce($3, now()), $4, $5)
            ON CONFLICT DO NOTHING
            """,
            kind,
            json.dumps(payload),
            run_at,
            idempotency_key,
            max_attempts,
        )
        return status.endswith(" 1")

    async def claim(self, worker_id: str, kinds: Sequence[str], lock_timeout: float) -> Job | None:
        if not kinds:
            return None
        kinds = list(kinds)
        async with self._pool.acquire() as conn:
            # A job whose worker died during its last attempt has no attempts left.
            await conn.execute(
                """
                UPDATE job
                SET state = 'failed', finished_at = now(), locked_by = NULL, locked_at = NULL,
                    last_error = $3
                WHERE state = 'running' AND kind = ANY($1::text[])
                  AND locked_at < now() - make_interval(secs => $2)
                  AND attempts >= max_attempts
                """,
                kinds,
                float(lock_timeout),
                LOST_WORKER_ERROR,
            )
            # SKIP LOCKED: a row another worker is taking right now is passed over,
            # so two workers never get the same job and neither waits for the other.
            row = await conn.fetchrow(
                """
                WITH next AS (
                    SELECT id FROM job
                    WHERE kind = ANY($2::text[])
                      AND ((state = 'pending' AND run_at <= now())
                        OR (state = 'running'
                            AND locked_at < now() - make_interval(secs => $3)))
                    ORDER BY run_at, created_at
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE job
                SET state = 'running', attempts = job.attempts + 1,
                    locked_by = $1, locked_at = now()
                FROM next
                WHERE job.id = next.id
                RETURNING job.id::text AS id, job.kind, job.payload::text AS payload,
                          job.attempts, job.max_attempts
                """,
                worker_id,
                kinds,
                float(lock_timeout),
            )
        if row is None:
            return None
        return Job(
            id=row["id"],
            kind=row["kind"],
            payload=json.loads(row["payload"]),
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
        )

    # complete() and fail() only act if this worker still holds this very attempt:
    # a worker that was presumed dead cannot close a job that someone else retook.

    async def complete(self, job: Job, worker_id: str) -> bool:
        status = await self._pool.execute(
            """
            UPDATE job
            SET state = 'done', finished_at = now(), locked_by = NULL, locked_at = NULL,
                last_error = NULL
            WHERE id = $1::uuid AND state = 'running' AND locked_by = $2 AND attempts = $3
            """,
            job.id,
            worker_id,
            job.attempts,
        )
        return status.endswith(" 1")

    async def fail(self, job: Job, worker_id: str, error: str, retry_in: float | None) -> bool:
        status = await self._pool.execute(
            """
            UPDATE job
            SET state = CASE WHEN $5::float8 IS NULL THEN 'failed' ELSE 'pending' END,
                run_at = CASE WHEN $5::float8 IS NULL THEN run_at
                              ELSE now() + make_interval(secs => $5::float8) END,
                finished_at = CASE WHEN $5::float8 IS NULL THEN now() END,
                locked_by = NULL, locked_at = NULL, last_error = $4
            WHERE id = $1::uuid AND state = 'running' AND locked_by = $2 AND attempts = $3
            """,
            job.id,
            worker_id,
            job.attempts,
            error[:ERROR_LIMIT],
            None if retry_in is None else float(retry_in),
        )
        return status.endswith(" 1")
