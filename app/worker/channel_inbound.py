"""Job channel_inbound: what arrived by the webhook of Telegram or of WhatsApp, handed to
the conversation.

Payload: {"channel": "telegram", "update": {...}}, as the API received it, or
{"channel": "whatsapp", "update": {"phone_number_id", "message", "name"}}, one message of a
delivery of Meta. The update is read again with the parser of its channel
(`telegram.parse_update`, `whatsapp.parse_update`): whatever the payload says, only what
that function accepts reaches the conversation, and from it only the sender of the update
(Telegram: a private chat whose sender is that same chat; WhatsApp: the phone that wrote
to our number).

The update may carry a link code and the owner's own words: the payload is emptied as
soon as the job is over, however it ends. A failure is not retried: the event is
recorded as seen first, so a second attempt would do nothing (the owner presses again).
"""

import logging
from collections.abc import Callable, Mapping
from typing import Protocol

from app.adapters.messaging import telegram
from app.application.conversation_service import ConversationService
from app.domain.channel_jobs import CHANNEL_INBOUND
from app.domain.channel_messages import InboundEvent
from app.domain.jobs import Job, PermanentJobError
from app.worker import log

KIND = CHANNEL_INBOUND


class InboundJobs(Protocol):
    async def scrub_job(self, job_id: str) -> None: ...

    async def scrub_finished_jobs(self, kind: str) -> int: ...


Parser = Callable[[object], InboundEvent | None]


class ChannelInbound:
    def __init__(
        self,
        service: ConversationService,
        jobs: InboundJobs,
        parsers: Mapping[str, Parser] | None = None,
    ) -> None:
        """`parsers`: the parser of each channel this process attends. Telegram by
        default; WhatsApp only with its number configured."""
        self._service = service
        self._jobs = jobs
        self._parsers: Mapping[str, Parser] = parsers or {telegram.CHANNEL: telegram.parse_update}

    async def __call__(self, job: Job) -> None:
        try:
            parser = self._parsers.get(str(job.payload.get("channel")))
            if parser is None:
                raise PermanentJobError("payload no válido: canal desconocido")
            event = parser(job.payload.get("update"))
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
