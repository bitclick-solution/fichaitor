"""What the worker does when Google says the owner's grant is gone for good.

`refresh_access_token` raises GrantRevoked only for `invalid_grant`: the refresh token
is dead (the owner removed the access in myaccount.google.com, or it expired). A 5xx, a
429 or a network failure is something else and is tried again; a 401 without that code
only asks the owner to connect again, as before.

Then the connection is marked as revoked and its encrypted token deleted: it is worth
nothing and is not kept a moment longer. Nothing is revoked at Google: it already is.
If that Google user is left without any valid connection, the countdown starts: the
daily retention pass deletes what was obtained from Google of all their profiles 30
days later (docs/retencion.md), unless the owner connects again before.

Sessions stay open and the panel shows `needs_reconnect`, as with any lost grant.
"""

import logging
from collections.abc import Callable
from datetime import datetime

from app.ports.worker_store import WorkerStore
from app.worker import log


async def record_revoked_grant(
    store: WorkerStore,
    google_sub: str,
    connection_id: str,
    clock: Callable[[], datetime],
    job_id: str,
) -> bool:
    """Mark the connection as revoked and delete its token. Returns True when the
    Google user is left without any valid connection (the countdown starts).

    A failure of the database goes up: the job is tried again, Google answers
    `invalid_grant` again and the token is deleted then."""
    orphaned = await store.revoke_connection(google_sub, connection_id, clock())
    log.event(
        "google_grant_revoked",
        logging.WARNING,
        job_id=job_id,
        countdown_started=orphaned,
    )
    return orphaned
