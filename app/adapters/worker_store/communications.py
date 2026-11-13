"""The communications of the backoffice, as the worker reads them to send each email
(db/schema_admin.sql, «Comunicaciones por correo»). The worker's role reads nothing of
those tables: two functions of the owner give it what one email needs and take its
outcome, nothing else."""

import json
import uuid

import asyncpg

OUTCOMES = ("sent", "failed", "skipped")


def _is_uuid(value: str | None) -> bool:
    if value is None:
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


class PostgresCommunications:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def mail(self, communication_id: str, account_id: str | None) -> dict | None:
        """The content of the communication and `deliver`: false when that account must
        not get it any more (it opted out of a commercial one, left or is leaving).
        None if the communication does not exist. Without an account: a test email."""
        if not _is_uuid(communication_id) or (account_id is not None and not _is_uuid(account_id)):
            return None
        raw = await self._pool.fetchval(
            "SELECT fichaitor_communication_mail($1::uuid, $2::uuid)", communication_id, account_id
        )
        if raw is None:
            return None
        return json.loads(raw) if isinstance(raw, str) else raw

    async def result(self, communication_id: str, outcome: str) -> None:
        """One more email sent, failed or skipped, in the counts of the communication."""
        if outcome not in OUTCOMES or not _is_uuid(communication_id):
            return
        await self._pool.execute(
            "SELECT fichaitor_communication_result($1::uuid, $2)", communication_id, outcome
        )
