"""Email over SMTP, with the standard library.

smtplib is blocking, so each message is sent in a thread: one short conversation per
email is all the worker needs, and no new dependency comes with it.

TLS is mandatory and the certificate is verified: implicit TLS on port 465, STARTTLS on
any other port (the message is not sent if the server does not offer it). The server
comes from the configuration; none is written here.

Errors carry a fixed message: never the address, the subject or the body.

A message may carry files (the PDF of an invoice, docs/admin-facturacion.md, 4.3).
"""

import asyncio
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from app.ports.mailer import Email, MailerError, MailRejected

IMPLICIT_TLS_PORT = 465


def build_message(
    email: Email, *, sender: str, sender_name: str = "", reply_to: str = ""
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = formataddr((sender_name, sender)) if sender_name else sender
    message["To"] = email.to
    message["Subject"] = email.subject
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or None)
    # An automatic, transactional message: auto-responders must not answer it.
    message["Auto-Submitted"] = "auto-generated"
    if reply_to:
        message["Reply-To"] = reply_to
    # The opt-out headers of a commercial communication (List-Unsubscribe and
    # List-Unsubscribe-Post, RFC 8058). Email only lets those through.
    for name, value in email.headers:
        message[name] = value
    message.set_content(email.text)
    message.add_alternative(email.html, subtype="html")
    # With a file, the message becomes multipart/mixed: the text and the HTML stay
    # alternatives of each other, and the PDF goes beside them.
    for attachment in email.attachments:
        maintype, _, subtype = attachment.content_type.partition("/")
        message.add_attachment(
            attachment.content, maintype=maintype, subtype=subtype, filename=attachment.filename
        )
    return message


class SmtpMailer:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        sender: str,
        sender_name: str = "",
        reply_to: str = "",
        user: str = "",
        password: str = "",
        timeout: float = 20.0,
        smtp: Callable[..., smtplib.SMTP] = smtplib.SMTP,
        smtp_ssl: Callable[..., smtplib.SMTP] = smtplib.SMTP_SSL,
        context: ssl.SSLContext | None = None,
        plaintext_local: bool = False,
    ) -> None:
        self._host = host
        self._port = port
        self._sender = sender
        self._sender_name = sender_name
        self._reply_to = reply_to
        self._user = user
        self._password = password
        self._timeout = timeout
        self._smtp = smtp
        self._smtp_ssl = smtp_ssl
        # Verifies the certificate and the host name.
        self._context = context or ssl.create_default_context()
        # Development only (Mailpit): no STARTTLS. MailSettings allows it only for a
        # local host.
        self._plaintext_local = plaintext_local

    async def send(self, email: Email) -> None:
        message = build_message(
            email, sender=self._sender, sender_name=self._sender_name, reply_to=self._reply_to
        )
        await asyncio.to_thread(self._send, message)

    def _send(self, message: EmailMessage) -> None:
        try:
            if self._port == IMPLICIT_TLS_PORT:
                server = self._smtp_ssl(
                    self._host, self._port, timeout=self._timeout, context=self._context
                )
            else:
                server = self._smtp(self._host, self._port, timeout=self._timeout)
            with server:
                if self._port != IMPLICIT_TLS_PORT and not self._plaintext_local:
                    server.ehlo()
                    if not server.has_extn("starttls"):
                        raise MailRejected("el servidor de correo no ofrece TLS")
                    server.starttls(context=self._context)
                    server.ehlo()
                if self._user:
                    server.login(self._user, self._password)
                server.send_message(message)
        except MailerError:
            raise
        except ssl.SSLError:
            raise MailRejected("el certificado del servidor de correo no es válido") from None
        except smtplib.SMTPAuthenticationError:
            raise MailRejected("el servidor de correo rechazó las credenciales") from None
        except smtplib.SMTPRecipientsRefused:
            raise MailRejected("el servidor de correo rechazó al destinatario") from None
        except smtplib.SMTPResponseException as exc:
            # 5xx: refused for good. 4xx: try again later. Only the code is kept: the
            # server's text may quote the address.
            code = exc.smtp_code
            if 500 <= code < 600:
                raise MailRejected(f"el servidor de correo rechazó el mensaje ({code})") from None
            raise MailerError(f"el servidor de correo no aceptó el mensaje ({code})") from None
        except (smtplib.SMTPException, OSError):
            raise MailerError("sin respuesta del servidor de correo") from None
