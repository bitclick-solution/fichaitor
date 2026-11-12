"""Step 6b of docs/multinegocio.md: the conversation with several active businesses in
one chat, end to end, with the store in memory and with PostgreSQL (the role of the
conversation, against the TEST database only; skipped without its four URLs).

What it covers:
- one lane per business: two proposals open at once, each with its header, and a
  review of 1 star of one business that does not interrupt the other;
- free text with several proposals open: quoting a message, the focus of a button, a
  single candidate, and the "sí" that is ambiguous and asks "¿De qué negocio?";
- a chat limited to one business: it gets nothing of the other and cannot move it;
- deactivating a business with its proposal open: cancelled without a word, its buttons
  say "stale.inactive", and the other business goes on (without a header once alone);
- with a single active business, the messages are exactly those of always;
- the weekly cap per business, and the results of actions only to who sees them.
"""

import asyncio
import itertools
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import asyncpg
import pytest

from app.adapters.conversation_store.memory import MemoryConversationStore, MemoryLocation
from app.adapters.conversation_store.postgres import PostgresConversationStore, insert_link_code
from app.adapters.messaging.fake import FakeChannel
from app.application.conversation_service import (
    ActionOutcome,
    ConversationService,
    KeyDateInput,
    ReviewInput,
    ServiceConfig,
)
from app.domain import channel_messages as cm
from app.domain import link_codes
from app.domain.channel_messages import InboundEvent, InboundKind
from app.domain.conversation import AccountInfo
from app.domain.proposals import OUTCOME_LOCATION_INACTIVE
from app.domain.proposals import ProposalState as S
from app.ports.proposal_actions import InMemoryProposalActions

OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")
CONVERSATION_URL = os.environ.get("FICHAITOR_TEST_CONVERSATION_URL")
HAS_DB = bool(
    os.environ.get("FICHAITOR_TEST_DATABASE_URL")
    and OWNER_URL
    and os.environ.get("FICHAITOR_TEST_WORKER_URL")
    and CONVERSATION_URL
)
STORES = [
    "memory",
    pytest.param(
        "postgres",
        marks=pytest.mark.skipif(not HAS_DB, reason="sin base de datos de pruebas configurada"),
    ),
]

START = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)  # Thursday, 11:00 in Madrid
PREFIX = "pytest-mn6b-"
REPLY = "Muchas gracias por tu reseña. Nos alegra que disfrutaras."
TITLE_A, TITLE_B = "Bar La Parra", "Taberna Sur"


def run(coro):
    return asyncio.run(coro)


# --- the stage, in memory or in PostgreSQL ----------------------------------------------


