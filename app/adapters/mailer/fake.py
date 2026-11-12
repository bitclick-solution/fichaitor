"""Email that is not sent, for tests and local development (FICHAITOR_FAKE_MAIL=true).

The whole message, with its code or its restore link, is written to the log with a
clear prefix, so that the flow can be walked without a mail server. Never use it with
real users: the log would hold their codes.
"""

import logging

from app.ports.mailer import Email, MailerError

log = logging.getLogger("fichaitor.mail")
PREFIX = "[CORREO SIMULADO]"


class FakeMailer:
    def __init__(self, fail: type[MailerError] | None = None) -> None:
        self.sent: list[Email] = []
        self.fail = fail

    async def send(self, email: Email) -> None:
        if self.fail is not None:
            raise self.fail("fallo simulado")
        self.sent.append(email)
        log.warning("%s para %s | %s\n%s", PREFIX, email.to, email.subject, email.text)
