"""Job channel_action_result: tell the owner, by the channel, how the action they
confirmed there ended.

Payload: {"action_id": "act_...", "ok": bool, "error": "<error code>" | null}. The
executor (app/worker/reply_review.py) enqueues it for every action it closes, with a key
per action; an action confirmed in the panel has no proposal and nothing is sent.
Telling twice is harmless: the conversation only moves a proposal that is still
`ejecutando`.
"""

from app.application.conversation_service import ActionOutcome, ConversationService
from app.domain.actions import ACTION_ID
from app.domain.channel_jobs import CHANNEL_ACTION_RESULT, result_error
from app.domain.jobs import Job, PermanentJobError
from app.worker import log

KIND = CHANNEL_ACTION_RESULT


class ChannelActionResult:
    def __init__(self, service: ConversationService) -> None:
        self._service = service

    async def __call__(self, job: Job) -> None:
        action_id, ok = job.payload.get("action_id"), job.payload.get("ok")
        if not isinstance(action_id, str) or not ACTION_ID.fullmatch(action_id):
            raise PermanentJobError("payload no válido: falta action_id")
        if not isinstance(ok, bool):
            raise PermanentJobError("payload no válido: falta ok")
        error = job.payload.get("error")
        outcome = ActionOutcome(
            ok, None if ok else result_error(error if isinstance(error, str) else "x")
        )
        told = await self._service.handle_action_finished(action_id, outcome)
        log.event("action_result_told" if told else "action_result_not_from_channel", job_id=job.id)
