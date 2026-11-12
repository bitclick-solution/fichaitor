"""Fake messaging channel for tests and local development: keeps what it is asked to
send and never talks to anybody."""

import itertools
from dataclasses import dataclass, field

from app.domain.channel_messages import Button, OutboundMessage
from app.ports.messaging import MessagingError, SentMessage


@dataclass(frozen=True)
class FakeSent:
    address: str
    message: OutboundMessage
    channel_message_id: str

    @property
    def text(self) -> str:
        return self.message.text

    @property
    def key(self) -> str:
        return self.message.key

    @property
    def titles(self) -> tuple[str, ...]:
        return tuple(button.title for button in self.message.buttons)

    def button(self, title: str) -> Button:
        return next(button for button in self.message.buttons if button.title == title)


@dataclass
class FakeChannel:
    name: str = "telegram"
    sent: list[FakeSent] = field(default_factory=list)
    notices: list[tuple[str, str]] = field(default_factory=list)  # (callback id, notice)
    cleared: dict[str, str] = field(default_factory=dict)  # message id -> text left
    fail_with: MessagingError | None = None  # set to make every send fail
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    async def send(self, address: str, message: OutboundMessage) -> SentMessage:
        if self.fail_with is not None:
            raise self.fail_with
        message_id = str(next(self._ids))
        self.sent.append(FakeSent(address, message, message_id))
        return SentMessage(message_id)

    async def acknowledge(self, callback_id: str, notice: str = "") -> None:
        self.notices.append((callback_id, notice))

    async def clear_buttons(self, address: str, channel_message_id: str, text: str) -> None:
        self.cleared[channel_message_id] = text

    def to(self, address: str) -> list[FakeSent]:
        return [sent for sent in self.sent if sent.address == address]
