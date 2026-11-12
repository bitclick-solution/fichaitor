"""The conversation: what happens when the owner writes or presses a button, and when
the system has something to tell them.

It only uses ports. It receives an inbound event of a channel or an event of the
system (a new review, a key date, the result of an action, the clock), loads the
state, applies the rules of `app.domain` and sends messages. When the owner
confirms, it hands an `ActionRequest` to `ProposalActions`; it never writes to
Google and never runs anything.

What holds whatever arrives:
- An action is requested only from `_confirm`, which needs the inbound event of an
  address linked, now, to the account that owns the proposal.
- What is confirmed is exactly what was shown: the suggested reply as stored, or the
  owner's own text as they sent it.
- The text of a review is only ever shown. It is never read to decide anything.
- Texts of the owner and of reviews are not logged.
"""

import logging
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from app.domain import channel_messages as cm
from app.domain import conversation as rules
from app.domain import link_codes
from app.domain.calendar import format_date_es
from app.domain.channel_messages import InboundEvent, InboundKind, MessageKind, OutboundMessage
from app.domain.conversation import ChannelIdentity, Conversation, HoursAnswer, HoursKind, Reply
from app.domain.proposals import PRIORITY_KEY_DATE as KEY_DATE_PRIORITY
from app.domain.proposals import (
    Proposal,
    ProposalEvent,
    ProposalState,
    ProposalType,
    advance,
    counts_towards_weekly_cap,
    idempotency_key,
    review_priority,
)
from app.ports import proposal_actions as pa
from app.ports.conversation_store import ConversationStore, LinkResult, MessageRecord
from app.ports.messaging import MessagingChannel, MessagingError
from app.ports.proposal_actions import ActionRejected, ActionRequest, ProposalActions

log = logging.getLogger("fichaitor.channels")

E, S = ProposalEvent, ProposalState

# The owner's own reply must be shown whole to be confirmed, inside a message that
# also fits WhatsApp (1,024 characters).
OWNER_TEXT_MAX = 900

# Outcomes of an action, as the executor reports them.
OUTCOME_TARGET_GONE = "target_gone"
OUTCOME_RECONNECT = "reconnect"
OUTCOME_TOO_LATE = "too_late"
OUTCOME_NO_REGULAR_HOURS = "no_regular_hours"


@dataclass(frozen=True)
class ServiceConfig:
    panel_url: str
    signup_url: str
    reconnect_url: str = ""
    weekly_cap: int = rules.DEFAULT_WEEKLY_CAP
    quiet: rules.QuietHours = rules.QuietHours()
    link_code_pepper: str = ""
    privacy_url: str = "https://fichaitor.es/privacidad/"
    # Wrong codes of every chat together per window (B3): past it, typed codes wait.
    link_global_max_failures: int = link_codes.DEFAULT_GLOBAL_MAX_FAILURES


@dataclass(frozen=True)
class ReviewInput:
    review_id: str
    stars: int
    text: str  # written by a third party: data, only ever shown
    reply: str  # the suggested reply, already validated by whoever drafted it


@dataclass(frozen=True)
class KeyDateInput:
    key: str  # e.g. "holiday:2026-12-08": one proposal per key and account
    kind: str  # "holiday" | "commercial" | "season"
    day: date
    name: str
    regular_hours: str = ""  # "de 9:00 a 14:00", if known, for the result message
    location_id: str = ""  # the profile it is about ("loc_..."): the action targets it
    # Ask "¿Abres?" with Abro normal / Cierro / Otro horario, as for a holiday, even if
    # it is not one (Nochebuena). Holidays always do.
    ask_hours: bool = False


@dataclass(frozen=True)
class ActionOutcome:
    ok: bool
    error: str | None = None  # OUTCOME_ constants, or anything else for "failed"
    link: str = ""  # where the change can be seen, if known


@dataclass(frozen=True)
class RoutedText:
    """What a router understood. It can choose an option that confirms nothing, or
    give hours (which are then shown and confirmed like any others). It has no way
    to confirm."""

    option: str | None = None
    hours: HoursAnswer | None = None


class FreeTextRouter(Protocol):
    """Extension point for a model based router (docs/03-lld.md, "Enrutador"). It is
    asked only when the fixed rules did not understand a text."""

    async def route(self, text: str, step: str, options: tuple[str, ...]) -> RoutedText | None: ...


@dataclass(frozen=True)
class Step:
    options: tuple[str, ...] = ()
    affirmative: str | None = None  # what "sí", "vale", "ok" mean here
    negative: str | None = None  # what "no", "déjalo" mean here
    hours: bool = False  # a text with hours is an answer here
    confirming: frozenset[str] = frozenset()  # options that end in an action


REVIEW_ASK, REVIEW_OPTIONS = "review.ask", "review.options"
REVIEW_EDIT_WAIT, REVIEW_EDIT_CONFIRM = "review.edit_wait", "review.edit_confirm"
REVIEW_CLARIFY_TEXT = "review.clarify_text"
HOLIDAY_ASK, KEYDATE_ASK = "holiday.ask", "keydate.ask"
HOURS_WAIT, HOURS_OPTIONS, CHANGE_CONFIRM = "hours.wait", "hours.options", "change.confirm"
ACTION_REJECTED = "action.rejected"

_REVIEW_CHOICES = Step(
    ("publish", "edit", "no_reply"), "publish", "no_reply", confirming=frozenset({"publish"})
)
_YES = frozenset({"yes_send"})
STEPS: Mapping[str, Step] = {
    REVIEW_ASK: _REVIEW_CHOICES,
    REVIEW_OPTIONS: _REVIEW_CHOICES,
    REVIEW_EDIT_WAIT: Step(),
    REVIEW_EDIT_CONFIRM: Step(("yes_send", "change", "cancel"), "yes_send", "cancel", False, _YES),
    REVIEW_CLARIFY_TEXT: Step(("yes_send", "no"), "yes_send", "no", False, _YES),
    # "sí" and "no" to "¿Abres?" could mean two things each: they confirm nothing.
    HOLIDAY_ASK: Step(
        ("open_normal", "closed_day", "other_hours"),
        hours=True,
        confirming=frozenset({"open_normal", "closed_day"}),
    ),
    KEYDATE_ASK: Step(
        ("change_hours", "nothing_this_year"), "change_hours", "nothing_this_year", hours=True
    ),
    HOURS_WAIT: Step(hours=True),
    HOURS_OPTIONS: Step(
        ("normal_hours", "closed", "in_panel"),
        hours=True,
        confirming=frozenset({"normal_hours", "closed"}),
    ),
    CHANGE_CONFIRM: Step(("yes_send", "change", "cancel"), "yes_send", "cancel", True, _YES),
    ACTION_REJECTED: Step(("change", "leave_it"), None, "leave_it", True),
}


