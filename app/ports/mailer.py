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


# Extra headers a message may carry. Only these: the sender, Reply-To, Date and the rest
# are the adapter's, from its configuration.
EXTRA_HEADERS = frozenset({"List-Unsubscribe", "List-Unsubscribe-Post"})


@dataclass(frozen=True)
class Email:
    to: str
    subject: str
    text: str
    html: str
    attachments: tuple[Attachment, ...] = ()
    # (name, value) pairs: the opt-out headers of a commercial communication (RFC 8058).
    headers: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for name, value in self.headers:
            if name not in EXTRA_HEADERS:
                raise ValueError("header not allowed")
            if any(c in value for c in "\r\n\x00") or len(value) > 2000:
                raise ValueError("header value not valid")


class MailerError(Exception):
    """The message could not be handed over (network, server busy). Worth retrying.
    The message of the exception is a fixed string: never an address, a code or a link."""


class MailRejected(MailerError):
    """The server refused the message or the recipient for good. Never retried."""


class Mailer(Protocol):
    async def send(self, email: Email) -> None: ...
