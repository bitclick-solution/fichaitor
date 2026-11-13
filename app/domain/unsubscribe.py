"""The link to opt out of the commercial communications (LEEME.md, 12; RFC 8058).

The token is signed, one per account and for this purpose only:

    token = base64url(account_id (16 bytes) + HMAC-SHA256(secret, PURPOSE + account_id))

- Signed with FICHAITOR_UNSUBSCRIBE_SECRET, the same in the API (which checks it) and in
  the worker (which writes it in each email). Nothing is stored: the database never holds
  a token, and a copy of it is no use to forge one.
- One purpose: the HMAC covers a fixed label, so a token of anything else signed with the
  same secret does not pass here, and this one does nothing else than opting out.
- No short expiry: an email read months later still has to work (LSSI art. 21: the
  opt-out is offered in every email). Changing the secret invalidates every link sent.
- 64 characters with no separator: the redaction of the fake mailer hides it whole.

The token goes in the fragment of the web link (/baja-comunicaciones#<token>), which never
reaches a server log. The one-click URL of List-Unsubscribe has to carry it in the URL
(the client POSTs a fixed body, RFC 8058): it is there, in the query. Leaked, all it does
is opt that account out of commercial emails.
"""

import base64
import binascii
import hashlib
import hmac
import uuid
from urllib.parse import quote

PURPOSE = b"fichaitor/comunicaciones/baja/v1:"
_MAC_BYTES = 32
_ID_BYTES = 16
MAX_TOKEN_LENGTH = 200
# The web page of the opt-out (the interface session builds it) and the one-click route.
PAGE_PATH = "/baja-comunicaciones"
ONE_CLICK_PATH = "/api/v1/public/unsubscribe/one-click"
# For the test email of a draft: it is not an account, so its link opts out nobody.
TEST_TOKEN = "prueba"


def _mac(secret: str, account: bytes) -> bytes:
    return hmac.new(secret.encode(), PURPOSE + account, hashlib.sha256).digest()


def sign(secret: str, account_id: str) -> str:
    if not secret:
        raise ValueError("falta FICHAITOR_UNSUBSCRIBE_SECRET")
    account = uuid.UUID(account_id).bytes
    raw = account + _mac(secret, account)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def verify(secret: str, token: str) -> str | None:
    """The account of a valid token; None for anything else (malformed, other secret,
    other purpose). Constant time on the signature."""
    if not secret or not isinstance(token, str) or not 1 <= len(token) <= MAX_TOKEN_LENGTH:
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (binascii.Error, ValueError):
        return None
    if len(raw) != _ID_BYTES + _MAC_BYTES:
        return None
    account, mac = raw[:_ID_BYTES], raw[_ID_BYTES:]
    if not hmac.compare_digest(mac, _mac(secret, account)):
        return None
    return str(uuid.UUID(bytes=account))


def page_url(public_base_url: str, token: str) -> str:
    """${enlace_baja}: https://fichaitor.es/baja-comunicaciones#<token>. From the
    configuration only, never from a request; the token in the fragment."""
    return f"{public_base_url.rstrip('/')}{PAGE_PATH}#{quote(token, safe='')}"


def one_click_url(public_base_url: str, token: str) -> str:
    """The URL of List-Unsubscribe: the mail client POSTs to it (RFC 8058)."""
    return f"{public_base_url.rstrip('/')}{ONE_CLICK_PATH}?token={quote(token, safe='')}"


def headers(public_base_url: str, token: str) -> tuple[tuple[str, str], ...]:
    """List-Unsubscribe with the https URL of one click, and List-Unsubscribe-Post.

    No mailto: an opt-out written to soporte@ would be done by hand, and the one-click
    URL is what Gmail and Yahoo ask of bulk senders."""
    return (
        ("List-Unsubscribe", f"<{one_click_url(public_base_url, token)}>"),
        ("List-Unsubscribe-Post", "List-Unsubscribe=One-Click"),
    )