def _showable(text: str) -> bool:
    return bool(text.strip()) and not cm._has_control(text, allowed="\n")


class ConversationService:
    def __init__(
        self,
        store: ConversationStore,
        channels: Mapping[str, MessagingChannel],
        actions: ProposalActions,
        config: ServiceConfig,
        *,
        clock: Callable[[], datetime],
        router: FreeTextRouter | None = None,
    ) -> None:
        self._store = store
        self._channels = channels
        self._actions = actions
        self._config = config
        self._clock = clock
        self._router = router

    # ==================================================================== inbound

    async def handle_inbound(self, event: InboundEvent) -> None:
        """A message, a button press or a "start" of any chat. Never raises for
        anything the sender did."""
        now = self._clock()
        if event.channel not in self._channels:
            return
        if not await self._store.record_inbound(event.channel, event.event_id, now):
            await self._ack(event)  # seen before: handled once
            return
        identity = await self._store.get_identity(event.channel, event.address)
        if event.kind is InboundKind.COMMAND:
            await self._command(event, identity, now)
            return
        if await self._is_link_attempt(event, identity):
            await self._handle_code(event, identity, now)
            return
        if identity is None:
            await self._unlinked(event, now)
            return
        async with self._store.lock(identity.account_id):
            await self._store.touch_inbound(identity.id, now)
            if event.kind is InboundKind.BUTTON:
                await self._button(event, identity, now)
            elif event.kind is InboundKind.TEXT:
                await self._text(event, identity, now)
            else:  # "start" without a code from a linked chat
                await self._record_in(event, identity, "[inicio]", now)
                await self._reply(identity, "generic.neutral")
            await self._pump(identity.account_id, now)

    async def _is_link_attempt(self, event: InboundEvent, identity: ChannelIdentity | None) -> bool:
        if event.kind is InboundKind.START:
            return bool(event.text.strip())
        if event.kind is not InboundKind.TEXT or not (
            link_codes.is_code_shaped(event.text) or link_codes.is_token_shaped(event.text)
        ):
            return False
        if identity is None:
            return True
        # Six digits from a linked chat are a code, unless a proposal is waiting for
        # a text (then they are that text).
        conversation = await self._store.get_conversation(identity.account_id)
        active = await self._active(conversation)
        return active is None or bool(STEPS[active.step].options)

    # ------------------------------------------------------------------- linking

    async def _handle_code(
        self, event: InboundEvent, identity: ChannelIdentity | None, now: datetime
    ) -> None:
        """A code arrives: six digits typed, or the token of the link. Neither is ever
        stored nor logged.

        B3 of docs/seguridad/revision-2026-11-12.md: the attempt of the chat is counted
        before the code is looked at, in one atomic step of the store, so that codes of
        the same chat handled at once cannot slip under the limit; and the wrong codes of
        every chat together are counted too. Past that global limit, typed codes are not
        looked at until the window ends; the token of the link (which cannot be guessed)
        still works."""
        sender_key = link_codes.address_hash(event.channel, event.address)
        attempt = await self._store.take_link_attempt(sender_key, now)
        if attempt is None:
            return  # blocked: no answer for an hour
        code = event.text.strip()
        typed = link_codes.is_code_shaped(code)
        if typed and await self._store.link_failures(now) >= self._config.link_global_max_failures:
            log.warning("códigos escritos en pausa: demasiados códigos incorrectos")
            await self._send_raw(event.channel, event.address, "link.paused", self._links())
            return
        hashed = link_codes.code_hash(code, self._config.link_code_pepper)
        shaped = typed or link_codes.is_token_shaped(code)
        record = await self._store.get_link_code(hashed) if shaped else None
        account_id = None
        if link_codes.check_code(record, now) is link_codes.CodeCheck.OK:
            assert record is not None
            if identity is not None and identity.account_id == record.account_id:
                # Already linked to that account: the code is left for another chat.
                await self._store.clear_link_attempts(sender_key)
                await self._reply(identity, "generic.neutral")
                return
            account_id = await self._store.consume_link_code(hashed, now)
        if account_id is None:
            await self._store.count_link_failure(now)
            blocked = link_codes.is_blocked(attempt, now)
            key = "link.blocked" if blocked else "link.code_invalid"
            await self._send_raw(event.channel, event.address, key, self._links())
            return
        try:
            result = await self._store.link_identity(account_id, event.channel, event.address, now)
        except rules.InvalidAddress:
            log.warning("vinculación rechazada: dirección no válida en %s", event.channel)
            return
        await self._store.clear_link_attempts(sender_key)
        await self._record_in(event, result.identity, "[código]", now)
        await self._welcome(result, event)
        if result.previous_account_id is not None:
            async with self._store.lock(result.previous_account_id):
                await self._pump(result.previous_account_id, now)
        async with self._store.lock(account_id):
            await self._pump(account_id, now)

    async def _welcome(self, result: LinkResult, event: InboundEvent) -> None:
        identity = result.identity
        if result.outcome is rules.LinkOutcome.MOVED:
            await self._reply(identity, "link.moved", **self._channel_words(identity.channel))
        info = await self._store.get_account_info(identity.account_id)
        name = cm.one_line(event.sender_name, cm.NAME_MAX)
        await self._reply(
            identity,
            "welcome.linked",
            nombre=f", {name}" if name else "",
            negocio=cm.one_line(info.business_name, 80) or "tu negocio",
        )

    async def _unlinked(self, event: InboundEvent, now: datetime) -> None:
        """An address that is not linked: the generic answer, once a day, and nothing
        else. No proposal, no action, no data of any business."""
        if event.kind is InboundKind.BUTTON:
            await self._ack(event, cm.render("stale.inactive"))
            return
        sender_key = link_codes.address_hash(event.channel, event.address)
        if not await self._store.claim_unlinked_reply(sender_key, now):
            return  # blocked, or already answered in the last 24 hours
        values = {**self._channel_words(event.channel), "enlace_alta": self._config.signup_url}
        await self._send_raw(event.channel, event.address, "unlinked.reply", values)

    async def _command(
        self, event: InboundEvent, identity: ChannelIdentity | None, now: datetime
    ) -> None:
        """/ayuda and /privacidad: a fixed answer to whoever asks, linked or not. No
        data of any business, nothing of any proposal. A blocked chat gets nothing."""
        if identity is None:
            sender = link_codes.address_hash(event.channel, event.address)
            if link_codes.is_blocked(await self._store.get_sender_state(sender), now):
                return
        words = self._channel_words(event.channel)
        values = {
            "este_chat_minusculas": words["este_chat"].lower(),
            "enlace_panel": self._config.panel_url,
            "enlace_privacidad": self._config.privacy_url,
        }
        key = "command.privacy" if event.text == cm.COMMAND_PRIVACY else "command.help"
        if identity is None:
            await self._send_raw(event.channel, event.address, key, values)
            return
        async with self._store.lock(identity.account_id):
            await self._store.touch_inbound(identity.id, now)
            await self._record_in(event, identity, f"/{event.text}", now)
            await self._reply(identity, key, **values)

    # ------------------------------------------------------------------- buttons

    async def _button(self, event: InboundEvent, identity: ChannelIdentity, now: datetime) -> None:
        press = cm.decode_button_id(event.button_id)
        proposal = await self._store.get_proposal(press.proposal_id) if press else None
        # Unknown and somebody else's get the same answer: nothing is revealed.
        if press is None or proposal is None or not rules.may_act(identity, proposal.account_id):
            await self._ack(event, cm.render("stale.inactive"))
            return
        await self._record_in(event, identity, press.option, now, proposal.id)
        if proposal.state is S.EXPIRED:
            await self._ack(event)
            await self._answer_expired(proposal, identity, now)
            return
        if not proposal.is_open:
            await self._ack(event, cm.render("stale.resolved"))
            return
        conversation = await self._store.get_conversation(identity.account_id)
        if conversation.active_proposal_id != proposal.id:
            await self._ack(event, cm.render("queue.wait"))
            return
        step = STEPS[proposal.step]
        if press.step_token != proposal.step_token or press.option not in step.options:
            await self._ack(event, cm.render("stale.inactive"))
            return
        await self._ack(event)
        await self._choose(proposal, press.option, event, identity, now)

    async def _answer_expired(
        self, proposal: Proposal, identity: ChannelIdentity, now: datetime
    ) -> None:
        """Flow 9: a late answer reopens the proposal if it still makes sense. It
        goes back to the queue and is shown again: the late press confirms nothing."""
        zone = await self._zone(proposal.account_id)
        if proposal.type is ProposalType.KEY_DATE:
            day = date.fromisoformat(proposal.payload["date"])
            if now.astimezone(zone).date() > day:
                await self._reply(identity, "expired.past", fecha_larga=format_date_es(day))
                return
            expires = rules.local_midnight(day + timedelta(days=1), zone)
        else:
            expires = None  # a review can always be answered: counted again when sent
        reopened = replace(
            self._at_first_step(proposal),
            state=advance(proposal.state, E.REOPEN),
            scheduled_for=now,
            remind_at=None,
            reminder_sent_at=now,  # it had its reminder
            expires_at=expires,
            sent_at=proposal.sent_at if proposal.type is ProposalType.KEY_DATE else None,
        )
        await self._store.save_proposal(reopened)
        conversation = await self._store.get_conversation(proposal.account_id)
        if await self._active(conversation) is not None:
            await self._reply(identity, "queue.wait")
            return
        # Shown at once: it answers what the owner just did.
        await self._deliver_next(proposal.account_id, now, answering=True)

    # ---------------------------------------------------------------------- text

    async def _text(self, event: InboundEvent, identity: ChannelIdentity, now: datetime) -> None:
        text = event.text
        conversation = await self._store.get_conversation(identity.account_id)
        active = await self._active(conversation)
        await self._record_in(event, identity, text, now, active.id if active else None)
        reply = rules.classify_reply(text)
        if active is None:
            if reply is Reply.COURTESY:
                return
            streak = conversation.unknown_streak + 1
            await self._store.save_conversation(replace(conversation, unknown_streak=streak))
            if streak == 1:
                await self._reply(identity, "fallback.first")
            elif streak == 2:
                await self._reply(identity, "fallback.other", **self._links())
            return
        if conversation.unknown_streak:
            await self._store.save_conversation(replace(conversation, unknown_streak=0))
        await self._text_on_step(active, text, reply, event, identity, now)

    async def _text_on_step(
        self,
        proposal: Proposal,
        text: str,
        reply: Reply,
        event: InboundEvent,
        identity: ChannelIdentity,
        now: datetime,
    ) -> None:
        step = STEPS[proposal.step]
        is_review = proposal.type is ProposalType.REVIEW_REPLY
        if proposal.step == REVIEW_EDIT_WAIT:
            # The next text after "Editar" is the reply, whatever it says.
            await self._offer_owner_text(proposal, text, REVIEW_EDIT_CONFIRM, identity)
            return
        if reply is Reply.COURTESY:
            return
        if reply is Reply.AFFIRMATIVE and step.affirmative:
            await self._choose(proposal, step.affirmative, event, identity, now)
            return
        if reply is Reply.NEGATIVE and step.negative:
            await self._choose(proposal, step.negative, event, identity, now)
            return
        if is_review:
            if proposal.step in (REVIEW_EDIT_CONFIRM, REVIEW_CLARIFY_TEXT, ACTION_REJECTED):
                # Two texts in a row: the last one is the one that counts.
                await self._offer_owner_text(proposal, text, REVIEW_EDIT_CONFIRM, identity)
            elif proposal.slots.get("clarifications", 0) == 0:
                await self._offer_owner_text(
                    proposal, text, REVIEW_CLARIFY_TEXT, identity, clarifications=1
                )
            else:
                await self._show_step(
                    self._collecting(proposal, REVIEW_OPTIONS), [identity], MessageKind.REPLY
                )
            return
        hours = rules.parse_hours(text) if step.hours else None
        if hours is None and self._router is not None:
            routed = await self._router.route(text, proposal.step, step.options)
            if routed is not None and routed.hours is not None and step.hours:
                hours = routed.hours
            elif routed is not None and routed.option in set(step.options) - step.confirming:
                assert routed.option is not None
                await self._choose(proposal, routed.option, event, identity, now)
                return
        if hours is not None:
            await self._offer_hours(proposal, hours, identity)
            return
        if proposal.step == HOLIDAY_ASK and reply is not Reply.OTHER:
            await self._show_step(
                self._collecting(proposal, HOURS_OPTIONS), [identity], MessageKind.REPLY
            )
        elif proposal.step in (HOLIDAY_ASK, HOURS_WAIT):
            asked = proposal.slots.get("clarifications", 0)
            key = rules.clarification_key(asked)
            moved = self._collecting(
                proposal,
                HOURS_OPTIONS if key == "hours.options" else HOURS_WAIT,
                clarifications=asked + 1,
            )
            if key == "hours.options":
                await self._show_step(moved, [identity], MessageKind.REPLY)
            else:
                await self._clear_buttons(proposal)
                await self._store.save_proposal(moved)
                await self._reply(identity, key)
        else:
            # A step with closed options: they are shown again.
            await self._show_step(proposal, [identity], MessageKind.REPLY, keep_token=True)

    async def _offer_owner_text(
        self, proposal: Proposal, text: str, step: str, identity: ChannelIdentity, **slots: Any
    ) -> None:
        """Show the owner their own text, exactly, and ask whether to send it."""
        text = text.strip()  # what is shown is what is kept, and what is sent
        if len(text) > OWNER_TEXT_MAX:
            await self._reply(identity, "review.edit_too_long", **self._links())
            return
        if not _showable(text):
            await self._reply(
                identity, "google.rejected", motivo="el texto tiene caracteres que no admite"
            )
            return
        moved = self._to_confirm(proposal, step, text=text, **slots)
        await self._show_step(moved, [identity], MessageKind.REPLY)

    async def _offer_hours(
        self, proposal: Proposal, hours: HoursAnswer, identity: ChannelIdentity
    ) -> None:
        moved = self._to_confirm(
            proposal, CHANGE_CONFIRM, hours={"kind": hours.kind.value, "periods": hours.periods}
        )
        await self._show_step(moved, [identity], MessageKind.REPLY)

    # ------------------------------------------------------------------ choosing

    async def _choose(
        self,
        proposal: Proposal,
        option: str,
        event: InboundEvent,
        identity: ChannelIdentity,
        now: datetime,
    ) -> None:
        """The owner chose `option` of the current step, by button or by an exact word."""
        is_review = proposal.type is ProposalType.REVIEW_REPLY

        async def confirm(payload: Mapping[str, Any], confirmed: Mapping[str, Any] | None) -> None:
            await self._confirm(proposal, payload, confirmed, option, event, identity, now)

        step = proposal.step
        review_choices = step in (REVIEW_ASK, REVIEW_OPTIONS)
        ask_reply = (proposal, REVIEW_EDIT_WAIT, "review.edit_ask", option, identity)
        ask_hours = (proposal, HOURS_WAIT, "change.edit_ask", option, identity)
        if review_choices and option == "publish":
            await confirm(self._reply_payload(proposal, proposal.payload["reply"]), None)
        elif step in (REVIEW_EDIT_CONFIRM, REVIEW_CLARIFY_TEXT) and option == "yes_send":
            await confirm(self._reply_payload(proposal, proposal.slots["text"]), None)
        elif (review_choices and option == "edit") or (step, option) == (
            REVIEW_EDIT_CONFIRM,
            "change",
        ):
            await self._ask(*ask_reply)
        elif (step, option) == (REVIEW_CLARIFY_TEXT, "no"):
            await self._clear_buttons(proposal, option)
            await self._show_step(
                self._collecting(proposal, REVIEW_OPTIONS, clarifications=1),
                [identity],
                MessageKind.REPLY,
                cleared=True,
            )
        elif review_choices and option == "no_reply":
            await self._dismiss(proposal, option, identity, "review.skipped", "no_reply")
        elif (step, option) in ((HOLIDAY_ASK, "open_normal"), (HOURS_OPTIONS, "normal_hours")):
            await confirm(*self._hours_payload(proposal, HoursAnswer(HoursKind.REGULAR)))
        elif (step, option) in ((HOLIDAY_ASK, "closed_day"), (HOURS_OPTIONS, "closed")):
            await confirm(*self._hours_payload(proposal, HoursAnswer(HoursKind.CLOSED)))
        elif (step, option) == (CHANGE_CONFIRM, "yes_send"):
            stored = proposal.slots["hours"]
            answer = HoursAnswer(
                HoursKind(stored["kind"]), tuple(tuple(p) for p in stored["periods"])
            )
            await confirm(*self._hours_payload(proposal, answer))
        elif (step, option) in ((HOLIDAY_ASK, "other_hours"), (KEYDATE_ASK, "change_hours")):
            day = format_date_es(date.fromisoformat(proposal.payload["date"]))
            await self._ask(proposal, HOURS_WAIT, "hours.ask", option, identity, fecha_larga=day)
        elif (step, option) == (CHANGE_CONFIRM, "change"):
            await self._ask(*ask_hours)
        elif (step, option) == (ACTION_REJECTED, "change"):
            await self._ask(*(ask_reply if is_review else ask_hours))
        elif (step, option) == (KEYDATE_ASK, "nothing_this_year"):
            await self._dismiss(proposal, option, identity, "keydate.declined", "declined")
        elif (step, option) == (HOURS_OPTIONS, "in_panel"):
            await self._clear_buttons(proposal, option)
            moved = replace(self._collecting(proposal, HOURS_OPTIONS), slots={"in_panel": True})
            await self._store.save_proposal(replace(moved, message_refs={}))
            await self._release(proposal)
            await self._reply(identity, "generic.panel_link", **self._links())
        elif option in ("cancel", "leave_it"):
            await self._dismiss(proposal, option, identity, "generic.cancelled", "cancelled")
        else:
            log.warning("opción sin tratamiento en el paso %s", proposal.step)

    async def _ask(
        self,
        proposal: Proposal,
        step: str,
        key: str,
        option: str,
        identity: ChannelIdentity,
        **values: str,
    ) -> None:
        """Move to a step that waits for a text of the owner, and ask for it."""
        await self._clear_buttons(proposal, option)
        moved = self._collecting(proposal, step)
        await self._store.save_proposal(replace(moved, message_refs={}))
        await self._reply(identity, key, **values)

    async def _dismiss(
        self, proposal: Proposal, option: str, identity: ChannelIdentity, key: str, reason: str
    ) -> None:
        events = (
            (E.DECLINE,)
            if proposal.state in (S.COLLECTING, S.TO_CONFIRM)
            else (
                E.OWNER_REPLIED,
                E.DECLINE,
            )
        )
        await self._clear_buttons(proposal, option)
        await self._store.save_proposal(
            replace(
                proposal,
                state=advance(proposal.state, *events),
                outcome_reason=reason,
                message_refs={},
            )
        )
        await self._release(proposal)
        await self._reply(identity, key)

    # ---------------------------------------------------------------- confirming

    def _reply_payload(self, proposal: Proposal, text: str) -> dict[str, Any]:
        return {"review_id": proposal.source_ref, "text": text}

    def _hours_payload(
        self, proposal: Proposal, answer: HoursAnswer
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        day = proposal.payload["date"]
        if answer.kind is HoursKind.CLOSED:
            entries: list[dict[str, Any]] = [{"date": day, "closed": True}]
        elif answer.kind is HoursKind.REGULAR:
            entries = [{"date": day, "regular": True}]
        else:
            entries = [{"date": day, "open": o, "close": c} for o, c in answer.periods]
        return {"special_hours": entries}, {"kind": answer.kind.value, "periods": answer.periods}

    async def _confirm(
        self,
        proposal: Proposal,
        payload: Mapping[str, Any],
        confirmed: Mapping[str, Any] | None,
        option: str,
        event: InboundEvent,
        identity: ChannelIdentity,
        now: datetime,
    ) -> None:
        """The only place an action is requested. It is reached from an inbound event
        of a linked address of the account that owns the proposal, and nowhere else."""
        if not rules.may_act(identity, proposal.account_id) or not event.event_id:
            return
        path = {
            S.SENT: (E.OWNER_REPLIED, E.DATA_COMPLETE, E.CONFIRM),
            S.REMINDED: (E.OWNER_REPLIED, E.DATA_COMPLETE, E.CONFIRM),
            S.COLLECTING: (E.DATA_COMPLETE, E.CONFIRM),
            S.TO_CONFIRM: (E.CONFIRM,),
        }[proposal.state]
        executing = advance(proposal.state, *path)
        action_type = rules.ALLOWED_ACTIONS[proposal.type]
        request = ActionRequest(
            account_id=proposal.account_id,
            proposal_id=proposal.id,
            type=action_type,
            source_ref=proposal.source_ref,
            payload=payload,
            idempotency_key=idempotency_key(proposal.id, action_type),
            confirmed_at=now,
            confirmed_channel=identity.channel,
            confirmed_event_id=event.event_id,
            location_id=str(proposal.payload.get("location_id", "")),
        )
        try:
            receipt = await self._actions.create(request)
        except ActionRejected as exc:
            await self._creation_rejected(proposal, exc.code, option, identity)
            return
        await self._clear_buttons(proposal, option)
        summary = payload["text"] if "text" in payload else str(payload["special_hours"])
        await self._store.save_proposal(
            replace(
                proposal,
                state=executing,
                slots={**proposal.slots, "confirmed": confirmed or {}},
                confirmed_at=now,
                confirmed_event_id=event.event_id,
                confirmed_summary=summary,
                action_id=receipt.action_id,
                message_refs={},
            )
        )
        await self._release(proposal)

    async def _creation_rejected(
        self, proposal: Proposal, code: str, option: str, identity: ChannelIdentity
    ) -> None:
        """Nothing was created. Either the proposal ends, or the owner changes it, or
        it stays as it is to be confirmed again."""
        if code == pa.REJECT_TARGET_GONE:
            gone = "review.gone" if proposal.type is ProposalType.REVIEW_REPLY else "keydate.gone"
            await self._dismiss(proposal, option, identity, gone, OUTCOME_TARGET_GONE)
        elif code == pa.REJECT_INVALID:
            await self._clear_buttons(proposal, option)
            await self._show_step(
                self._collecting(proposal, ACTION_REJECTED),
                [identity],
                MessageKind.REPLY,
                cleared=True,
            )
        elif code == pa.REJECT_RECONNECT:
            await self._reply(
                identity, "reconnect.needed", enlace_reconectar=self._config.reconnect_url
            )
        else:
            await self._reply(identity, "google.failed", **self._links())

    # =================================================================== linking

    async def create_link_code(self, account_id: str) -> str:
        """For the web session: a new code, returned once and stored only as a hash.
        The account's earlier unused codes stop being valid."""
        return (await self.create_link(account_id)).code

    async def create_link(self, account_id: str) -> link_codes.IssuedLink:
        """The same, with the token for the link (`t.me/<bot>?start=<token>`)."""
        issued = link_codes.issue_link(account_id, self._clock(), self._config.link_code_pepper)
        await self._store.save_link_code(issued.record)
        return issued

    async def unlink(self, account_id: str, identity_id: str) -> bool:
        """From the panel. From this moment the address gets nothing and is not
        attended. False if it is not an active address of that account."""
        for identity in await self._store.list_identities(account_id):
            if identity.id == identity_id:
                await self._store.unlink_identity(identity_id, self._clock())
                return True
        return False

    # ============================================================= system events

    async def submit_review(self, account_id: str, review: ReviewInput) -> str | None:
        """A new review with a suggested reply. Returns the proposal id, or None if
        that review already had one. Raises ValueError if it cannot be shown whole."""
        if not 1 <= review.stars <= 5:
            raise ValueError("estrellas fuera de rango")
        if not _showable(review.reply) or len(review.reply) > cm.SHOWN_REPLY_MAX:
            raise ValueError("la respuesta propuesta no se puede mostrar entera")
        now = self._clock()
        proposal = Proposal(
            id=_new_id(),
            account_id=account_id,
            type=ProposalType.REVIEW_REPLY,
            source_ref=review.review_id,
            state=S.SCHEDULED,
            priority=review_priority(review.stars),
            created_at=now,
            scheduled_for=now,
            payload={"stars": review.stars, "review_text": review.text, "reply": review.reply},
            step=REVIEW_ASK,
            step_token=_new_token(),
        )
        self._first_message(proposal)  # raises now if it would not fit
        return await self._submit(proposal, now)

    async def submit_key_date(self, account_id: str, key_date: KeyDateInput) -> str | None:
        """A holiday or a commercial date to ask about. None if it already had a
        proposal or it is too late for it."""
        if key_date.kind not in rules.KEY_DATE_KINDS:
            raise ValueError("tipo de fecha desconocido")
        now = self._clock()
        zone = await self._zone(account_id)
        remind_at, expires_at = rules.key_date_deadlines(key_date.kind, key_date.day, zone)
        if now >= expires_at:
            return None
        proposal = Proposal(
            id=_new_id(),
            account_id=account_id,
            type=ProposalType.KEY_DATE,
            source_ref=key_date.key,
            state=S.SCHEDULED,
            priority=KEY_DATE_PRIORITY,
            created_at=now,
            scheduled_for=now,
            payload={
                "kind": key_date.kind,
                "date": key_date.day.isoformat(),
                "name": cm.one_line(key_date.name, 80),
                "regular_hours": cm.one_line(key_date.regular_hours, 80),
                **({"location_id": key_date.location_id} if key_date.location_id else {}),
                **({"ask": "hours"} if key_date.ask_hours else {}),
            },
            step=HOLIDAY_ASK
            if key_date.kind == rules.KIND_HOLIDAY or key_date.ask_hours
            else KEYDATE_ASK,
            step_token=_new_token(),
            remind_at=remind_at,
            expires_at=expires_at,
        )
        self._first_message(proposal)
        return await self._submit(proposal, now)

    async def _submit(self, proposal: Proposal, now: datetime) -> str | None:
        async with self._store.lock(proposal.account_id):
            if not await self._store.add_proposal(proposal):
                return None
            await self._pump(proposal.account_id, now)
        return proposal.id

    async def handle_action_result(self, proposal_id: str, outcome: ActionOutcome) -> None:
        """The executor finished the action of a proposal. Asked twice, told once."""
        found = await self._store.get_proposal(proposal_id)
        if found is None:
            return
        now = self._clock()
        async with self._store.lock(found.account_id):
            proposal = await self._store.get_proposal(proposal_id)
            if proposal is None or proposal.state is not S.EXECUTING:
                return
            event = E.APPLIED if outcome.ok else E.FAILED
            await self._store.save_proposal(
                replace(
                    proposal,
                    state=advance(proposal.state, event),
                    outcome_reason=None if outcome.ok else (outcome.error or "failed"),
                )
            )
            key, values = self._result_message(proposal, outcome)
            for identity in await self._store.list_identities(proposal.account_id):
                await self._reply(identity, key, kind=MessageKind.RESULT, **values)
            await self._pump(proposal.account_id, now)

    async def handle_action_finished(self, action_id: str, outcome: ActionOutcome) -> bool:
        """The same, for an executor that only knows the action. False if that action
        did not come from a conversation (it was confirmed in the panel)."""
        proposal = await self._store.get_proposal_by_action(action_id)
        if proposal is None:
            return False
        await self.handle_action_result(proposal.id, outcome)
        return True

    def _result_message(self, proposal: Proposal, outcome: ActionOutcome) -> tuple[str, dict]:
        if not outcome.ok:
            if outcome.error in (OUTCOME_TOO_LATE, OUTCOME_NO_REGULAR_HOURS) and (
                proposal.type is ProposalType.KEY_DATE
            ):
                day = format_date_es(date.fromisoformat(proposal.payload["date"]))
                if outcome.error == OUTCOME_TOO_LATE:
                    return "hours.too_late", {"fecha_larga": day}
                return "hours.no_regular", {"fecha_larga": day, **self._links()}
            if outcome.error == OUTCOME_TARGET_GONE:
                return "review.gone", {}
            if outcome.error == OUTCOME_RECONNECT:
                return "reconnect.needed", {"enlace_reconectar": self._config.reconnect_url}
            return "google.failed", self._links()
        if proposal.type is ProposalType.REVIEW_REPLY:
            return "review.sent", {"enlace_reseña": outcome.link}
        day = format_date_es(date.fromisoformat(proposal.payload["date"]))
        confirmed = proposal.slots.get("confirmed", {})
        if confirmed.get("kind") == HoursKind.CLOSED.value:
            return "holiday.sent_closed", {"fecha_larga": day, "enlace_ficha": outcome.link}
        if confirmed.get("kind") == HoursKind.REGULAR.value:
            regular = proposal.payload.get("regular_hours", "")
            return "holiday.sent_open", {
                "fecha_larga": day,
                "horario": f", {regular}" if regular else "",
                "enlace_ficha": outcome.link,
            }
        change = f"horario del {day}, {rules.hours_text(confirmed.get('periods', ()))}"
        return "change.sent", {"cambio": change, "enlace_ficha": outcome.link}

    async def tick(self) -> None:
        """What time alone brings: reminders, expiries and whatever was waiting for
        the quiet hours to end. To be run every few minutes."""
        now = self._clock()
        for account_id in await self._store.accounts_with_open_work():
            async with self._store.lock(account_id):
                await self._pump(account_id, now)

    # ======================================================== queue and delivery

    async def _pump(self, account_id: str, now: datetime) -> None:
        """Bring the thread of an account up to date. Called with its lock held."""
        conversation = await self._store.get_conversation(account_id)
        zone = await self._zone(account_id)
        may_write = rules.may_send_proactively(now, zone, self._config.quiet)
        identities = list(await self._store.list_identities(account_id))
        for proposal in await self._store.list_unfinished_proposals(account_id):
            due = rules.due_event(proposal, now)
            if due in (E.CANCEL, E.EXPIRE):
                reason = "expired" if due is E.EXPIRE else "not_sent_in_time"
                await self._store.save_proposal(
                    replace(
                        proposal,
                        state=advance(proposal.state, due),
                        outcome_reason=reason,
                        parked_at=None,
                    )
                )
                if conversation.active_proposal_id == proposal.id:
                    conversation = await self._release(proposal)
            elif due is E.REMIND and may_write and identities:
                if conversation.active_proposal_id == proposal.id:
                    await self._remind(proposal, identities, zone, now)
        active = await self._active(conversation)
        waiting = rules.queue_order(await self._store.list_unfinished_proposals(account_id), now)
        if active and waiting and may_write and rules.should_interrupt(active, waiting[0]):
            # A review of 1 or 2 stars takes the place of what was open.
            await self._store.save_proposal(replace(active, parked_at=now))
            await self._release(active)
        await self._deliver_next(account_id, now)

    async def _deliver_next(self, account_id: str, now: datetime, answering: bool = False) -> None:
        conversation = await self._store.get_conversation(account_id)
        if await self._active(conversation) is not None:
            return
        identities = list(await self._store.list_identities(account_id))
        if not identities:
            return  # nobody linked: nothing is sent, the panel has it
        zone = await self._zone(account_id)
        if not answering and not rules.may_send_proactively(now, zone, self._config.quiet):
            return
        proposals = await self._store.list_unfinished_proposals(account_id)
        for proposal in rules.queue_order(proposals, now):
            if rules.is_parked(proposal):
                resumed = replace(self._at_first_step(proposal), parked_at=None)
                prefix = cm.render("queue.resume", resumen_propuesta=self._summary(resumed))
                sent = await self._show_step(
                    resumed, identities, MessageKind.PROPOSAL, prefix=prefix, key="queue.resume"
                )
            else:
                first_time = proposal.sent_at is None
                if first_time and counts_towards_weekly_cap(proposal.type):
                    recent = await self._store.count_capped_sent_since(
                        account_id, now - rules.CAP_WINDOW
                    )
                    if not rules.under_weekly_cap(recent, self._config.weekly_cap):
                        continue  # postponed: it stays in the queue
                moved = replace(
                    proposal, state=advance(proposal.state, E.SEND), sent_at=proposal.sent_at or now
                )
                if moved.type is ProposalType.REVIEW_REPLY and first_time:
                    remind_at, expires_at = rules.review_deadlines(now)
                    if proposal.reminder_sent_at is not None:  # reopened: no second reminder
                        remind_at = None
                    moved = replace(moved, remind_at=remind_at, expires_at=expires_at)
                sent = await self._show_step(moved, identities, MessageKind.PROPOSAL)
            if sent:
                fresh = await self._store.get_conversation(account_id)
                await self._store.save_conversation(replace(fresh, active_proposal_id=proposal.id))
                return

    async def _remind(self, proposal: Proposal, identities: list, zone: Any, now: datetime) -> None:
        assert proposal.expires_at is not None
        # "Caduca el": the last day on which it can still be answered.
        last_day = (proposal.expires_at - timedelta(seconds=1)).astimezone(zone).date()
        prefix = cm.render(
            "reminder.pending",
            resumen_propuesta=self._summary(proposal),
            fecha_larga=format_date_es(last_day),
        )
        reminded = replace(proposal, state=advance(proposal.state, E.REMIND), reminder_sent_at=now)
        sent = await self._show_step(
            reminded,
            identities,
            MessageKind.PROPOSAL,
            prefix=prefix,
            key="reminder.pending",
            keep_token=True,
        )
        if not sent:
            await self._store.save_proposal(proposal)  # not reminded: tried again later

    # ================================================================== messages

    def _summary(self, proposal: Proposal) -> str:
        if proposal.type is ProposalType.REVIEW_REPLY:
            return f"la reseña de {cm.stars_text(proposal.payload['stars'])}"
        day = format_date_es(date.fromisoformat(proposal.payload["date"]))
        if proposal.payload["kind"] == rules.KIND_HOLIDAY:
            return f"el festivo del {day}"
        return f"{proposal.payload['name']} ({day})"

    def _first_message(self, proposal: Proposal) -> tuple[str, str]:
        """(key, text) of the message that opens a proposal."""
        data = proposal.payload
        if proposal.type is ProposalType.KEY_DATE:
            day = format_date_es(date.fromisoformat(data["date"]))
            if data["kind"] == rules.KIND_HOLIDAY:
                key = "holiday.ask"
                return key, cm.render(key, fecha_larga=day, nombre_festivo=data["name"])
            if data.get("ask") == "hours":
                return "eve.ask", cm.render("eve.ask", fecha_larga=day, nombre=data["name"])
            return "keydate.ask", cm.render("keydate.ask", evento=data["name"], fecha_larga=day)
        stars = cm.stars_text(data["stars"])
        # The review is somebody else's text: one line, bounded, between quotes. The
        # reply is never cut; if space is short, the excerpt gives way.
        whole = cm.quoted(data["review_text"], 10_000)
        if not whole:
            key = "review.new.no_text"
            return key, cm.render(key, estrellas=stars, respuesta=data["reply"])
        key = "review.new.negative" if data["stars"] <= 3 else "review.new.positive"
        fixed = len(cm.render(key, estrellas=stars, extracto="", respuesta=data["reply"]))
        room = min(cm.REVIEW_EXCERPT_MAX, cm.CORE_LIMITS.max_body - cm.PREFIX_RESERVE - fixed)
        if room < min(cm.REVIEW_EXCERPT_MIN, len(whole)):
            raise ValueError("la respuesta propuesta no deja sitio para la reseña")
        excerpt = cm.quoted(data["review_text"], room)
        return key, cm.render(key, estrellas=stars, extracto=excerpt, respuesta=data["reply"])

    def _step_message(self, proposal: Proposal) -> tuple[str, str]:
        """(key, text) of the message of the step the proposal is in."""
        step, slots = proposal.step, proposal.slots
        if step in (REVIEW_ASK, HOLIDAY_ASK, KEYDATE_ASK):
            return self._first_message(proposal)
        if step == REVIEW_OPTIONS:
            # With the suggested reply in the same message as its "Publicar".
            body = self._first_message(proposal)[1]
            return "review.clarify_options", f"{cm.render('review.clarify_options')}\n{body}"
        if step in (REVIEW_EDIT_CONFIRM, REVIEW_CLARIFY_TEXT):
            return step, cm.render(step, texto=slots["text"])
        if step == HOURS_OPTIONS:
            return "hours.options", cm.render("hours.options")
        if step == CHANGE_CONFIRM:
            day = format_date_es(date.fromisoformat(proposal.payload["date"]))
            hours = slots["hours"]
            if hours["kind"] == HoursKind.CLOSED.value:
                line = f"• {day[0].upper()}{day[1:]}: cerrado."
            elif hours["kind"] == HoursKind.REGULAR.value:
                line = f"• Horario del {day}: el normal."
            else:
                line = f"• Horario del {day}: {rules.hours_text(hours['periods'])}."
            return "change.confirm", cm.render("change.confirm", cambios=line)
        if step == ACTION_REJECTED:
            what = "el texto" if proposal.type is ProposalType.REVIEW_REPLY else "el horario"
            return "google.rejected", cm.render("google.rejected", motivo=f"{what} no es válido")
        raise ValueError(f"el paso {step} no tiene mensaje con botones")

    async def _show_step(
        self,
        proposal: Proposal,
        identities: list[ChannelIdentity],
        kind: MessageKind,
        *,
        prefix: str = "",
        key: str = "",
        keep_token: bool = False,
        cleared: bool = False,
    ) -> bool:
        """Send the message of the proposal's step, with its buttons, and store the
        proposal. False (and nothing stored) if no address received it."""
        if not keep_token and not cleared:
            await self._clear_buttons(proposal)
        if not keep_token:
            proposal = replace(proposal, step_token=_new_token())
        step_key, text = self._step_message(proposal)
        if prefix:
            text = f"{prefix}\n{text}"
        buttons = tuple(
            cm.Button(
                cm.encode_button_id(proposal.id, proposal.step_token, option),
                cm.button_title(option),
            )
            for option in STEPS[proposal.step].options
        )
        refs = dict(proposal.message_refs) if keep_token else {}
        for identity in identities:
            ref = await self._send(identity, key or step_key, text, kind, buttons, proposal.id)
            if ref is not None:
                refs[identity.id] = ref
        if not refs and kind is MessageKind.PROPOSAL:
            return False
        await self._store.save_proposal(
            replace(proposal, message_refs=refs, slots={**proposal.slots, "shown": text})
        )
        return True

    async def _clear_buttons(self, proposal: Proposal, chosen: str | None = None) -> None:
        """Leave the last message with buttons without them, with what was chosen."""
        shown = proposal.slots.get("shown", "")
        if not proposal.message_refs or not shown:
            return
        if chosen is not None:
            shown = f"{shown}\n\n{cm.render('fmt.choice', titulo=cm.button_title(chosen))}"
        identities = {i.id: i for i in await self._store.list_identities(proposal.account_id)}
        for identity_id, ref in proposal.message_refs.items():
            identity = identities.get(identity_id)
            if identity is not None:
                await self._channels[identity.channel].clear_buttons(identity.address, ref, shown)

    async def _reply(
        self,
        identity: ChannelIdentity,
        key: str,
        kind: MessageKind = MessageKind.REPLY,
        **values: str,
    ) -> None:
        await self._send(identity, key, cm.render(key, **values), kind)

    async def _send(
        self,
        identity: ChannelIdentity,
        key: str,
        text: str,
        kind: MessageKind,
        buttons: tuple[cm.Button, ...] = (),
        proposal_id: str | None = None,
    ) -> str | None:
        """Send to a linked address. None if the channel did not take it."""
        message = cm.validate(
            OutboundMessage(identity.id, kind, text, buttons, key=key, proposal_id=proposal_id)
        )
        try:
            sent = await self._channels[identity.channel].send(identity.address, message)
        except MessagingError as exc:
            log.warning("mensaje %s no enviado por %s: %s", key, identity.channel, exc)
            return None
        conversation = await self._store.get_conversation(identity.account_id)
        await self._store.record_message(
            MessageRecord(
                conversation_id=conversation.id,
                identity_id=identity.id,
                direction="out",
                kind=kind.value,
                body=text,
                at=self._clock(),
                channel=identity.channel,
                channel_message_id=sent.channel_message_id,
                key=key,
                proposal_id=proposal_id,
            )
        )
        return sent.channel_message_id

    async def _send_raw(self, channel: str, address: str, key: str, values: dict) -> None:
        """To an address that is not linked. Nothing about it is stored."""
        message = cm.validate(
            OutboundMessage(
                f"{channel}:unlinked", MessageKind.SYSTEM, cm.render(key, **values), key=key
            )
        )
        try:
            await self._channels[channel].send(address, message)
        except MessagingError as exc:
            log.warning("mensaje %s no enviado por %s: %s", key, channel, exc)

    async def _ack(self, event: InboundEvent, notice: str = "") -> None:
        if event.callback_id:
            await self._channels[event.channel].acknowledge(event.callback_id, notice)

    async def _record_in(
        self,
        event: InboundEvent,
        identity: ChannelIdentity,
        body: str,
        now: datetime,
        proposal_id: str | None = None,
    ) -> None:
        conversation = await self._store.get_conversation(identity.account_id)
        await self._store.record_message(
            MessageRecord(
                conversation_id=conversation.id,
                identity_id=identity.id,
                direction="in",
                kind=event.kind.value,
                body=body,
                at=now,
                channel=event.channel,
                channel_message_id=event.event_id,
                proposal_id=proposal_id,
            )
        )

    # ==================================================================== helpers

    def _links(self) -> dict[str, str]:
        return {"enlace_panel": self._config.panel_url}

    @staticmethod
    def _channel_words(channel: str) -> dict[str, str]:
        name, this_chat = cm.CHANNEL_WORDS.get(channel, (channel, "Este chat"))
        return {"canal": name, "este_chat": this_chat}

    async def _zone(self, account_id: str) -> Any:
        info = await self._store.get_account_info(account_id)
        return rules.timezone_for(info.timezone)

    async def _active(self, conversation: Conversation) -> Proposal | None:
        if conversation.active_proposal_id is None:
            return None
        proposal = await self._store.get_proposal(conversation.active_proposal_id)
        return proposal if proposal is not None and proposal.is_open else None

    async def _release(self, proposal: Proposal) -> Conversation:
        conversation = await self._store.get_conversation(proposal.account_id)
        if conversation.active_proposal_id == proposal.id:
            conversation = replace(conversation, active_proposal_id=None)
            await self._store.save_conversation(conversation)
        return conversation

    @staticmethod
    def _at_first_step(proposal: Proposal) -> Proposal:
        """Back to its opening question, with nothing collected."""
        if proposal.type is ProposalType.REVIEW_REPLY:
            step = REVIEW_ASK
        else:
            holiday = proposal.payload["kind"] == rules.KIND_HOLIDAY
            asks_hours = holiday or proposal.payload.get("ask") == "hours"
            step = HOLIDAY_ASK if asks_hours else KEYDATE_ASK
        state = proposal.state
        if state is S.TO_CONFIRM:
            state = advance(state, E.REQUEST_CHANGES)
        return replace(proposal, state=state, step=step, slots={}, message_refs={})

    @staticmethod
    def _collecting(proposal: Proposal, step: str, **slots: Any) -> Proposal:
        """The owner answered and something is still missing."""
        event = {
            S.SENT: E.OWNER_REPLIED,
            S.REMINDED: E.OWNER_REPLIED,
            S.COLLECTING: E.CLARIFY,
            S.TO_CONFIRM: E.REQUEST_CHANGES,
        }[proposal.state]
        return replace(
            proposal,
            state=advance(proposal.state, event),
            step=step,
            step_token=_new_token(),
            slots={**proposal.slots, **slots},
        )

    @classmethod
    def _to_confirm(cls, proposal: Proposal, step: str, **slots: Any) -> Proposal:
        """Everything is there: it only needs the owner's yes."""
        moved = cls._collecting(proposal, step, **slots)
        return replace(moved, state=advance(moved.state, E.DATA_COMPLETE))


def _new_id() -> str:
    return f"prop_{secrets.token_hex(8)}"


def _new_token() -> str:
    return secrets.token_hex(3)
