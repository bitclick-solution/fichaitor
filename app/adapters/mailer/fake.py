"""Email that is not sent, for tests and local development (FICHAITOR_FAKE_MAIL=true).

The message is kept in `sent` (the tests read the code and the link from there) and
written to the log with a clear prefix, so that the flow can be followed without a mail
server. What is written is redacted (B13 of docs/seguridad/revision-2026-11-12-b.md):
the address is masked, every six digit code becomes "[código]" and the token of the
restore link "[oculto]". Even so, never use it with real users: production refuses it
(deploy/deploy.sh and deploy/docker-compose.prod.yml).
"""

import logging
import re

from app.domain.offboarding import mask_email
from app.domain.offboarding_mail import RESTORE_PARAM
from app.ports.mailer import Email, MailerError

log = logging.getLogger("fichaitor.mail")
PREFIX = "[CORREO SIMULADO]"

_CODE = re.compile(r"(?<![0-9])[0-9]{6}(?![0-9])")
_RESTORE = re.compile(rf"({RESTORE_PARAM}=)[^\s\"'<>]+")
# Anything that looks like a long random token, wherever it appears.
_TOKEN = re.compile(r"[A-Za-z0-9_%\-]{32,}")


def redact(text: str) -> str:
    """The text of a message without its codes, its restore token or long tokens."""
    text = _RESTORE.sub(r"\1[oculto]", text)
    text = _TOKEN.sub("[oculto]", text)
    return _CODE.sub("[código]", text)


class FakeMailer:
    def __init__(self, fail: type[MailerError] | None = None) -> None:
        self.sent: list[Email] = []
        self.fail = fail

    async def send(self, email: Email) -> None:
        if self.fail is not None:
            raise self.fail("fallo simulado")
        self.sent.append(email)
        log.warning(
            "%s para %s | %s\n%s",
            PREFIX,
            mask_email(email.to),
            redact(email.subject),
            redact(email.text),
        )
