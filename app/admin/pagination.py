"""Pages of the listings of the backoffice: keyset, never offset (docs/contrato-backoffice.md,
«Paginación»).

Each listing has a stable order key with an index behind it: a value (a date or a name)
and an id. The cursor is that key of the last row shown, opaque for the interface: the
base64url of a small JSON array. The next page starts right after it, so a row inserted
between two pages neither repeats another one nor leaves a gap.

The functions of the database take the key as `p_after_key` and `p_after_id` and allow
one row more than the largest page: the process asks for `limit + 1` rows and, if the
extra one comes, there is another page.
"""

import base64
import binascii
import json
from collections.abc import Callable, Sequence
from datetime import date, datetime
from typing import Any

from app.admin.store import AdminError

MAX_LIMIT = 200
MAX_AUDIT_LIMIT = 500

Key = tuple[str, str]


def _text(value: Any) -> str:
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def encode(key: str, ident: str) -> str:
    raw = json.dumps([key, ident], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(cursor: str | None) -> Key | None:
    """(key, id) of a cursor, or None for the first page. A cursor that was not made here
    is a 422, never a database error."""
    if cursor is None or cursor == "":
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        key, ident = json.loads(raw)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise AdminError(
            422, "invalid_cursor", "El cursor no vale: vuelve a la primera página."
        ) from exc
    if not isinstance(key, str) or not isinstance(ident, str) or len(key) + len(ident) > 600:
        raise AdminError(422, "invalid_cursor", "El cursor no vale: vuelve a la primera página.")
    return key, ident


def from_next(next_key: dict | None) -> str | None:
    """The cursor of the `next` that admin_list_page returns ({"key", "id"} or null)."""
    if not next_key:
        return None
    return encode(str(next_key["key"]), str(next_key["id"]))


def page(
    rows: Sequence[dict], limit: int, key_of: Callable[[dict], tuple[Any, Any]]
) -> tuple[list[dict], str | None]:
    """The rows to show and the cursor of the next page, from `limit + 1` rows."""
    shown = list(rows[:limit])
    if len(rows) <= limit or not shown:
        return shown, None
    key, ident = key_of(shown[-1])
    return shown, encode(_text(key), _text(ident))
