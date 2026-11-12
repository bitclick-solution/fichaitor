"""Leaving: the pure rules, the emails, the SMTP adapter, the revocation at Google and
the guard that keeps integration tests away from real data. No database, no network:
SMTP and HTTP are simulated transports."""

import asyncio
import logging
import smtplib
import ssl
from datetime import UTC, datetime

import httpx
import pytest

from app.adapters.google_profile.real import REVOKE_URL, RealGoogleProfile
from app.adapters.mailer.fake import PREFIX, FakeMailer
from app.adapters.mailer.smtp import SmtpMailer, build_message
from app.domain import offboarding as rules
from app.domain.offboarding_mail import code_email, restore_url, scheduled_email
from app.mail_config import MailSettings
from app.ports.google_profile import GoogleProfileError, GoogleRejected
from app.ports.mailer import Email, MailerError, MailRejected
from app.retention_config import RetentionSettings
from tests.conftest import database_name, guard_message, unsafe_test_urls

DELETE_AT = datetime(2026, 11, 14, 11, 16, tzinfo=UTC)
ADDRESS = "dueno@example.com"


# --- the guard of the integration tests ---------------------------------------


def test_the_guard_only_accepts_databases_whose_name_ends_in_test():
    safe = "postgresql://u:p@127.0.0.1:5433/fichaitor_test"
    assert database_name(safe) == "fichaitor_test"
    assert unsafe_test_urls({}) == []
    assert unsafe_test_urls({"FICHAITOR_TEST_OWNER_URL": safe}) == []
    env = {
        "FICHAITOR_TEST_DATABASE_URL": "postgresql://u:p@127.0.0.1:5433/fichaitor",
        "FICHAITOR_TEST_OWNER_URL": safe,
        "FICHAITOR_TEST_WORKER_URL": "postgresql://u:secreta@db/fichaitor_test_copia?x=_test",
    }
    assert unsafe_test_urls(env) == ["FICHAITOR_TEST_DATABASE_URL", "FICHAITOR_TEST_WORKER_URL"]
    assert unsafe_test_urls({"FICHAITOR_TEST_WORKER_URL": "postgresql://u:p@db"}) == [
        "FICHAITOR_TEST_WORKER_URL"
    ]
    message = guard_message(unsafe_test_urls(env))
    assert "FICHAITOR_TEST_DATABASE_URL" in message and "_test" in message
    assert "secreta" not in message  # names only: the URLs carry passwords


# --- codes, tokens and masks ---------------------------------------------------


def test_a_code_has_six_digits_and_its_fingerprint_ties_account_session_and_scope():
    codes = {rules.new_code() for _ in range(200)}
    assert all(len(c) == 6 and c.isdigit() for c in codes) and len(codes) > 150
    base = rules.code_hash("secreto", "cuenta-1", "sesion-1", "ficha", "123456")
    assert len(base) == 64 and "123456" not in base
    assert rules.same_hash(
        base, rules.code_hash("secreto", "cuenta-1", "sesion-1", "ficha", "123456")
    )
    for other in (
        rules.code_hash("otro", "cuenta-1", "sesion-1", "ficha", "123456"),
        rules.code_hash("secreto", "cuenta-2", "sesion-1", "ficha", "123456"),
        rules.code_hash("secreto", "cuenta-1", "sesion-2", "ficha", "123456"),
        rules.code_hash("secreto", "cuenta-1", "sesion-1", "cuenta", "123456"),
        rules.code_hash("secreto", "cuenta-1", "sesion-1", "ficha", "123457"),
    ):
        assert not rules.same_hash(base, other)


def test_the_restore_token_is_long_and_only_its_hash_is_kept():
    token = rules.new_restore_token()
    assert len(token) >= 40 and token != rules.new_restore_token()
    assert len(rules.restore_token_hash(token)) == 64 and token not in rules.restore_token_hash(
        token
    )


@pytest.mark.parametrize(
    ("email", "masked"),
    [
        ("dueno@example.com", "d***o@example.com"),
        ("ab@example.com", "a***@example.com"),
        ("a@example.com", "a***@example.com"),
        ("sin-arroba", "***"),
    ],
)
def test_the_email_is_masked(email, masked):
    assert rules.mask_email(email) == masked


