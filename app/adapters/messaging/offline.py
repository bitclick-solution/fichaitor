"""A channel that is not configured (no bot token): every send fails as temporary, so
the conversation keeps its proposals for later and nothing is lost. Used by the
conversation process when FICHAITOR_TELEGRAM_BOT_TOKEN is empty."""

from dataclasses import dataclass

from app.domain.channel_messages import OutboundMessage
from app.ports.messaging import MessagingUnavailable, SentMessage


@dataclass
class OfflineChannel:
    name: str
    # The conversation does not even try it: nothing is counted in the bag of a channel
    # that cannot send (app/domain/message_allowance.py).
    available = False

    async def send(self, address: str, message: OutboundMessage) -> SentMessage:
        raise MessagingUnavailable(f"{self.name}: canal sin configurar")

    async def acknowledge(self, callback_id: str, notice: str = "") -> None:
        return None

    async def clear_buttons(self, address: str, channel_message_id: str, text: str) -> None:
        return None
