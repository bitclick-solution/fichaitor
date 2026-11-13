"""Authentication of the backoffice (docs/admin-facturacion.md, 6.2): passwords, TOTP,
recovery codes and the tokens of the sessions and enrollment links.

What lives here and what lives in the database:
- Here, with the pepper and the TOTP key of admin.env: Argon2id of the passwords and of
  the recovery codes, the AES-256-GCM of the TOTP secrets and checking a code. A copy
  of the database is not enough to make codes nor to test passwords offline.
- In the database (db/schema_admin.sql): the last TOTP step accepted (a code never works
  twice), the lock after 5 failures, the global cap of 20 failures per hour, the
  sessions and the audit log.

Dependencies: argon2-cffi (Argon2id, RFC 9106) and pyotp (RFC 6238); cryptography for
AES-GCM. All three pinned in pyproject.toml and uv.lock.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pyotp
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

TOTP_PERIOD = 30
TOTP_DIGITS = 6
TOTP_ISSUER = "Fichaitor backoffice"
RECOVERY_CODES = 10
PASSWORD_MIN = 12
PASSWORD_MAX = 128
ENROLL_STATE_SECONDS = 15 * 60

_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
_CODE = re.compile(r"^[0-9]{6}$")
_COMMON_FILE = Path(__file__).resolve().parent / "data" / "common_passwords.txt"


# --- tokens ------------------------------------------------------------------------------


def new_token() -> str:
    """A session or enrollment token: 32 random bytes, URL-safe."""
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """What the database keeps of a token: its SHA-256 in hexadecimal."""
    return hashlib.sha256(token.encode()).hexdigest()


# --- passwords -----------------------------------------------------------------------------


@lru_cache(maxsize=1)
def common_passwords() -> frozenset[str]:
    """The list shipped with the image (app/admin/data/common_passwords.txt), lowercased."""
    try:
        lines = _COMMON_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return frozenset()
    return frozenset(
        line.strip().lower() for line in lines if line.strip() and not line.startswith("#")
    )


def _is_sequence(text: str) -> bool:
    if len(set(text)) <= 2:
        return True
    steps = {ord(b) - ord(a) for a, b in zip(text, text[1:], strict=False)}
    return len(steps) == 1 and steps.pop() in (-1, 1)


def password_problem(password: str, username: str) -> str | None:
    """Why a new password is refused, as a code for the screen; None if it is fine.

    At least 12 characters (and 128 at most), not in the list of common passwords, not
    the user name nor containing it, and not a run of one or two characters or a plain
    sequence ("abcdefghijkl", "123456789012")."""
    if len(password) < PASSWORD_MIN:
        return "password_too_short"
    if len(password) > PASSWORD_MAX:
        return "password_too_long"
    folded = password.strip().lower()
    if folded in common_passwords():
        return "password_common"
    if username and username.lower() in folded:
        return "password_has_username"
    if _is_sequence(folded):
        return "password_common"
    return None


class Passwords:
    """Argon2id over HMAC-SHA256(pepper, secret): the pepper stays out of the database.
    Used for the passwords and for the recovery codes."""

    def __init__(
        self, pepper: str, *, time_cost: int = 3, memory_kib: int = 65536, parallelism: int = 4
    ) -> None:
        if not pepper:
            raise ValueError("falta la pimienta")
        self._pepper = pepper.encode()
        self._hasher = PasswordHasher(
            time_cost=time_cost,
            memory_cost=memory_kib,
            parallelism=parallelism,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        self._dummy = self._hasher.hash(self._peppered(secrets.token_urlsafe(16)))

    def _peppered(self, secret: str) -> bytes:
        return hmac.new(self._pepper, secret.encode("utf-8"), hashlib.sha256).digest()

    def hash(self, secret: str) -> str:
        return self._hasher.hash(self._peppered(secret))

    def verify(self, stored: str | None, secret: str) -> bool:
        """Whether `secret` matches. With no stored hash, the same work is done against a
        dummy one: the answer takes the same time whether the user exists or not."""
        try:
            return self._hasher.verify(stored or self._dummy, self._peppered(secret)) and bool(
                stored
            )
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, stored: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(stored)
        except InvalidHashError:
            return True


# --- recovery codes ------------------------------------------------------------------------


def new_recovery_codes() -> list[str]:
    """10 codes like "k7m2-q9xa-4tpz" (about 59 bits each), shown once."""
    codes = []
    for _ in range(RECOVERY_CODES):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(12))
        codes.append(f"{raw[:4]}-{raw[4:8]}-{raw[8:]}")
    return codes


def normalize_recovery_code(code: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (code or "").lower())


# --- encryption of the TOTP secrets --------------------------------------------------------


class SecretBox:
    """AES-256-GCM with the key of admin.env (FICHAITOR_ADMIN_TOTP_KEY). `purpose` is the
    associated data: a secret sealed for one user (or one enrollment link) cannot be
    moved to another row and still open."""

    _PREFIX = "v1."

    def __init__(self, key_hex: str) -> None:
        key = bytes.fromhex(key_hex)
        if len(key) != 32:
            raise ValueError("la clave del TOTP son 32 bytes")
        self._aead = AESGCM(key)

    def seal(self, plaintext: bytes, purpose: str) -> str:
        nonce = os.urandom(12)
        sealed = self._aead.encrypt(nonce, plaintext, purpose.encode())
        return self._PREFIX + base64.urlsafe_b64encode(nonce + sealed).decode().rstrip("=")

    def open(self, token: str | None, purpose: str) -> bytes | None:
        if not token or not token.startswith(self._PREFIX):
            return None
        body = token[len(self._PREFIX) :]
        try:
            raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
            return self._aead.decrypt(raw[:12], raw[12:], purpose.encode())
        except (ValueError, InvalidTag):
            return None

    # The TOTP secret of a user, as admin_user.totp_secret_enc keeps it.
    def seal_totp(self, secret: str, username: str) -> str:
        return self.seal(secret.encode(), f"totp:{username}")

    def open_totp(self, sealed: str | None, username: str) -> str | None:
        plain = self.open(sealed, f"totp:{username}")
        return plain.decode() if plain is not None else None

    # The secret shown in the QR, between "start" and "finish" of an enrollment: carried by
    # the browser, sealed to that link (or session) and valid 15 minutes. Nothing is kept
    # in the database until the code is confirmed.
    def seal_state(self, secret: str, bound_to: str, now: float) -> str:
        body = json.dumps({"s": secret, "e": int(now) + ENROLL_STATE_SECONDS}).encode()
        return self.seal(body, f"enroll:{bound_to}")

    def open_state(self, state: str | None, bound_to: str, now: float) -> str | None:
        plain = self.open(state, f"enroll:{bound_to}")
        if plain is None:
            return None
        try:
            body = json.loads(plain)
        except ValueError:
            return None
        if not isinstance(body, dict) or int(body.get("e", 0)) < now:
            return None
        secret = body.get("s")
        return secret if isinstance(secret, str) else None


# --- TOTP ----------------------------------------------------------------------------------


def new_totp_secret() -> str:
    """160 random bits in base32: what Google Authenticator reads."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_PERIOD).provisioning_uri(
        name=username, issuer_name=TOTP_ISSUER
    )


def totp_step(secret: str, code: str, now: float) -> int | None:
    """The 30-second step whose code is `code`, in the window of ±1 step around `now`;
    None if none. The database then refuses a step not later than the last accepted (a
    code never works twice) or outside its own window."""
    code = re.sub(r"\s", "", code or "")
    if not _CODE.match(code):
        return None
    otp = pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_PERIOD)
    current = int(now // TOTP_PERIOD)
    found = None
    for step in (current - 1, current, current + 1):
        if hmac.compare_digest(otp.generate_otp(step), code) and found is None:
            found = step
    return found


def csrf_token(pepper: str, session_token: str) -> str:
    """The CSRF token of a session: derived from its cookie, kept nowhere. The browser
    sends it in X-CSRF-Token with every request that changes something."""
    key = hashlib.sha256(b"fichaitor-admin/csrf/" + pepper.encode()).digest()
    return hmac.new(key, session_token.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class AuthKit:
    """What the routes need to authenticate: built once from the settings."""

    passwords: Passwords
    box: SecretBox
    pepper: str
    clock: Callable[[], float] = time.time

    def csrf(self, session_token: str) -> str:
        return csrf_token(self.pepper, session_token)
