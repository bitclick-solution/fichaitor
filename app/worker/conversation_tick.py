"""Job conversation_tick: what time alone brings to the conversation (reminders,
expiries, and what waited for the quiet hours to end). Payload: {}. The planner of the
conversation process enqueues one every five minutes, with a key per slot."""

from app.application.conversation_service import ConversationService
from app.domain.channel_jobs import CONVERSATION_TICK
from app.domain.jobs import Job

KIND = CONVERSATION_TICK


class ConversationTick:
    def __init__(self, service: ConversationService) -> None:
        self._service = service

    async def __call__(self, job: Job) -> None:
        await self._service.tick()
