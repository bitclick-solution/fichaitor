"""Signed, expiring `state` values for the OAuth flow. Pure functions.

The state is sent to Google and also kept in an HttpOnly cookie. The callback
accepts it only if both match, the signature is ours and it has not expired.

    state = <issued>.<nonce>.<flags>.<hmac of "<issued>.<nonce>.<flags>">

`flags` carries what the person chose before going to Google, signed with the rest:
"1" opposes the commercial communications from the sign-up (LSSI art. 21.2), "0" does
not. Changing it breaks the signature and the match with the cookie.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass

MAX_AGE_SECONDS = 600
_FLAGS = ("0", "1")


class InvalidState(ValueError):
    pass


@dataclass(frozen=True)
class OAuthState:
    # The box «No quiero recibir novedades ni ofertas…» was ticked before connecting.
    commercial_optout: bool = False


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_state(secret: str, now: float, *, commercial_optout: bool = False) -> str:
    payload = f"{int(now)}.{secrets.token_urlsafe(16)}.{'1' if commercial_optout else '0'}"
    return f"{payload}.{_sign(secret, payload)}"


def verify_state(secret: str, state: str, cookie_state: str | None, now: float) -> OAuthState:
    if not state or not cookie_state or not hmac.compare_digest(state, cookie_state):
        raise InvalidState("state y cookie no coinciden")
    parts = state.split(".")
    if len(parts) != 4:
        raise InvalidState("formato")
    issued, nonce, flags, signature = parts
    if not hmac.compare_digest(signature, _sign(secret, f"{issued}.{nonce}.{flags}")):
        raise InvalidState("firma")
    if flags not in _FLAGS:
        raise InvalidState("formato")
    if not issued.isdigit() or not 0 <= now - int(issued) <= MAX_AGE_SECONDS:
        raise InvalidState("caducado")
    return OAuthState(commercial_optout=flags == "1")