class Stage:
    """One account with two active businesses (A and B), the conversation and a fake
    Telegram. `store` is the memory or the PostgreSQL one; what differs between them
    (activating, filtering a chat) goes through the methods of the subclass."""

    def __init__(self, store, account: str, a: str, b: str, *, weekly_cap: int = 2):
        self.store = store
        self.account, self.a, self.b = account, a, b
        self.now = START
        self.telegram = FakeChannel("telegram")
        self.actions = InMemoryProposalActions()
        self._events = itertools.count(1)
        self._reviews = itertools.count(1)
        self.addresses: list[str] = []
        self.service = ConversationService(
            store,
            {"telegram": self.telegram},
            self.actions,
            ServiceConfig(
                panel_url="https://panel.test/app",
                signup_url="https://alta.test",
                weekly_cap=weekly_cap,
            ),
            clock=lambda: self.now,
        )

    # what differs between the stores
    async def deactivate(self, location: str) -> None:
        raise NotImplementedError

    async def set_filter(self, address: str, locations: list[str] | None) -> None:
        raise NotImplementedError

    # driving
    def new_address(self) -> str:
        address = str(uuid.uuid4().int % 10**11 + 10**11)
        self.addresses.append(address)
        return address

    def _event(self, kind, address, **fields) -> InboundEvent:
        return InboundEvent("telegram", address, f"{PREFIX}{next(self._events)}", kind, **fields)

    async def new_code(self, locations: list[str] | None = None) -> str:
        if locations is None:
            return await self.service.create_link_code(self.account)
        # A code asked for with a filter, as the API stores it (step 7).
        issued = link_codes.issue_link(self.account, self.now, "", tuple(locations))
        await self.store.save_link_code(issued.record)
        return issued.code

    async def link(self, locations: list[str] | None = None) -> str:
        address = self.new_address()
        code = await self.new_code(locations)
        await self.service.handle_inbound(
            self._event(InboundKind.START, address, text=code, sender_name="Marta")
        )
        return address

    async def say(self, address: str, text: str, reply_to: str = "") -> None:
        await self.service.handle_inbound(
            self._event(InboundKind.TEXT, address, text=text, reply_to=reply_to)
        )

    async def press(self, address: str, sent, title: str) -> None:
        await self.press_id(address, sent.button(title).id, sent.channel_message_id)

    async def press_id(self, address: str, button_id: str, ref: str = "") -> None:
        await self.service.handle_inbound(
            self._event(
                InboundKind.BUTTON,
                address,
                button_id=button_id,
                callback_id=f"cb{next(self._events)}",
                message_ref=ref,
            )
        )

    async def review(self, location: str, stars: int = 5, text: str = "Todo perfecto.") -> str:
        review_id = f"rev_{uuid.uuid4().hex[:16]}"
        return await self.service.submit_review(
            self.account, ReviewInput(review_id, stars, text, REPLY, location)
        )

    async def holiday(self, location: str, day: date = date(2026, 12, 8)) -> str | None:
        key = KeyDateInput(f"{location}:{day}", "holiday", day, "Inmaculada", location)
        return await self.service.submit_key_date(self.account, key)

    async def commercial(self, location: str, day: date) -> str | None:
        key = KeyDateInput(f"{location}:{day}", "commercial", day, "Black Friday", location)
        return await self.service.submit_key_date(self.account, key)

    # looking
    def sent(self, address: str):
        return self.telegram.to(address)

    def last(self, address: str):
        return self.sent(address)[-1]

    def about(self, address: str, proposal_id: str):
        """The last message with buttons of that proposal sent to that address."""
        found = [
            m
            for m in self.sent(address)
            if m.message.buttons and m.message.proposal_id == proposal_id
        ]
        return found[-1]

    def notice(self) -> str:
        return self.telegram.notices[-1][1]

    async def proposal(self, proposal_id: str):
        return await self.store.get_proposal(proposal_id)

    async def lane(self, location: str):
        return (await self.store.get_lane(self.account, location)).active_proposal_id


class MemoryStage(Stage):
    @classmethod
    def build(cls, **kwargs) -> "MemoryStage":
        store = MemoryConversationStore()
        account = "acc-varias"
        store.accounts[account] = AccountInfo(business_name=TITLE_A)
        store.locations["loc_parra"] = MemoryLocation(account, TITLE_A)
        store.locations["loc_sur"] = MemoryLocation(account, TITLE_B)
        return cls(store, account, "loc_parra", "loc_sur", **kwargs)

    async def deactivate(self, location: str) -> None:
        self.store.locations[location] = replace(self.store.locations[location], active=False)

    async def set_filter(self, address: str, locations: list[str] | None) -> None:
        identity = await self.store.get_identity("telegram", address)
        self.store.identities[identity.id] = replace(
            identity, location_filter=None if locations is None else tuple(locations)
        )


async def owner(sql: str, *args):
    conn = await asyncpg.connect(OWNER_URL)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


class PostgresStage(Stage):
    async def new_code(self, locations: list[str] | None = None) -> str:
        # The API writes the codes (the conversation only reads and spends them).
        location_filter = tuple(locations) if locations is not None else None
        issued = link_codes.issue_link(self.account, self.now, "", location_filter)
        conn = await asyncpg.connect(OWNER_URL)
        try:
            await insert_link_code(conn, issued.record)
        finally:
            await conn.close()
        return issued.code

    async def deactivate(self, location: str) -> None:
        # As the API does it (step 3) or the worker by removing it; the conversation
        # finds out on its own (lazy cleanup).
        await owner("UPDATE location SET active_since = NULL, plan = NULL WHERE id = $1", location)

    async def set_filter(self, address: str, locations: list[str] | None) -> None:
        await owner(
            "UPDATE channel_identity SET location_filter = $2"
            " WHERE channel = 'telegram' AND address = $1 AND unlinked_at IS NULL",
            address,
            locations,
        )


