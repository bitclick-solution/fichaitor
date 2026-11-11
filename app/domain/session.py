"""Session tokens. Pure functions.

The browser holds the token; the database only ever sees its SHA-256. A copy of
the `session` table is therefore useless to open a session.
"""

import hashlib
import secrets

SESSION_TTL_SECONDS = 30 * 24 * 3600
TOKEN_BYTES = 32


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    # The token has 256 bits of entropy: a plain hash is enough, no salt or stretching.
    return hashlib.sha256(token.encode()).hexdigest()
