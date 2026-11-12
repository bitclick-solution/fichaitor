"""Job channel_inbound: what arrived by Telegram's webhook, handed to the conversation.

Payload: {"channel": "telegram", "update": {...}}, as the API received it. The update is
read again with `telegram.parse_update`: whatever the payload says, only what that
function accepts reaches the conversation, and from it only the chat the update came
from (a private chat whose sender is that same chat).

The update may carry a link code and the owner's own words: the payload is emptied as
soon as the job is over, however it ends. A failure is not retried: the event is
recorded as seen first, so a second attempt would do nothing (the owner presses again).
"""

import logging
from typing import Protocol

from app.adapters.messaging import telegram
from app.application.conversation_service import ConversationService
from app.domain.channel_jobs import CHANNEL_INBOUND
from app.domain.jobs import Job, PermanentJobError
from app.worker import log

KIND = CHANNEL_INBOUND


class InboundJobs(Protocol):
    async def scrub_job(self, job_id: str) -> None: ...

    async def scrub_finished_jobs(self, kind: str) -> int: ...


class ChannelInbound:
    def __init__(self, service: ConversationService, jobs: InboundJobs) -> None:
        self._service = service
        self._jobs = jobs

    async def __call__(self, job: Job) -> None:
        try:
            if job.payload.get("channel") != telegram.CHANNEL:
                raise PermanentJobError("payload no válido: canal desconocido")
            event = telegram.parse_update(job.payload.get("update"))
            if event is None:
                log.event("inbound_ignored", job_id=job.id)
                return
            try:
                await self._service.handle_inbound(event)
            except Exception as exc:
                # Only the type: the message may quote what the owner wrote.
                raise PermanentJobError(
                    f"evento de canal sin procesar: {type(exc).__name__}"
                ) from None
        finally:
            try:
                await self._jobs.scrub_job(job.id)
            except Exception as exc:  # on_lost tries again
                log.event("inbound_not_scrubbed", logging.WARNING, error=type(exc).__name__)

    async def on_lost(self, kind: str) -> int:
        """Jobs whose worker died before emptying them."""
        return await self._jobs.scrub_finished_jobs(kind)
