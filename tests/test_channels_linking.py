"""Linking hardened (B3 of docs/seguridad/revision-2026-11-12.md), the commands of
Telegram and the pure pieces of the integration: masked addresses, job keys and slots.
Everything in memory; Telegram is the fake channel."""

import asyncio
import itertools
from datetime import UTC, datetime, timedelta

import pytest

from app.adapters.conversation_store.memory import MemoryConversationStore
from app.adapters.messaging.fake import FakeChannel
from app.adapters.messaging.telegram import parse_update
from app.application.conversation_service import ConversationService, ServiceConfig
from app.domain import channel_jobs
from app.domain import link_codes as lc
from app.domain.channel_messages import COMMAND_HELP, COMMAND_PRIVACY, InboundEvent, InboundKind
from app.domain.conversation import AccountInfo, masked_address, timezone_for
from app.ports.proposal_actions import InMemoryProposalActions

START = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
OWNER, STRANGER = "1001", "9999"
ACC = "acc-bar"
PEPPER = "pimienta"


class World:
    def __init__(self, global_max: int = 50) -> None:
        self.now = START
        self.store = MemoryConversationStore()
        self.store.accounts[ACC] = AccountInfo(business_name="Bar Pepe")
        self.telegram = FakeChannel("telegram")
        self._ids = itertools.count(1)
        self.service = ConversationService(
            self.store,
            {"telegram": self.telegram},
            InMemoryProposalActions(),
            ServiceConfig(
                panel_url="https://panel.test/app",
                signup_url="https://alta.test",
                link_code_pepper=PEPPER,
                privacy_url="https://fichaitor.test/privacidad/",
                link_global_max_failures=global_max,
            ),
            clock=lambda: self.now,
        )

    def run(self, coro):
        return asyncio.run(coro)

    def event(self, kind, address, text="", name="Marta"):
        return InboundEvent(
            "telegram", address, f"e{next(self._ids)}", kind, text, sender_name=name
        )

    def start(self, payload, address=OWNER):
        self.run(self.service.handle_inbound(self.event(InboundKind.START, address, payload)))

    def say(self, text, address=OWNER):
        self.run(self.service.handle_inbound(self.event(InboundKind.TEXT, address, text)))

    def command(self, name, address=OWNER):
        self.run(self.service.handle_inbound(self.event(InboundKind.COMMAND, address, name)))

    def keys(self, address=OWNER):
        return [m.key for m in self.telegram.to(address)]

    def linked(self, address=OWNER):
        return self.run(self.store.get_identity("telegram", address))


# --- the token of the link -----------------------------------------------------------


def test_the_link_carries_a_long_token_that_links_and_spends_the_code():
    w = World()
    issued = w.run(w.service.create_link(ACC))
    assert lc.is_token_shaped(issued.token) and len(issued.token) == 43
    assert lc.is_code_shaped(issued.code)
    assert issued.record.token_hash == lc.code_hash(issued.token, PEPPER)
    w.start(issued.token)
    assert w.keys() == ["welcome.linked"]
    assert w.linked().account_id == ACC
    # Same code: the six digits no longer work once the token was used.
    w.say(issued.code, STRANGER)
    assert w.keys(STRANGER) == ["link.code_invalid"]
    # Neither the token nor the digits are in the record of messages.
    assert all(issued.token not in m.body and issued.code not in m.body for m in w.store.messages)


def test_the_six_digits_spend_the_token_too():
    w = World()
    issued = w.run(w.service.create_link(ACC))
    w.say(issued.code)
    assert w.keys() == ["welcome.linked"]
    w.start(issued.token, STRANGER)
    assert w.keys(STRANGER) == ["link.code_invalid"]


def test_a_token_pasted_as_text_also_links():
    w = World()
    issued = w.run(w.service.create_link(ACC))
    w.say(issued.token)
    assert w.keys() == ["welcome.linked"]


def test_a_new_code_voids_the_token_of_the_previous_one():
    w = World()
    first = w.run(w.service.create_link(ACC))
    w.run(w.service.create_link(ACC))
    w.start(first.token)
    assert w.keys() == ["link.code_invalid"]


# --- counts: per chat and of every chat together -------------------------------------


def test_the_attempt_is_counted_before_the_code_is_looked_at():
    state = lc.SenderState()
    for expected in (1, 2):
        state = lc.register_attempt(state, START)
        assert state.failed_attempts == expected and not lc.is_blocked(state, START)
    state = lc.register_attempt(state, START)
    assert lc.is_blocked(state, START)
    assert lc.register_attempt(state, START + timedelta(minutes=59)) is None
    again = lc.register_attempt(state, START + timedelta(hours=1))
    assert again.failed_attempts == 1 and again.blocked_until is None


def test_a_right_code_on_the_third_attempt_still_links_and_lifts_the_count():
    w = World()
    good = w.run(w.service.create_link(ACC))
    w.say("000000" if good.code != "000000" else "111111")
    w.say("000001" if good.code != "000001" else "111112")
    w.start(good.token)
    assert w.keys()[-1] == "welcome.linked"
    state = w.run(w.store.get_sender_state(lc.address_hash("telegram", OWNER)))
    assert state.failed_attempts == 0 and state.blocked_until is None


def test_every_wrong_code_counts_in_the_global_window():
    w = World()
    for n in range(4):
        w.say(f"{n:06d}", f"70{n}")
    assert w.run(w.store.link_failures(w.now)) == 4
    w.now += lc.GLOBAL_FAILURE_WINDOW
    assert w.run(w.store.link_failures(w.now)) == 0


