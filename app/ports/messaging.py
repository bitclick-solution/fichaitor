"""A messaging channel: Telegram and WhatsApp. The conversation only knows this
interface."""

from dataclasses import dataclass
from typing import Protocol

from app.domain.channel_messages import OutboundMessage


class MessagingError(Exception):
    """The channel did not take the message. The text is a fixed string: never the
    message, the chat, the credentials nor anything the provider said."""


class MessagingUnavailable(MessagingError):
    """Worth trying again later (network, 5xx, flood control)."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class MessagingRejected(MessagingError):
    """Trying again will not help: the chat blocked the bot, or the request is wrong."""


@dataclass(frozen=True)
class SentMessage:
    channel_message_id: str


class MessagingChannel(Protocol):
    """Two optional attributes, read with getattr by the conversation:
    - `available` (True by default): False for a channel that is not configured; nothing
      is tried on it, and nothing is counted in the bag of WhatsApp messages.
    - `shows_notices` (True by default): False if `acknowledge` cannot show its notice to
      whoever pressed (WhatsApp); the conversation then sends the notice as a message."""

    name: str

    async def send(self, address: str, message: OutboundMessage) -> SentMessage:
        """Send a message, with its buttons if it has any. Raises MessagingError."""
        ...

    async def acknowledge(self, callback_id: str, notice: str = "") -> None:
        """Tell the channel a button press was received, with an optional short
        notice for whoever pressed. Never raises: it is a courtesy."""
        ...

    async def clear_buttons(self, address: str, channel_message_id: str, text: str) -> None:
        """Leave an already sent message without buttons, with `text` as its content.
        Never raises: a channel that cannot edit simply does nothing."""
        ...