async def pg_cleanup(addresses: list[str]) -> None:
    mine = f"(SELECT id FROM account WHERE google_sub LIKE '{PREFIX}%')"
    hashes = [link_codes.address_hash("telegram", a) for a in addresses]
    for sql in (
        f"DELETE FROM channel_message WHERE conversation_id IN"
        f" (SELECT id FROM conversation WHERE account_id IN {mine})",
        f"DELETE FROM conversation_lane WHERE account_id IN {mine}",
        f"DELETE FROM conversation WHERE account_id IN {mine}",
        f"DELETE FROM proposal WHERE account_id IN {mine}",
        f"DELETE FROM channel_message WHERE identity_id IN"
        f" (SELECT id FROM channel_identity WHERE account_id IN {mine})",
        f"DELETE FROM channel_identity WHERE account_id IN {mine}",
        f"DELETE FROM link_code WHERE account_id IN {mine}",
        f"DELETE FROM channel_inbound_event WHERE event_id LIKE '{PREFIX}%'",
        "DELETE FROM channel_sender WHERE address_hash = ANY($1::text[])",
        f"UPDATE account SET selected_location_id = NULL WHERE id IN {mine}",
        f"DELETE FROM location WHERE account_id IN {mine}",
        f"DELETE FROM location_activation_log WHERE account_id IN {mine}",
        f"DELETE FROM account WHERE id IN {mine}",
    ):
        await owner(sql, *((hashes,) if "$1" in sql else ()))


@asynccontextmanager
async def stage(kind: str, **kwargs):
    if kind == "memory":
        yield MemoryStage.build(**kwargs)
        return
    sub = f"{PREFIX}{uuid.uuid4().hex[:10]}"
    account = (
        await owner(
            "INSERT INTO account (google_sub, email) VALUES ($1, 'x@x') RETURNING id::text", sub
        )
    )[0]["id"]
    locations = []
    for title in (TITLE_A, TITLE_B):
        rows = await owner(
            "INSERT INTO location (account_id, gbp_account, gbp_location, title)"
            " VALUES ($1::uuid, 'accounts/1', $2, $3) RETURNING id",
            account,
            f"locations/{PREFIX}{uuid.uuid4().hex[:12]}",
            title,
        )
        locations.append(rows[0]["id"])
        await owner(
            "UPDATE location SET active_since = now(), plan = 'pro' WHERE id = $1", rows[0]["id"]
        )
    pool = await asyncpg.create_pool(CONVERSATION_URL, min_size=1, max_size=3)
    staged = PostgresStage(PostgresConversationStore(pool), account, *locations, **kwargs)
    try:
        yield staged
    finally:
        await pool.close()
        await pg_cleanup(staged.addresses)


@pytest.fixture(autouse=True)
def _clean_database():
    if HAS_DB:
        run(pg_cleanup([]))
    yield


# =============================================================================
# Step 7: the filter chosen with the code.
# =============================================================================


@pytest.mark.parametrize("kind", STORES)
def test_a_code_with_a_filter_links_a_chat_that_never_gets_the_other_business(kind):
    async def go():
        async with stage(kind) as s:
            manager = await s.link([s.b])
            identity = await s.store.get_identity("telegram", manager)
            assert identity.location_filter == (s.b,)
            # From the first message: the welcome names only its business.
            welcome = s.last(manager)
            assert welcome.key == "welcome.linked"
            assert TITLE_B in welcome.text and TITLE_A not in welcome.text
            owner_chat = await s.link()
            assert (await s.store.get_identity("telegram", owner_chat)).location_filter is None

            pa = await s.review(s.a, 5, "El arroz, espectacular.")
            pb = await s.review(s.b, 4, "Muy buena atención.")
            assert s.about(owner_chat, pa) and s.about(owner_chat, pb)
            assert s.about(manager, pb).text.startswith(f"{TITLE_B}\n")
            assert all(m.message.proposal_id != pa for m in s.sent(manager))
            assert all(TITLE_A not in m.text for m in s.sent(manager))

    run(go())


# =============================================================================
# The walk through of the brief: two businesses, one chat.
# =============================================================================


