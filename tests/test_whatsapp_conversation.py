"""The conversation with a WhatsApp address (docs/canales.md, "WhatsApp"): it only sees
the Pro+ profiles, every message of ours spends the bag of the account, a message that may
go out of the 24 hour window carries its template, and once the bag is used up the notices
go by Telegram or wait in the panel. Memory store and fake channels: no network."""

import asyncio
import itertools
from datetime import UTC, date, datetime, timedelta

from app.adapters.conversation_store.memory import MemoryConversationStore, MemoryLocation
from app.adapters.messaging.fake import FakeChannel
from app.adapters.messaging.offline import OfflineChannel
from app.application.conversation_service import (
    ActionOutcome,
    ConversationService,
    KeyDateInput,
    ReviewInput,
    ServiceConfig,
)
from app.domain import channel_messages as cm
from app.domain import message_allowance as allowance
from app.domain.channel_messages import InboundEvent, InboundKind
from app.domain.conversation import AccountInfo
from app.domain.plans import Plan
from app.domain.proposals import ProposalState as S
from app.ports.proposal_actions import InMemoryProposalActions

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)  # Thursday, 11:00 in Madrid
ACC = "acc-bar"
PLUS, PRO = "loc_plus", "loc_pro"  # a Pro+ profile and a Pro one of the same account
PHONE, CHAT = "+34600111222", "1001"
REPLY = "Muchas gracias por tu reseña. Nos alegra que disfrutaras el arroz."


class NoticeChannel(FakeChannel):
    """A fake WhatsApp: it cannot show the notice of a press."""

    shows_notices = False


class World:
    def __init__(self, pro_plus: int = 1, whatsapp=None) -> None:
        self.now = NOW
        self.store = MemoryConversationStore()
        self.store.accounts[ACC] = AccountInfo()
        plus_plan = Plan.PRO_WHATSAPP if pro_plus else Plan.PRO  # pro_plus=0: no Pro+ at all
        self.store.locations[PLUS] = MemoryLocation(ACC, "Bar Plus", plan=plus_plan)
        self.store.locations[PRO] = MemoryLocation(ACC, "Bar Pro", plan=Plan.PRO)
        for i in range(1, pro_plus):
            self.store.locations[f"loc_plus{i}"] = MemoryLocation(
                ACC, f"Otro Plus {i}", plan=Plan.PRO_WHATSAPP
            )
        self.telegram = FakeChannel("telegram")
        self.whatsapp = whatsapp if whatsapp is not None else NoticeChannel("whatsapp")
        self.actions = InMemoryProposalActions()
        self._ids = itertools.count(1)
        self.service = ConversationService(
            self.store,
            {"telegram": self.telegram, "whatsapp": self.whatsapp},
            self.actions,
            ServiceConfig(panel_url="https://panel.test/app", signup_url="https://alta.test"),
            clock=lambda: self.now,
        )

    def run(self, coro):
        return asyncio.run(coro)

    def event(self, kind, address, channel, **fields):
        return InboundEvent(channel, address, f"wamid.e{next(self._ids)}", kind, **fields)

    def link(self, address=PHONE, channel="whatsapp"):
        code = self.run(self.service.create_link_code(ACC))
        self.run(
            self.service.handle_inbound(self.event(InboundKind.TEXT, address, channel, text=code))
        )

    def say(self, text, address=PHONE, channel="whatsapp"):
        self.run(
            self.service.handle_inbound(self.event(InboundKind.TEXT, address, channel, text=text))
        )

    def press(self, button_id, address=PHONE, channel="whatsapp"):
        self.run(self.service.handle_inbound(
            self.event(InboundKind.BUTTON, address, channel, button_id=button_id)))  # fmt: skip

    def review(self, location=PLUS, review_id=None):
        review_id = review_id or f"rev_{next(self._ids):016x}"
        return self.run(
            self.service.submit_review(
                ACC, ReviewInput(review_id, 5, "El arroz, espectacular.", REPLY, location)
            )
        )

    def used(self):
        start, _ = allowance.month_window(self.now, self.run_zone())
        return self.store.allowance_used.get((allowance.scope(ACC), start), 0)

    @staticmethod
    def run_zone():
        from app.domain.conversation import DEFAULT_TIMEZONE, timezone_for

        return timezone_for(DEFAULT_TIMEZONE)

    def wa(self):
        return self.whatsapp.to(PHONE)


def proposals_sent(sent):
    return [m for m in sent if m.message.proposal_id and m.message.buttons]


def test_linking_by_whatsapp_welcomes_with_the_window_open_and_names_only_pro_plus():
    w = World()
    w.link()
    (welcome,) = w.wa()
    assert welcome.key == "welcome.linked" and "Bar Plus" in welcome.text
    assert "Bar Pro" not in welcome.text
    assert welcome.message.last_inbound_at == NOW  # the code opened the window
    assert w.used() == 1


def test_whatsapp_only_tells_of_the_pro_plus_profiles_the_rest_goes_by_telegram():
    w = World()
    w.link()
    w.link(CHAT, "telegram")
    plus, pro = w.review(PLUS), w.review(PRO)
    assert [m.message.proposal_id for m in proposals_sent(w.wa())] == [plus]
    assert {m.message.proposal_id for m in proposals_sent(w.telegram.to(CHAT))} == {plus, pro}