def test_past_the_global_limit_typed_codes_wait_and_the_link_still_works():
    w = World(global_max=3)
    good = w.run(w.service.create_link(ACC))
    for n in range(3):  # three different chats, one wrong code each
        w.say("999999" if good.code != "999999" else "888888", f"80{n}")
    # A fourth chat types the right code: not even looked at.
    w.say(good.code, STRANGER)
    assert w.keys(STRANGER) == ["link.paused"]
    assert "https://panel.test/app" in w.telegram.to(STRANGER)[-1].text
    assert w.linked(STRANGER) is None
    # The token of the link cannot be guessed: it still works.
    w.start(good.token, OWNER)
    assert w.keys() == ["welcome.linked"]
    # When the window ends, typed codes are looked at again.
    w.now += lc.GLOBAL_FAILURE_WINDOW
    fresh = w.run(w.service.create_link(ACC))
    w.say(fresh.code, STRANGER)
    assert w.keys(STRANGER)[-1] == "welcome.linked"


def test_a_blocked_chat_gets_nothing_and_its_codes_are_not_counted_globally():
    w = World()
    for _ in range(3):
        w.say("123123")
    assert w.keys() == ["link.code_invalid", "link.code_invalid", "link.blocked"]
    before = w.run(w.store.link_failures(w.now))
    w.say("123123")
    w.command(COMMAND_HELP)
    assert len(w.telegram.to(OWNER)) == 3
    assert w.run(w.store.link_failures(w.now)) == before


# --- commands ------------------------------------------------------------------------


def test_ayuda_and_privacidad_answer_a_chat_that_is_not_linked_without_any_data():
    w = World()
    w.command(COMMAND_HELP, STRANGER)
    w.command(COMMAND_PRIVACY, STRANGER)
    assert w.keys(STRANGER) == ["command.help", "command.privacy"]
    help_text, privacy = (m.text for m in w.telegram.to(STRANGER))
    assert "https://panel.test/app" in help_text and "/privacidad" in help_text
    assert "90 días" in privacy and "https://fichaitor.test/privacidad/" in privacy
    assert "Bar Pepe" not in help_text + privacy
    # Commands do not use up the once-a-day generic answer.
    w.say("hola", STRANGER)
    assert w.keys(STRANGER)[-1] == "unlinked.reply"
    assert not w.store.messages  # nothing of an unknown chat is stored


def test_a_command_of_a_linked_chat_is_recorded_and_moves_no_proposal():
    w = World()
    w.start(w.run(w.service.create_link(ACC)).token)
    w.command(COMMAND_PRIVACY)
    assert w.keys()[-1] == "command.privacy"
    assert [m.body for m in w.store.messages if m.direction == "in"][-1] == "/privacidad"


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ("/ayuda", InboundKind.COMMAND, "ayuda"),
        ("/Ayuda@FichaitorBot", InboundKind.COMMAND, "ayuda"),
        ("/privacidad por favor", InboundKind.COMMAND, "privacidad"),
        ("/help", InboundKind.TEXT, "/help"),
        ("/borrar todo", InboundKind.TEXT, "/borrar todo"),
        ("/start", InboundKind.START, ""),
    ],
)
def test_telegram_knows_only_start_ayuda_and_privacidad(text, kind, value):
    update = {
        "update_id": 1,
        "message": {
            "from": {"id": 1001, "is_bot": False, "first_name": "Marta"},
            "chat": {"id": 1001, "type": "private"},
            "text": text,
        },
    }
    event = parse_update(update)
    assert event.kind is kind and event.text == value


# --- pure pieces ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("channel", "address", "shown"),
    [
        ("telegram", "123457342", "Telegram de ••••7342"),
        ("telegram", "-1001234", "Telegram de ••••1234"),
        ("telegram", "42", "Telegram de ••••42"),
        ("whatsapp", "+34600112233", "WhatsApp de ••••2233"),
        ("email", "marta@bar.es", "Correo de m••••@bar.es"),
    ],
)
def test_masked_address(channel, address, shown):
    assert masked_address(channel, address) == shown


def test_job_keys_and_slots():
    madrid = timezone_for("Europe/Madrid")
    noon = datetime(2026, 11, 12, 11, 0, tzinfo=UTC)  # 12:00 in Madrid
    assert channel_jobs.tick_key(noon) == channel_jobs.tick_key(noon + timedelta(minutes=4))
    assert channel_jobs.tick_key(noon) != channel_jobs.tick_key(noon + timedelta(minutes=5))
    day = channel_jobs.sync_slot(noon, madrid)
    assert day.startswith("d")
    assert channel_jobs.sync_slot(noon + timedelta(minutes=29), madrid) == day
    assert channel_jobs.sync_slot(noon + timedelta(minutes=30), madrid) != day
    night = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)  # 00:00 in Madrid
    assert channel_jobs.sync_slot(night, madrid).startswith("n")
    # 8:00 in Madrid is still night; 8:30 is day.
    assert channel_jobs.sync_slot(datetime(2026, 11, 12, 7, 0, tzinfo=UTC), madrid)[0] == "n"
    assert channel_jobs.sync_slot(datetime(2026, 11, 12, 7, 30, tzinfo=UTC), madrid)[0] == "d"
    key = channel_jobs.sync_key("sub", "locations/1", day)
    assert key == f"sync:sub:locations/1:{day}"
    assert len(channel_jobs.sync_key("s" * 255, "locations/1", day)) <= 200
    assert channel_jobs.result_error("review_gone") == "target_gone"
    assert channel_jobs.result_error("reconnect") == "reconnect"
    assert channel_jobs.result_error("suspended") == "failed"
    assert channel_jobs.result_error(None) is None