@pytest.mark.parametrize("kind", STORES)
def test_two_businesses_in_one_chat_end_to_end(kind):
    async def go():
        async with stage(kind) as s:
            chat = await s.link()
            welcome = s.last(chat)
            assert welcome.key == "welcome.linked_many"
            assert "tus 2 negocios" in welcome.text

            # Two new reviews, one of each business: both open at once, each with its
            # header (the line of the business, then the message of always).
            pa = await s.review(s.a, 5, "El arroz, espectacular.")
            pb = await s.review(s.b, 4, "Muy buena atención.")
            to_a, to_b = s.about(chat, pa), s.about(chat, pb)
            assert to_a.text.split("\n")[0] == TITLE_A
            assert to_b.text.split("\n")[0] == TITLE_B
            assert to_a.text.split("\n", 1)[1].startswith("Nueva reseña, 5 estrellas")
            assert (await s.lane(s.a), await s.lane(s.b)) == (pa, pb)

            # A reply quoting the message of B is about B, whatever is open in A.
            await s.say(chat, "Gracias, Ana. Vuelve pronto.", reply_to=to_b.channel_message_id)
            assert s.last(chat).key == "review.clarify_text"
            assert s.last(chat).text.startswith(f"{TITLE_B}\n")
            assert (await s.proposal(pb)).step == "review.clarify_text"
            assert (await s.proposal(pa)).step == "review.ask"

            # A "sí" that could be about either: nothing is confirmed, it asks which.
            await s.say(chat, "sí")
            which = s.last(chat)
            assert which.key == "text.which"
            assert which.text == "¿De qué negocio me hablas?"
            assert which.titles == (TITLE_A, TITLE_B)
            assert s.actions.requests == []

            # The owner picks A: its question is shown again, and the next "sí" is A's.
            await s.press(chat, which, TITLE_A)
            again = s.last(chat)
            assert again.text.startswith(f"{TITLE_A}\nEscríbelo otra vez, por favor.\n")
            await s.say(chat, "sí")
            (request,) = s.actions.requests
            assert request.proposal_id == pa and request.location_id == s.a
            assert request.payload["text"] == REPLY
            assert (await s.proposal(pa)).state is S.EXECUTING
            assert (await s.proposal(pb)).state is S.TO_CONFIRM

            # A chat limited to B: it sees nothing of A and cannot move it.
            manager = await s.link()
            await s.set_filter(manager, [s.b])
            before = len(s.sent(manager))
            await s.service.handle_action_result(pa, ActionOutcome(True, link="https://g.co/r"))
            assert s.last(chat).key == "review.sent"
            assert s.last(chat).text.startswith(f"{TITLE_A}\n")
            assert len(s.sent(manager)) == before  # the result of A is not told to B's chat
            await s.press_id(manager, to_a.message.buttons[0].id)
            assert s.notice() == cm.render("stale.inactive")
            a_again = await s.review(s.a, 5, "Volveremos.")
            assert len(s.sent(manager)) == before
            assert s.about(chat, a_again).text.startswith(f"{TITLE_A}\n")
            # Its "sí" can only be about B (the only proposal it sees): decision 6, it acts.
            await s.say(manager, "sí")
            assert [r.proposal_id for r in s.actions.requests] == [pa, pb]
            assert s.actions.requests[-1].payload["text"] == "Gracias, Ana. Vuelve pronto."

            # B is deactivated with a proposal open: cancelled without a word, its buttons
            # no longer work, and A goes on, without a header now that it is alone.
            holiday_b = await s.holiday(s.b)
            holiday_msg = s.about(chat, holiday_b)
            assert holiday_msg.text.startswith(f"{TITLE_B}\n")
            sent_before = len(s.telegram.sent)
            await s.deactivate(s.b)
            await s.service.tick()
            cancelled = await s.proposal(holiday_b)
            assert cancelled.state is S.CANCELLED
            assert cancelled.outcome_reason == OUTCOME_LOCATION_INACTIVE
            assert await s.lane(s.b) is None
            assert len(s.telegram.sent) == sent_before
            await s.press(chat, holiday_msg, "Cierro")
            assert s.notice() == cm.render("stale.inactive")
            assert len(s.actions.requests) == 2

            await s.say(chat, "no", reply_to=s.about(chat, a_again).channel_message_id)
            assert s.last(chat).text == cm.render("review.skipped")  # alone: no header
            last_review = await s.review(s.a, 5, "Perfecto.")
            plain = s.about(chat, last_review)
            assert not plain.text.startswith(TITLE_A)
            assert plain.text.startswith("Nueva reseña, 5 estrellas")

    run(go())