def test_a_press_from_whatsapp_on_a_profile_that_is_not_pro_plus_does_nothing():
    w = World()
    w.link()
    w.link(CHAT, "telegram")
    pro = w.review(PRO)
    sent = proposals_sent(w.telegram.to(CHAT))[-1]
    w.press(sent.button("Publicar").id)
    assert not w.actions.requests
    assert w.store.proposals[pro].state is S.SENT
    # WhatsApp cannot show a notice: it is sent as a message.
    assert w.wa()[-1].text == cm.render("stale.inactive")


def test_a_proposal_carries_its_template_with_the_ids_of_its_buttons():
    w = World()
    w.link()
    w.review(PLUS)
    proposal = proposals_sent(w.wa())[-1].message
    fallback = proposal.template_fallback
    assert fallback.name == "resena_nueva_v1"
    assert fallback.params == ("Bar Plus", "5 estrellas", "El arroz, espectacular.", REPLY)
    assert fallback.button_ids == tuple(b.id for b in proposal.buttons)


def test_out_of_the_window_a_holiday_and_its_reminder_carry_their_templates():
    w = World()
    w.link()
    w.now += timedelta(days=2)  # the window closed long ago
    day = date(2026, 12, 8)
    proposal_id = w.run(
        w.service.submit_key_date(
            ACC, KeyDateInput(f"holiday:{day}", "holiday", day, "Inmaculada Concepción", PLUS)
        )
    )
    ask = proposals_sent(w.wa())[-1].message
    assert ask.template_fallback.name == "festivo_v1"
    assert ask.last_inbound_at == NOW
    w.now = datetime(2026, 12, 7, 10, 0, tzinfo=UTC)  # reminder: one day before
    w.run(w.service.tick())
    reminder = w.wa()[-1].message
    assert reminder.key == "reminder.pending"
    assert reminder.template_fallback.name == "recordatorio_v1"
    (show,) = reminder.template_fallback.button_ids
    assert show.endswith(".show") and show.startswith(proposal_id)


def test_the_show_button_of_a_template_shows_the_step_again_and_confirms_nothing():
    w = World()
    w.link()
    proposal_id = w.review(PLUS)
    proposal = w.store.proposals[proposal_id]
    before = len(w.wa())
    w.press(cm.encode_button_id(proposal_id, proposal.step_token, cm.SHOW_OPTION))
    again = w.wa()[before:]
    assert len(again) == 1 and again[0].message.buttons
    assert again[0].message.last_inbound_at == w.now  # the press opened the window
    assert not w.actions.requests
    w.press(again[0].button("Publicar").id)
    assert len(w.actions.requests) == 1


def test_the_result_of_a_change_carries_the_result_template():
    w = World()
    w.link()
    proposal_id = w.review(PLUS)
    w.press(proposals_sent(w.wa())[-1].button("Publicar").id)
    w.now += timedelta(days=3)
    w.run(w.service.handle_action_result(proposal_id, ActionOutcome(ok=True)))
    result = w.wa()[-1].message
    assert result.key == "review.sent"
    assert result.template_fallback.name == "resultado_cambio_v1"
    assert result.template_fallback.params[0] == "Bar Plus"


def test_every_message_of_ours_spends_the_bag_and_none_goes_once_it_is_used_up():
    w = World()  # one Pro+ profile: 80 messages
    w.link()
    w.link(CHAT, "telegram")
    assert w.used() == 1
    w.store.allowance_used[(allowance.scope(ACC), allowance.month_window(NOW, w.run_zone())[0])] = (
        80
    )
    before = len(w.wa())
    proposal_id = w.review(PLUS)
    assert len(w.wa()) == before  # nothing by WhatsApp
    # The notice went by the next linked channel.
    assert proposals_sent(w.telegram.to(CHAT))[-1].message.proposal_id == proposal_id
    w.say("hola")  # nor an answer to the owner
    assert len(w.wa()) == before


def test_with_only_whatsapp_and_the_bag_used_up_the_proposal_waits_for_the_panel():
    w = World()
    w.link()
    w.store.allowance_used[(allowance.scope(ACC), allowance.month_window(NOW, w.run_zone())[0])] = (
        80
    )
    proposal_id = w.review(PLUS)
    assert w.store.proposals[proposal_id].state is S.SCHEDULED
    # Next month the bag is full again and the proposal goes out.
    w.now = datetime(2026, 12, 1, 9, 30, tzinfo=UTC)
    w.run(w.service.tick())
    assert proposals_sent(w.wa())[-1].message.proposal_id == proposal_id


def test_the_bag_is_80_per_pro_plus_profile_added_up():
    w = World(pro_plus=2)
    for _ in range(160):
        assert w.run(w.store.take_message_allowance(ACC, 1, NOW))
    assert not w.run(w.store.take_message_allowance(ACC, 1, NOW))
    assert not w.run(World(pro_plus=0).store.take_message_allowance(ACC, 1, NOW))


def test_without_pro_plus_a_whatsapp_number_gets_nothing():
    w = World(pro_plus=0)
    w.link()
    assert w.wa() == []  # not even the welcome: the bag is 0
    assert w.run(w.store.get_identity("whatsapp", PHONE)).account_id == ACC


def test_a_channel_that_is_not_configured_spends_nothing():
    w = World(whatsapp=OfflineChannel("whatsapp"))
    w.link()
    w.review(PLUS)
    assert w.used() == 0


def test_the_answer_to_an_unknown_number_is_not_of_any_bag_and_knows_the_window_is_open():
    w = World()
    w.say("hola", "+34699999999")
    (answer,) = w.whatsapp.to("+34699999999")
    assert answer.key == "unlinked.reply" and "WhatsApp" in answer.text
    assert answer.message.last_inbound_at == NOW
    assert w.used() == 0
