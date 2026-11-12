"""Outgoing email. The worker sends; the API never talks to a mail server."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Email:
    to: str
    subject: str
    text: str
    html: str


class MailerError(Exception):
    """The message could not be handed over (network, server busy). Worth retrying.
    The message of the exception is a fixed string: never an address, a code or a link."""


class MailRejected(MailerError):
    """The server refused the message or the recipient for good. Never retried."""


class Mailer(Protocol):
    async def send(self, email: Email) -> None: ...
