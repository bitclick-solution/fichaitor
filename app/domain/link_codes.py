"""Link codes: how a chat proves it belongs to an account. No I/O.

The owner asks for a code from the web session, and sends it to the bot (or opens
`https://t.me/<bot>?start=<code>`). Whoever sends a valid code gets that chat linked
to the account, so the code is a short lived secret:

- 6 digits from a cryptographic source, valid for 10 minutes and for a single use;
- only its SHA-256 is stored, never the code;
- a chat that sends 3 wrong codes in a row is ignored for an hour.

Six digits are a million values: the hash does not protect a stored code from being
guessed by brute force, the 10 minutes and the single use do. An optional pepper (a
secret of the server that is not in the database) closes that gap as well.

B3 of docs/seguridad/revision-2026-11-12.md: every code comes with a long random
token, and the link `t.me/<bot>?start=<token>` carries the token, never the six
digits. The six digits are only for typing them. Both are the same single use code:
whichever is sent first spends it. Besides the count of each chat, there is a count of
wrong codes of every chat together, per window: past its limit, typed codes are not
looked at (the links still work, the token cannot be guessed).
"""

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum

CODE_LENGTH = 6
CODE_TTL = timedelta(minutes=10)
MAX_FAILED_ATTEMPTS = 3
BLOCK_DURATION = timedelta(hours=1)
# An unknown chat gets the generic answer once in this period (flow 13 of the script).
UNLINKED_REPLY_EVERY = timedelta(hours=24)
# Wrong codes of every chat together. Past the limit within the window, typed codes are
# paused until the window ends. Over an hour, 50 guesses at a million values.
GLOBAL_FAILURE_WINDOW = timedelta(hours=1)
DEFAULT_GLOBAL_MAX_FAILURES = 50

# 32 random bytes in base64url: 43 characters of A-Z, a-z, 0-9, _ and -, the alphabet
# of Telegram's ?start= (up to 64 characters).
TOKEN_BYTES = 32
TOKEN_MIN_LENGTH, TOKEN_MAX_LENGTH = 32, 64

_CODE = re.compile(rf"\d{{{CODE_LENGTH}}}")
_TOKEN = re.compile(rf"[A-Za-z0-9_\-]{{{TOKEN_MIN_LENGTH},{TOKEN_MAX_LENGTH}}}")


def generate_code() -> str:
    """Six digits, leading zeros included."""
    return f"{secrets.randbelow(10**CODE_LENGTH):0{CODE_LENGTH}d}"


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def is_code_shaped(text: object) -> bool:
    return isinstance(text, str) and _CODE.fullmatch(text.strip()) is not None


def is_token_shaped(text: object) -> bool:
    return isinstance(text, str) and _TOKEN.fullmatch(text.strip()) is not None


def failure_window(now: datetime) -> datetime:
    """Start of the fixed window of the global count that `now` falls in."""
    seconds = int(GLOBAL_FAILURE_WINDOW.total_seconds())
    return datetime.fromtimestamp(int(now.timestamp()) // seconds * seconds, UTC)


def code_hash(code: str, pepper: str = "") -> str:
    """What is stored instead of the code: SHA-256, keyed with the pepper if there is one."""
    data = code.strip().encode()
    if pepper:
        return hmac.new(pepper.encode(), data, hashlib.sha256).hexdigest()
    return hashlib.sha256(data).hexdigest()


def address_hash(channel: str, address: str) -> str:
    """Chats that are not linked are remembered (attempts, last answer) by hash only."""
    return hashlib.sha256(f"{channel}\n{address}".encode()).hexdigest()


@dataclass(frozen=True)
class LinkCode:
    code_hash: str
    account_id: str
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None = None
    token_hash: str | None = None  # the long token of the link, same code


def new_link_code(account_id: str, now: datetime, pepper: str = "") -> tuple[str, LinkCode]:
    """The code to show once to the owner, and the record to store (without the code)."""
    code = generate_code()
    return code, LinkCode(
        code_hash=code_hash(code, pepper),
        account_id=account_id,
        created_at=now,
        expires_at=now + CODE_TTL,
    )


@dataclass(frozen=True)
class IssuedLink:
    """What the web session gets, once: the six digits to type and the token of the
    link. The record keeps neither, only their hashes."""

    code: str
    token: str
    record: LinkCode


def issue_link(account_id: str, now: datetime, pepper: str = "") -> IssuedLink:
    code, record = new_link_code(account_id, now, pepper)
    token = generate_token()
    return IssuedLink(code, token, replace(record, token_hash=code_hash(token, pepper)))


class CodeCheck(StrEnum):
    OK = "ok"
    UNKNOWN = "unknown"
    EXPIRED = "expired"
    USED = "used"


def check_code(record: LinkCode | None, now: datetime) -> CodeCheck:
    if record is None:
        return CodeCheck.UNKNOWN
    if record.used_at is not None:
        return CodeCheck.USED
    if now >= record.expires_at:
        return CodeCheck.EXPIRED
    return CodeCheck.OK


@dataclass(frozen=True)
class SenderState:
    """What is remembered of a chat that is not linked."""

    failed_attempts: int = 0
    blocked_until: datetime | None = None
    last_unlinked_reply_at: datetime | None = None


def is_blocked(state: SenderState, now: datetime) -> bool:
    return state.blocked_until is not None and now < state.blocked_until


def register_failure(state: SenderState, now: datetime) -> SenderState:
    """One more wrong code. The third in a row blocks the chat for an hour; the count
    starts again when the block is over."""
    if state.blocked_until is not None and now >= state.blocked_until:
        state = replace(state, failed_attempts=0, blocked_until=None)
    failed = state.failed_attempts + 1
    if failed >= MAX_FAILED_ATTEMPTS:
        return replace(state, failed_attempts=failed, blocked_until=now + BLOCK_DURATION)
    return replace(state, failed_attempts=failed)


def register_attempt(state: SenderState, now: datetime) -> SenderState | None:
    """One more code of that chat, counted BEFORE the code is looked at, so that two
    codes of the same chat handled at once cannot both slip under the limit. None if
    the chat is blocked: the code is not looked at. The attempt that reaches the limit
    blocks the chat; a right code lifts the block (`register_success`)."""
    if is_blocked(state, now):
        return None
    return register_failure(state, now)


def register_success(state: SenderState) -> SenderState:
    return replace(state, failed_attempts=0, blocked_until=None)


def may_send_unlinked_reply(state: SenderState, now: datetime) -> bool:
    last = state.last_unlinked_reply_at
    return last is None or now - last >= UNLINKED_REPLY_EVERY
