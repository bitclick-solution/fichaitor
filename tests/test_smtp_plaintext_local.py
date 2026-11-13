"""Plain SMTP to Mailpit: development only, and only with a local host."""

import pytest
from pydantic import ValidationError

from app.mail_config import MailSettings


def settings(**values):
    return MailSettings(_env_file=None, **values)


def test_plaintext_is_accepted_only_for_a_local_catcher():
    for host in ("mailpit", "localhost", "127.0.0.1"):
        assert settings(smtp_host=host, smtp_plaintext_local=True).smtp_plaintext_local


@pytest.mark.parametrize("host", ["smtp.ionos.es", "mail.example.com", ""])
def test_plaintext_is_refused_for_any_other_host(host):
    with pytest.raises(ValidationError):
        settings(smtp_host=host, smtp_plaintext_local=True)


def test_plaintext_is_off_by_default():
    assert settings(smtp_host="smtp.ionos.es").smtp_plaintext_local is False
