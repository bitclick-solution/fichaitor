"""The PDF of an issued invoice or credit note, as the worker reads it to attach it to its
email (docs/admin-facturacion.md, 4.3). With the worker's role, which reads only
(id, state, pdf) of billing_document (db/schema_billing.sql)."""

import uuid

import asyncpg


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


class PostgresBillingDocuments:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def pdf(self, document_id: str) -> bytes | None:
        if not _is_uuid(document_id):
            return None
        return await self._pool.fetchval(
            "SELECT pdf FROM billing_document WHERE id = $1::uuid AND state = 'emitida'",
            document_id,
        )
