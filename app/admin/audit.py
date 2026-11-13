"""Audit of the backoffice from the process (docs/admin-facturacion.md, 6.2).

Every action and every sensitive read is audited by its database function, in the same
transaction (login_ok, account_detail_read, refund_requested…). The process adds its own
rows, always with an action that starts with `app_` (the database refuses any other from
this role, and signs the row with its user and the role of the person):

- the reads whose function does not write a row (the chats and the synchronisation of an
  account, the jobs and the retention, the list of users);
- every attempt the database refused because of the role (`app_denied`), so that the log
  also shows who tried what they may not do.

Never personal data: the route, the account id and a code.
"""

import logging

from app.admin.session import Actor
from app.admin.store import AdminError, AdminStore

log = logging.getLogger("fichaitor.admin")

APP_SYNC_READ = "app_sync_read"
APP_CHANNELS_READ = "app_channels_read"
APP_JOBS_READ = "app_jobs_read"
APP_USERS_READ = "app_users_read"
APP_DENIED = "app_denied"
APP_OFFBOARDING_DMARC_FAILED = "app_offboarding_dmarc_failed"


async def audit_read(
    store: AdminStore, actor: Actor, action: str, *, account_id: str | None = None
) -> None:
    await store.audit(actor.user_id, action, account_id=account_id)


async def audit_denied(store: AdminStore, actor: Actor | None, route: str, code: str) -> None:
    """A refusal of the database (role or fresh TOTP), noted with the route template."""
    if actor is None:
        return
    try:
        await store.audit(actor.user_id, APP_DENIED, target=route, detail={"code": code})
    except AdminError:
        log.warning("admin_audit_failed")