# =============================================================================
# Lanes, text and filter, case by case.
# =============================================================================


@pytest.mark.parametrize("kind", STORES)
def test_a_one_star_review_of_a_does_not_interrupt_b(kind):
    async def go():
        async with stage(kind) as s:
            chat = await s.link()
            holiday_b = await s.holiday(s.b)
            review_a = await s.review(s.a, 4)
            low_a = await s.review(s.a, 1, "Fatal.")
            # B keeps its question; in A the 1 star review takes the place of the other.
            assert await s.lane(s.b) == holiday_b
            assert await s.lane(s.a) == low_a
            assert (await s.proposal(review_a)).parked_at is not None
            assert (await s.proposal(holiday_b)).parked_at is None
            assert s.about(chat, low_a).text.startswith(f"{TITLE_A}\nReseña de 1 estrella")

    run(go())


@pytest.mark.parametrize("kind", STORES)
def test_the_focus_of_a_button_decides_the_next_text(kind):
    async def go():
        async with stage(kind) as s:
            chat = await s.link()
            pa = await s.review(s.a)
            await s.review(s.b)
            # "Editar" on A, then a text: the text is A's reply, though B is open too.
            await s.press(chat, s.about(chat, pa), "Editar")
            assert s.last(chat).text == f"{TITLE_A}\n{cm.render('review.edit_ask')}"
            await s.say(chat, "Gracias por venir")
            assert s.last(chat).key == "review.edit_confirm"
            assert s.last(chat).text.startswith(f"{TITLE_A}\n")
            assert (await s.proposal(pa)).slots["text"] == "Gracias por venir"
            # Thirty minutes later the focus no longer decides: it asks.
            s.now += timedelta(minutes=31)
            await s.say(chat, "vale")
            assert s.last(chat).key == "text.which"

    run(go())


def test_hours_go_to_the_only_proposal_that_takes_hours():
    async def go():
        async with stage("memory") as s:
            chat = await s.link()
            holiday = await s.holiday(s.a)
            await s.review(s.b)
            await s.say(chat, "de 10 a 14")
            assert s.last(chat).key == "change.confirm"
            assert (await s.proposal(holiday)).step == "change.confirm"

    run(go())


def test_a_greeting_with_several_open_is_not_answered_and_more_than_three_ask_to_quote():
    async def go():
        async with stage("memory") as s:
            for n in (3, 4):
                s.store.locations[f"loc_x{n}"] = MemoryLocation(s.account, f"Sitio {n}")
            chat = await s.link()
            for location in (s.a, s.b, "loc_x3"):
                await s.review(location)
            sent = len(s.sent(chat))
            await s.say(chat, "gracias")
            assert len(s.sent(chat)) == sent
            await s.say(chat, "sí")
            assert s.last(chat).titles == (TITLE_A, TITLE_B, "Sitio 3")
            await s.review("loc_x4")
            await s.say(chat, "sí")
            assert s.last(chat).key == "text.quote"
            assert s.actions.requests == []

    run(go())


def test_a_which_button_of_an_old_step_is_stale():
    async def go():
        async with stage("memory") as s:
            chat = await s.link()
            pa = await s.review(s.a)
            await s.review(s.b)
            await s.say(chat, "sí")
            which = s.last(chat)
            await s.press(chat, s.about(chat, pa), "Editar")  # A moves to another step
            await s.press(chat, which, TITLE_A)
            assert s.notice() == cm.render("stale.inactive")

    run(go())


def test_six_digits_are_a_text_when_a_seen_proposal_waits_for_one():
    async def go():
        async with stage("memory") as s:
            chat = await s.link()
            pa = await s.review(s.a)
            await s.review(s.b)
            await s.press(chat, s.about(chat, pa), "Editar")
            await s.say(chat, "123456")
            assert s.last(chat).key == "review.edit_confirm"

    run(go())


