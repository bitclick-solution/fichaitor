"""Leaving Fichaitor: ask for a code, confirm with it, restore.

The API talks to nobody here: the emails are jobs the worker sends, and the leave
becomes effective in the worker when its date arrives. Until then nothing is revoked or
deleted, so that restoring needs no new connection with Google.

Neither the code nor the restore token is stored: only fingerprints. They exist in
clear in the payload of the mail job, which the worker empties once the email is out.

Restoring, from the session or from the link, enqueues the email that says so in the
same write: if anything fails, nothing is restored and the link still works.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.application.rate_limit import SlidingWindowLimiter
from app.domain.offboarding import (
    CODE_GONE,
    CODE_LOCKED,
    CODE_OK,
    MAIL_CODE,
    MAIL_RESTORED,
    MAIL_SCHEDULED,
    CodeCheck,
    code_hash,
    mask_email,
    new_code,
    new_restore_token,
    restore_token_hash,
)
from app.ports.store import Account, Store

log = logging.getLogger("fichaitor.offboarding")


class MailUnavailable(Exception):
    """No mail server is configured: a code could never reach the owner."""


class CodeRateLimited(Exception):
    pass


class LeavePending(Exception):
    """The account already has a scheduled leave: it can only be restored."""


class CodeWrong(Exception):
    def __init__(self, attempts_left: int) -> None:
        super().__init__("código incorrecto")
        self.attempts_left = attempts_left


class CodeGone(Exception):
    """No code, or it expired, was used or was replaced by a newer one."""


class CodeLocked(Exception):
    """Too many wrong attempts: that code is dead."""


@dataclass(frozen=True)
class CodeSent:
    sent_to: str  # masked
    expires_in: int  # seconds


class Offboarding:
    def __init__(
        self,
        store: Store,
        *,
        secret: str,
        mail_available: bool,
        clock: Callable[[], float],
        code_ttl_seconds: int = 600,
        max_attempts: int = 5,
        grace_hours: float = 48.0,
        code_limit: int = 5,
        code_window_seconds: int = 3600,
    ) -> None:
        self._store = store
        self._secret = secret
        self._mail_available = mail_available
        self._clock = clock
        self._code_ttl = code_ttl_seconds
        self._max_attempts = max_attempts
        self._grace = timedelta(hours=grace_hours)
        self._limiter = SlidingWindowLimiter(code_limit, code_window_seconds, clock)

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), UTC)

    async def request_code(self, account: Account, scope: str) -> CodeSent:
        if not (self._mail_available and self._secret):
            raise MailUnavailable
        if account.pending_deletion is not None:
            raise LeavePending
        if account.session_id is None or not account.email:
            raise MailUnavailable
        if not self._limiter.allow(account.id):
            raise CodeRateLimited
        now = self._now()
        code = new_code()
        await self._store.create_offboarding_code(
            account.id,
            account.session_id,
            scope,
            code_hash(self._secret, account.id, account.session_id, scope, code),
            now,
            now + timedelta(seconds=self._code_ttl),
            {
                "template": MAIL_CODE,
                "to": account.email,
                "scope": scope,
                "code": code,
                "minutes": max(self._code_ttl // 60, 1),
            },
        )
        return CodeSent(mask_email(account.email), self._code_ttl)

    async def code_mail_state(self, account: Account) -> str | None:
        return await self._store.offboarding_code_mail(account.id)

    async def confirm(self, account: Account, scope: str, code: str) -> datetime:
        """Schedule the leave. Returns when it becomes effective."""
        if account.pending_deletion is not None:
            raise LeavePending
        if account.session_id is None:
            raise CodeGone
        now = self._now()
        delete_at = now + self._grace
        token = new_restore_token()
        check: CodeCheck = await self._store.confirm_offboarding(
            account.id,
            code_hash(self._secret, account.id, account.session_id, scope, code),
            scope,
            now,
            max_attempts=self._max_attempts,
            delete_at=delete_at,
            restore_hash=restore_token_hash(token),
            mail={
                "template": MAIL_SCHEDULED,
                "to": account.email,
                "scope": scope,
                "delete_at": delete_at.isoformat(),
                "token": token,
            },
        )
        if check.outcome == CODE_OK:
            return check.delete_at or delete_at
        if check.outcome == CODE_GONE:
            raise CodeGone
        if check.outcome == CODE_LOCKED:
            raise CodeLocked
        raise CodeWrong(check.attempts_left)

    async def restore(self, account: Account) -> bool:
        return await self._store.restore_offboarding(
            account.id, self._now(), mail={"template": MAIL_RESTORED}
        )

    async def restore_by_token(self, token: str) -> str | None:
        """The account whose leave was cancelled, or None. Same answer whatever was wrong.
        The token is spent only if the restore is saved: an error leaves it valid."""
        return await self._store.restore_offboarding_by_token(
            restore_token_hash(token), self._now(), mail={"template": MAIL_RESTORED}
        )
