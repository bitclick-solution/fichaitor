"""Outgoing email. The worker sends; the API never talks to a mail server."""

from dataclasses import dataclass, field
from typing import Protocol

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class Attachment:
    """A file sent with the message: the PDF of an invoice. Its bytes stay out of repr."""

    filename: str
    content: bytes = field(repr=False)
    content_type: str = "application/pdf"

    def __post_init__(self) -> None:
        maintype, _, subtype = self.content_type.partition("/")
        if not maintype or not subtype:
            raise ValueError("content_type must be type/subtype")
        if len(self.content) > MAX_ATTACHMENT_BYTES:
            raise ValueError("attachment too large")
        if not self.filename or any(c in self.filename for c in '/\\\r\n"'):
            raise ValueError("attachment filename not valid")


@dataclass(frozen=True)
class Email:
    to: str
    subject: str
    text: str
    html: str
    attachments: tuple[Attachment, ...] = ()


class MailerError(Exception):
    """The message could not be handed over (network, server busy). Worth retrying.
    The message of the exception is a fixed string: never an address, a code or a link."""


class MailRejected(MailerError):
    """The server refused the message or the recipient for good. Never retried."""


class Mailer(Protocol):
    async def send(self, email: Email) -> None: ...