def test_the_terms_cannot_be_configured_beyond_what_the_policy_promises():
    policy = RetentionSettings(_env_file=None).policy()
    assert (policy.google_data_days, policy.session_days, policy.removed_days) == (30, 30, 30)
    assert (policy.audit_months, policy.waitlist_months) == (12, 12)
    for field, value in (
        ("google_data_days", 31),
        ("session_days", 31),
        ("waitlist_months", 13),
        ("audit_months", 11),
    ):
        with pytest.raises(ValueError):
            RetentionSettings(_env_file=None, **{field: value})


# --- the emails ----------------------------------------------------------------


def test_the_restore_link_carries_the_token_in_the_fragment_never_in_the_query():
    url = restore_url("https://fichaitor.es/", "tok/en+1")
    assert url == "https://fichaitor.es/app#restaurar=tok%2Fen%2B1"
    assert "?" not in url


def test_the_code_email_says_what_for_when_it_expires_and_to_ignore_it():
    mail = code_email(ADDRESS, "cuenta", "042137", 10)
    assert mail.to == ADDRESS
    assert "042137" in mail.text and "042137" in mail.html
    assert "042137" not in mail.subject
    assert "borrar tu cuenta" in mail.text and "10 minutos" in mail.text
    assert "Si no lo has pedido tú, ignora este correo" in mail.text
    assert "desconectar tu ficha" in code_email(ADDRESS, "ficha", "042137", 10).text


def test_the_scheduled_email_gives_the_date_the_terms_and_the_link():
    url = restore_url("http://localhost:8080", "TOKEN")
    for scope, phrase in (("ficha", "desconectar tu ficha"), ("cuenta", "borrar tu cuenta")):
        mail = scheduled_email(ADDRESS, scope, DELETE_AT, url)
        assert phrase in mail.text
        assert "14 de noviembre de 2026" in mail.text
        assert url in mail.text and 'href="http://localhost:8080/app#restaurar=TOKEN"' in mail.html
        assert "30 días" in mail.text and "12 meses" in mail.text
        assert "TOKEN" not in mail.subject


# --- SMTP ----------------------------------------------------------------------


class FakeSmtp:
    """Stands in for smtplib.SMTP and SMTP_SSL: records the conversation."""

    instances: list["FakeSmtp"] = []
    extensions = ("starttls",)
    fail: Exception | None = None

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.steps: list[str] = []
        self.sent = []
        FakeSmtp.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.steps.append("quit")

    def ehlo(self):
        self.steps.append("ehlo")

    def has_extn(self, name):
        return name in self.extensions

    def starttls(self, context=None):
        self.steps.append("starttls")
        self.tls_context = context

    def login(self, user, password):
        self.steps.append(f"login:{user}")

    def send_message(self, message):
        if self.fail is not None:
            raise self.fail
        self.steps.append("send")
        self.sent.append(message)


@pytest.fixture
def smtp(monkeypatch):
    FakeSmtp.instances = []
    monkeypatch.setattr(FakeSmtp, "extensions", ("starttls",))
    monkeypatch.setattr(FakeSmtp, "fail", None)
    return FakeSmtp


def mailer(port=587, **options):
    return SmtpMailer(
        "smtp.example.test",
        port,
        sender="avisos@fichaitor.test",
        sender_name="Fichaitor",
        user="avisos@fichaitor.test",
        password="clave",
        smtp=FakeSmtp,
        smtp_ssl=FakeSmtp,
        **options,
    )


MAIL = Email(ADDRESS, "Asunto con eñe", "Texto plano\n", "<p>HTML</p>")


def test_the_message_has_both_bodies_and_the_headers_of_an_automatic_email():
    message = build_message(
        MAIL, sender="avisos@fichaitor.test", sender_name="Fichaitor", reply_to="soporte@f.test"
    )
    assert message["From"] == "Fichaitor <avisos@fichaitor.test>"
    assert message["To"] == ADDRESS and message["Reply-To"] == "soporte@f.test"
    assert message["Auto-Submitted"] == "auto-generated"
    assert message["Message-ID"].endswith("@fichaitor.test>") and message["Date"]
    kinds = [part.get_content_type() for part in message.iter_parts()]
    assert kinds == ["text/plain", "text/html"]
    without = build_message(MAIL, sender="avisos@fichaitor.test")
    assert without["Reply-To"] is None and without["From"] == "avisos@fichaitor.test"


