"""Job billing_purge, once a day: what of the charging is past its six years
(art. 30 of the Código de Comercio; docs/retencion.md). The database function
`billing_purge()` does it, with the term written there: this process deletes nothing by
itself."""

import logging

from app.domain.jobs import Job
from app.ports.billing_store import BillingJobsStore

log = logging.getLogger("fichaitor.billing")

KIND = "billing_purge"


class Purge:
    def __init__(self, store: BillingJobsStore) -> None:
        self._store = store

    async def __call__(self, job: Job) -> None:
        counts = await self._store.purge()
        log.info("purga del cobro a 6 años: %s", {k: v for k, v in counts.items() if v})
