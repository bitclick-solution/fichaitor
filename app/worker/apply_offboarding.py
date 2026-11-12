"""Job apply_offboarding: make a scheduled leave effective when its date arrives.

Payload: {"offboarding_id": "<uuid>"}. The API enqueues it when the owner confirms the
leave, to run at its date. The job decides again when it runs: a leave that was
restored, or whose date has not come, is left alone. The daily retention pass calls the
same code for any leave whose job was lost.

What it does, in order:

1. Makes the leave effective in the database, in one transaction: it can no longer be
   restored, the account has no profiles and, when the scope is the account, the
   account is deleted and its sessions closed.
2. Revokes at Google EVERY stored connection of that user, not only the latest, and
   deletes each encrypted token as soon as its revocation is settled.
   https://developers.google.com/identity/protocols/oauth2/web-server#tokenrevoke
3. Records how it went. If Google could not be reached after every attempt, the tokens
   are deleted all the same and the leave is marked 'failed', with a warning in the log:
   a token that will never be used again is not kept.

If the owner has already connected again, nothing is revoked: Google revokes the whole
grant, and that would cut the new connection. The old tokens are only deleted.

What was obtained from Google is deleted later, by the retention task (30 days at
most), or at once if the owner connects again.

The refresh token lives only in local variables. No error or log carries it.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from app.adapters.crypto.sealed import unseal
from app.domain.jobs import Job, JobError, PermanentJobError
from app.domain.offboarding import (
    REVOKE_FAILED,
    REVOKE_NONE,
    REVOKE_REVOKED,
    REVOKE_SUPERSEDED,
)
from app.ports.google_profile import GoogleProfile, GoogleRejected
from app.ports.worker_store import WorkerStore
from app.worker import log

KIND = "apply_offboarding"


def _offboarding_id(job: Job) -> str:
    value = job.payload.get("offboarding_id")
    try:
        return str(uuid.UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise PermanentJobError("payload no válido: falta offboarding_id") from None


class ApplyOffboarding:
    def __init__(
        self,
        store: WorkerStore,
        google: GoogleProfile,
        private_key: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._google = google
        self._private_key = private_key
        self._clock = clock

    async def __call__(self, job: Job) -> None:
        # On the last attempt nothing is left for later: what cannot be revoked now is
        # deleted and recorded as not revoked.
        await self.apply(_offboarding_id(job), give_up=job.is_last_attempt)

    async def apply(self, offboarding_id: str, *, give_up: bool = False) -> str | None:
        """How the revocation ended, or None if there was nothing to do."""
        leave = await self._store.begin_offboarding(offboarding_id, self._clock())
        if leave is None:
            log.event("offboarding_skipped", offboarding_id=offboarding_id)
            return None
        tokens, reconnected = await self._store.leave_tokens(leave.id)
        failed = 0
        for stored in tokens:
            if not reconnected and not await self._revoke(stored.refresh_token_enc, give_up):
                failed += 1
            # Settled, one way or the other: the token is not kept a moment longer.
            await self._store.delete_connection(stored.connection_id)
        if reconnected:
            state = REVOKE_SUPERSEDED
        elif not tokens:
            state = REVOKE_NONE
        else:
            state = REVOKE_FAILED if failed else REVOKE_REVOKED
        await self._store.finish_revocation(leave.id, state)
        log.event(
            "offboarding_effective",
            logging.WARNING if failed else logging.INFO,
            offboarding_id=leave.id,
            scope=leave.scope,
            connections=len(tokens),
            not_revoked=failed,
            revocation=state,
        )
        return state

    async def _revoke(self, refresh_token_enc: str, give_up: bool) -> bool:
        """True if Google revoked the token or it was no longer valid. False if it
        could not be revoked and never will. Raises JobError to try again later."""
        try:
            refresh_token = unseal(self._private_key, refresh_token_enc)
        except Exception:
            return False  # cannot be read: it cannot be used either
        try:
            await self._google.revoke_token(refresh_token)
        except GoogleRejected:
            return False
        except Exception as exc:
            if give_up:
                return False
            # Only the type: the message may quote the request.
            raise JobError(
                f"fallo temporal al revocar el acceso en Google: {type(exc).__name__}"
            ) from None
        return True

    async def on_exhausted(self, job: Job) -> None:
        """Out of attempts for a reason other than Google: finish without it."""
        try:
            offboarding_id = _offboarding_id(job)
        except PermanentJobError:
            return
        await self.apply(offboarding_id, give_up=True)