def test_starttls_is_mandatory_and_the_certificate_is_verified(smtp):
    asyncio.run(mailer(reply_to="soporte@fichaitor.test").send(MAIL))
    (server,) = smtp.instances
    assert server.steps == [
        "ehlo",
        "starttls",
        "ehlo",
        "login:avisos@fichaitor.test",
        "send",
        "quit",
    ]
    assert server.tls_context.verify_mode == ssl.CERT_REQUIRED
    assert server.tls_context.check_hostname is True
    assert server.sent[0]["Reply-To"] == "soporte@fichaitor.test"

    smtp.instances = []
    smtp.extensions = ()
    with pytest.raises(MailRejected, match="no ofrece TLS"):
        asyncio.run(mailer().send(MAIL))
    assert "send" not in smtp.instances[0].steps and not any(
        s.startswith("login") for s in smtp.instances[0].steps
    )


def test_port_465_uses_tls_from_the_first_byte(smtp):
    asyncio.run(mailer(port=465).send(MAIL))
    (server,) = smtp.instances
    assert server.context.verify_mode == ssl.CERT_REQUIRED
    assert server.steps == ["login:avisos@fichaitor.test", "send", "quit"]


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (
            smtplib.SMTPRecipientsRefused({ADDRESS: (550, b"no such user dueno@example.com")}),
            MailRejected,
        ),
        (smtplib.SMTPDataError(554, b"rejected dueno@example.com"), MailRejected),
        (smtplib.SMTPDataError(451, b"try later dueno@example.com"), MailerError),
        (smtplib.SMTPServerDisconnected("bye dueno@example.com"), MailerError),
        (OSError("dueno@example.com unreachable"), MailerError),
        (ssl.SSLCertVerificationError("bad cert"), MailRejected),
    ],
)
def test_smtp_errors_are_sorted_and_never_quote_the_address(smtp, failure, expected):
    smtp.fail = failure
    with pytest.raises(MailerError) as caught:
        asyncio.run(mailer().send(MAIL))
    assert type(caught.value) is expected
    assert ADDRESS not in str(caught.value) and caught.value.__cause__ is None


def test_the_fake_mailer_writes_the_message_to_the_log_with_its_prefix(caplog):
    fake = FakeMailer()
    with caplog.at_level(logging.WARNING, logger="fichaitor.mail"):
        asyncio.run(fake.send(code_email(ADDRESS, "ficha", "042137", 10)))
    assert fake.sent and PREFIX in caplog.text and "042137" in caplog.text


def test_mail_is_available_only_with_a_server_or_in_fake_mode():
    assert not MailSettings(_env_file=None).available
    assert MailSettings(_env_file=None, fake_mail=True).available
    assert not MailSettings(_env_file=None, smtp_host="smtp.example.test").available
    assert MailSettings(_env_file=None, smtp_host="s", mail_from="a@b.test").available
    assert MailSettings(_env_file=None).mail_from_name == "Fichaitor"
    assert MailSettings(_env_file=None).public_base_url == "http://localhost:8080"


# --- revocation at Google --------------------------------------------------------

TOKEN = "1//refresh-token-secreto"


def google(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RealGoogleProfile("client-id", "client-secret", client=client)


def test_revocation_posts_the_token_in_the_body_to_googles_endpoint():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    asyncio.run(google(handler).revoke_token(TOKEN))
    (request,) = seen
    assert request.method == "POST" and str(request.url) == REVOKE_URL
    assert REVOKE_URL == "https://oauth2.googleapis.com/revoke"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert request.content.decode() == "token=1%2F%2Frefresh-token-secreto"
    assert TOKEN not in str(request.url)


def test_a_token_google_calls_invalid_counts_as_revoked():
    def handler(request):
        return httpx.Response(400, json={"error": "invalid_token", "error_description": TOKEN})

    asyncio.run(google(handler).revoke_token(TOKEN))  # no error


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (400, {"error": "invalid_request"}, GoogleRejected),
        (403, {}, GoogleRejected),
        (429, {}, GoogleProfileError),
        (503, {}, GoogleProfileError),
    ],
)
def test_revocation_failures_are_sorted_and_never_quote_the_token(status, body, expected):
    def handler(request):
        return httpx.Response(status, json={**body, "echo": TOKEN})

    with pytest.raises(GoogleProfileError) as caught:
        asyncio.run(google(handler).revoke_token(TOKEN))
    assert type(caught.value) is expected and TOKEN not in str(caught.value)


def test_a_network_failure_while_revoking_is_temporary_and_silent_about_the_token():
    def handler(request):
        raise httpx.ConnectError(f"cannot send {TOKEN}")

    with pytest.raises(GoogleProfileError) as caught:
        asyncio.run(google(handler).revoke_token(TOKEN))
    assert type(caught.value) is GoogleProfileError
    assert TOKEN not in str(caught.value) and caught.value.__cause__ is None