def test_a_filtered_chat_gets_only_its_business_and_its_welcome_names_only_it():
    async def go():
        s = MemoryStage.build()
        original = s.store.link_identity

        async def linked_to_b(*args, **kwargs):
            result = await original(*args, **kwargs)
            filtered = replace(result.identity, location_filter=(s.b,))
            s.store.identities[filtered.id] = filtered
            return replace(result, identity=filtered)

        owner_chat = await s.link()
        s.store.link_identity = linked_to_b
        manager = await s.link()
        assert s.last(manager).key == "welcome.linked"
        assert TITLE_B in s.last(manager).text and TITLE_A not in s.last(manager).text
        pa = await s.review(s.a)
        pb = await s.review(s.b)
        assert [m.message.proposal_id for m in s.sent(manager) if m.message.buttons] == [pb]
        assert {m.message.proposal_id for m in s.sent(owner_chat) if m.message.buttons} == {
            pa,
            pb,
        }
        # Nothing of A reaches the manager, whatever it writes.
        await s.say(manager, "Gracias por todo")
        assert s.last(manager).key == "review.clarify_text"
        assert (await s.proposal(pb)).step == "review.clarify_text"
        assert (await s.proposal(pa)).step == "review.ask"

    run(go())


def test_with_nobody_seeing_a_business_its_proposal_waits():
    async def go():
        s = MemoryStage.build()
        chat = await s.link()
        await s.set_filter(chat, [s.a])
        pb = await s.review(s.b)
        assert (await s.proposal(pb)).state is S.SCHEDULED
        assert s.sent(chat)[-1].key == "welcome.linked_many"

    run(go())


def test_the_weekly_cap_is_per_business():
    async def go():
        s = MemoryStage.build(weekly_cap=1)
        chat = await s.link()
        first_a = await s.commercial(s.a, date(2026, 11, 27))
        await s.press(chat, s.about(chat, first_a), "Nada este año")
        second_a = await s.commercial(s.a, date(2026, 12, 20))
        first_b = await s.commercial(s.b, date(2026, 11, 27))
        assert (await s.proposal(second_a)).state is S.SCHEDULED  # A used its week
        assert (await s.proposal(first_b)).state is S.SENT  # B had its own

    run(go())


def test_a_business_that_is_not_active_gets_no_proposal():
    async def go():
        s = MemoryStage.build()
        await s.link()
        await s.deactivate(s.b)
        assert await s.review(s.b) is None
        assert await s.holiday(s.b) is None
        assert s.store.proposals == {}

    run(go())


def test_the_input_needs_its_business():
    s = MemoryStage.build()
    with pytest.raises(ValueError):
        run(
            s.service.submit_review(
                s.account, ReviewInput("rev_0000000000000001", 5, "", REPLY, "")
            )
        )
    with pytest.raises(ValueError):
        run(
            s.service.submit_key_date(
                s.account, KeyDateInput("k", "holiday", date(2026, 12, 8), "Inmaculada", "")
            )
        )


def test_with_one_active_business_the_messages_are_those_of_always():
    async def go():
        s = MemoryStage.build()
        await s.deactivate(s.b)
        chat = await s.link()
        assert s.last(chat).text == cm.render("welcome.linked", nombre=", Marta", negocio=TITLE_A)
        pa = await s.review(s.a, 5, "El arroz, espectacular.")
        assert s.about(chat, pa).text == cm.render(
            "review.new.positive",
            estrellas="5 estrellas",
            extracto="El arroz, espectacular.",
            respuesta=REPLY,
        )

    run(go())


def test_the_longest_message_with_header_and_prefix_fits_whatsapp():
    async def go():
        s = MemoryStage.build()
        long_title = "Restaurante " + "muy largo " * 10
        s.store.locations[s.a] = MemoryLocation(s.account, long_title)
        chat = await s.link()
        reply = ("Gracias por tu visita. " * 40)[: cm.SHOWN_REPLY_MAX].strip()
        pa = await s.service.submit_review(
            s.account, ReviewInput("rev_00000000000000aa", 2, "Mal " * 500, reply, s.a)
        )
        await s.review(s.b)
        s.now += timedelta(hours=25)  # the reminder: the longest prefix
        await s.service.tick()
        for sent in s.sent(chat):
            assert len(sent.text) <= cm.WHATSAPP_LIMITS.max_body
        assert s.about(chat, pa).key == "reminder.pending"

    run(go())
