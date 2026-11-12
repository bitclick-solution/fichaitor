"""Settings of the channels. `_env_file=None`: the tests never read a real .env."""

from datetime import time

import pytest
from pydantic import ValidationError

from app.channels_config import ChannelsSettings

TOKEN = "123456789:AAFakeTokenForTests_abc-DEF"


def settings(**env):
    return ChannelsSettings(_env_file=None, **env)


def test_defaults(monkeypatch):
    for name in ("BOT_TOKEN", "BOT_USERNAME", "WEBHOOK_SECRET", "POLLING"):
        monkeypatch.delenv(f"FICHAITOR_TELEGRAM_{name}", raising=False)
    cfg = settings()
    assert cfg.telegram_bot_username == "FichaitorBot"
    assert not cfg.telegram_enabled and not cfg.telegram_polling
    assert cfg.telegram_webhook_secret.get_secret_value() == ""
    assert cfg.telegram_link("123456") == "https://t.me/FichaitorBot?start=123456"
    service = cfg.service_config()
    assert service.weekly_cap == 2
    assert (service.quiet.start, service.quiet.end) == (time(21, 0), time(9, 0))


def test_reads_the_agreed_variable_names(monkeypatch):
    monkeypatch.setenv("FICHAITOR_TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("FICHAITOR_TELEGRAM_BOT_USERNAME", "OtroBot")
    monkeypatch.setenv("FICHAITOR_TELEGRAM_WEBHOOK_SECRET", "s3cret_-OK")
    monkeypatch.setenv("FICHAITOR_TELEGRAM_POLLING", "true")
    cfg = settings()
    assert cfg.telegram_enabled and cfg.telegram_polling
    assert cfg.telegram_bot_token.get_secret_value() == TOKEN
    assert cfg.telegram_webhook_secret.get_secret_value() == "s3cret_-OK"
    assert cfg.telegram_link("000001") == "https://t.me/OtroBot?start=000001"


def test_secrets_do_not_show_in_a_repr(monkeypatch):
    monkeypatch.setenv("FICHAITOR_TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("FICHAITOR_TELEGRAM_WEBHOOK_SECRET", "s3cret_-OK")
    monkeypatch.setenv("FICHAITOR_LINK_CODE_PEPPER", "pimienta")
    shown = f"{settings()!r} {settings()}"
    assert TOKEN not in shown and "s3cret_-OK" not in shown and "pimienta" not in shown


@pytest.mark.parametrize("bad", ["con espacio", "ñ", "a/b", "x" * 257])
def test_webhook_secret_must_be_what_telegram_accepts(monkeypatch, bad):
    monkeypatch.setenv("FICHAITOR_TELEGRAM_WEBHOOK_SECRET", bad)
    with pytest.raises(ValidationError):
        settings()


def test_bot_username_shape(monkeypatch):
    monkeypatch.setenv("FICHAITOR_TELEGRAM_BOT_USERNAME", "con/barra")
    with pytest.raises(ValidationError):
        settings()


def test_a_badly_shaped_secret_is_not_printed_by_its_error(monkeypatch):
    monkeypatch.setenv("FICHAITOR_TELEGRAM_WEBHOOK_SECRET", "secreto con espacio")
    with pytest.raises(ValidationError) as caught:
        settings()
    assert "secreto con espacio" not in str(caught.value)
