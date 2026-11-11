"""Signed, expiring `state` values for the OAuth flow. Pure functions.

The state is sent to Google and also kept in an HttpOnly cookie. The callback
accepts it only if both match, the signature is ours and it has not expired.
"""

import hashlib
import hmac
import secrets

MAX_AGE_SECONDS = 600


class InvalidState(ValueError):
    pass


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_state(secret: str, now: float) -> str:
    payload = f"{int(now)}.{secrets.token_urlsafe(16)}"
    return f"{payload}.{_sign(secret, payload)}"


def verify_state(secret: str, state: str, cookie_state: str | None, now: float) -> None:
    if not state or not cookie_state or not hmac.compare_digest(state, cookie_state):
        raise InvalidState("state y cookie no coinciden")
    parts = state.split(".")
    if len(parts) != 3:
        raise InvalidState("formato")
    issued, nonce, signature = parts
    if not hmac.compare_digest(signature, _sign(secret, f"{issued}.{nonce}")):
        raise InvalidState("firma")
    if not issued.isdigit() or not 0 <= now - int(issued) <= MAX_AGE_SECONDS:
        raise InvalidState("caducado")
