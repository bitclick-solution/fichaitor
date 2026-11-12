"""Leaving Fichaitor: the one-time code, the restore token and the retention terms.
No I/O. Documented in docs/retencion.md and docs/contrato-web-api.md.

A leave ("baja") is asked for with a code sent by email, stays scheduled for a while
(it can be restored) and then becomes effective: the worker revokes the Google token
and the deletion terms start to count.

Neither the code nor the restore token is ever stored: only a fingerprint is. The
fingerprint of a code ties it to the account, the session and the scope it was asked
for, so a code asked for "ficha" is useless for "cuenta" or from another session.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime

SCOPE_PROFILE = "ficha"  # disconnect Google: the account stays, without profiles
SCOPE_ACCOUNT = "cuenta"  # delete the account
SCOPES = (SCOPE_PROFILE, SCOPE_ACCOUNT)

# Kinds of the jobs of a leave (app/worker/send_mail.py and apply_offboarding.py).
SEND_MAIL_KIND = "send_mail"
APPLY_KIND = "apply_offboarding"
RETENTION_KIND = "retention"
MAIL_CODE = "offboarding_code"
MAIL_SCHEDULED = "offboarding_scheduled"
MAIL_RESTORED = "offboarding_restored"
MAIL_MAX_ATTEMPTS = 3
# The database refuses a leave scheduled for less than this (offboarding_guard in
# db/schema_worker.sql, M5). The grace is at least MIN_GRACE_HOURS; the hour between the
# two absorbs the time from the clock of the API to the one of the database.
FLOOR_HOURS = 47
MIN_GRACE_HOURS = 48
APPLY_MAX_ATTEMPTS = 5

CODE_DIGITS = 6
RESTORE_TOKEN_BYTES = 32

# How the email with the code went, as the web can ask.
MAIL_PENDING = "pending"
MAIL_SENT = "sent"
MAIL_FAILED = "failed"
MAIL_LABELS = {MAIL_PENDING: "pendiente", MAIL_SENT: "enviado", MAIL_FAILED: "fallido"}

# What Google answered when the tokens were revoked.
REVOKE_REVOKED = "revoked"
REVOKE_FAILED = "failed"
REVOKE_SUPERSEDED = "superseded"
REVOKE_NONE = "none"

# Outcome of checking a code.
CODE_OK = "ok"
CODE_WRONG = "wrong"
CODE_GONE = "gone"  # none, expired, used or replaced by a newer one
CODE_LOCKED = "locked"  # too many attempts: that code is dead


def new_code() -> str:
    return f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"


def code_hash(secret: str, account_id: str, session_id: str, scope: str, code: str) -> str:
    """Fingerprint of a code. Keyed: the table alone does not let anyone try codes."""
    message = f"{account_id}\n{session_id}\n{scope}\n{code}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def same_hash(left: str, right: str) -> bool:
    """Constant-time comparison."""
    return hmac.compare_digest(left.encode(), right.encode())


def new_restore_token() -> str:
    return secrets.token_urlsafe(RESTORE_TOKEN_BYTES)


def restore_token_hash(token: str) -> str:
    # 256 bits of entropy: a plain hash is enough, as with session tokens.
    return hashlib.sha256(token.encode()).hexdigest()


def mask_email(email: str) -> str:
    """ "d***o@example.com": enough for the owner to recognise the mailbox."""
    local, at, domain = email.partition("@")
    if not at or not local:
        return "***"
    tail = local[-1] if len(local) > 2 else ""
    return f"{local[0]}***{tail}@{domain}"


@dataclass(frozen=True)
class PendingDeletion:
    """A scheduled leave of an account: what, and when it becomes effective."""

    scope: str
    delete_at: datetime


@dataclass(frozen=True)
class CodeCheck:
    """What came of trying a code. `attempts_left` only means something for CODE_WRONG."""

    outcome: str
    attempts_left: int = 0
    delete_at: datetime | None = None


# --- retention --------------------------------------------------------------------------


@dataclass(frozen=True)
class RetentionPolicy:
    """Terms approved by the owner on 2026-11-12. Values come from app/retention_config.py."""

    google_data_days: int = 30  # everything obtained from Google, after the leave
    removed_days: int = 30  # reviews and profiles Google stopped returning
    session_days: int = 30  # sessions, after they expired or were closed
    audit_months: int = 12  # the record of published replies, after the leave
    waitlist_months: int = 12  # the waiting list, after signing up
    waitlist_launched_at: datetime | None = None  # entries older than the launch go too
    code_days: int = 30  # fingerprints of used or expired codes
    # Messaging channels (docs/canales.md): the messages of a conversation, the events
    # already seen, finished proposals, unlinked addresses and the counts of unknown chats.
    channel_days: int = 90
    # Finished jobs (done or failed), days after they finished (I12). At least 2: the
    # daily jobs of yesterday keep their idempotency key.
    job_days: int = 14
