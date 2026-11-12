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
from app.domain.conversation import ChannelIdentity, HoursAnswer, HoursKind, Reply
from app.domain.proposals import (
    CANCELLED_BY_DEACTIVATION,
    OUTCOME_LOCATION_INACTIVE,
    OUTCOME_OFFBOARDING,
    Proposal,
    ProposalEvent,
    ProposalState,
    ProposalType,
    advance,
    counts_towards_weekly_cap,
    idempotency_key,
    review_priority,
)
from app.domain.proposals import PRIORITY_KEY_DATE as KEY_DATE_PRIORITY
from app.ports import proposal_actions as pa
from app.ports.conversation_store import (
    ConversationStore,
    Lane,
    LinkResult,
    LocationInactive,
    MessageRecord,
)
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

# The option of the buttons of "¿De qué negocio me hablas?" (text.which): it only sets
# the focus of that address on that proposal, and never answers anything.
WHICH_OPTION = "focus"
# The longest line of a business (with its line break) a message may open with: what a
# new proposal is checked against, so that it always fits whatever the account becomes.
_LONGEST_HEADER = "x" * (cm.HEADER_RESERVE - 1)


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
    location_id: str  # the active profile ("loc_...") the review is of: its lane


@dataclass(frozen=True)
class KeyDateInput:
    key: str  # e.g. "holiday:2026-12-08": one proposal per key and account
    kind: str  # "holiday" | "commercial" | "season"
    day: date
    name: str
    location_id: str  # the active profile it is about ("loc_..."): its lane and the action's
    regular_hours: str = ""  # "de 9:00 a 14:00", if known, for the result message
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
        # Six digits from a linked chat are a code, unless a proposal this chat sees is
        # waiting for a text (then they are that text).
        open_ones = await self._visible_active(identity)
        return all(STEPS[p.step].options for p in open_ones)

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
        name = cm.one_line(event.sender_name, cm.NAME_MAX)
        nombre = f", {name}" if name else ""
        active = list(await self._store.active_locations(identity.account_id))
        seen = [loc for loc in active if rules.sees(identity, loc)]
        filtered = identity.location_filter is not None
        if not filtered and len(active) <= 1:
            # One business (or none active yet): the welcome of always, with its name.
            if active:
                negocio = await self._store.location_label(identity.account_id, active[0])
            else:
                info = await self._store.get_account_info(identity.account_id)
                negocio = cm.one_line(info.business_name, 80)
            await self._reply(
                identity, "welcome.linked", nombre=nombre, negocio=negocio or "tu negocio"
            )
            return
        # Only the businesses this chat sees are named (docs/multinegocio.md, 10).
        labels = [await self._store.location_label(identity.account_id, loc) for loc in seen]
        if len(labels) <= 1:
            negocio = labels[0] if labels else "tu negocio"
            await self._reply(identity, "welcome.linked", nombre=nombre, negocio=negocio)
            return
        await self._reply(
            identity,
            "welcome.linked_many",
            nombre=nombre,
            negocios=cm.businesses_phrase(labels, filtered=filtered),
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
        # Unknown, somebody else's, of a business this chat does not see, or of a business
        # that is no longer active: the same answer, and nothing is revealed.
        if (
            press is None
            or proposal is None
            or not rules.may_act(identity, proposal.account_id, proposal.location_id)
            or proposal.outcome_reason == OUTCOME_LOCATION_INACTIVE
            or not await self._store.location_active(proposal.account_id, _lane_of(proposal))
        ):
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
        lane = await self._store.get_lane(proposal.account_id, _lane_of(proposal))
        if lane.active_proposal_id != proposal.id:
            await self._ack(event, cm.render("queue.wait"))
            return
        if press.step_token != proposal.step_token:
            await self._ack(event, cm.render("stale.inactive"))
            return
        if press.option == WHICH_OPTION:
            # "¿De qué negocio?": this one. The text was not kept: it is asked again.
            await self._ack(event)
            await self._store.set_focus(identity.id, proposal.id, now)
            await self._ask_again(proposal, identity)
            return
        if press.option not in STEPS[proposal.step].options:
            await self._ack(event, cm.render("stale.inactive"))
            return
        await self._ack(event)
        await self._store.set_focus(identity.id, proposal.id, now)
        await self._choose(proposal, press.option, event, identity, now)

    async def _ask_again(self, proposal: Proposal, identity: ChannelIdentity) -> None:
        """After "¿De qué negocio?": the step of that proposal again, asking for the text
        once more. A step with buttons is shown again with the same (still valid) ones."""
        again = cm.render("text.write_again")
        if STEPS[proposal.step].options:
            await self._show_step(
                proposal, [identity], MessageKind.REPLY, prefix=again, keep_token=True
            )
        else:
            await self._reply(identity, "text.write_again", about=proposal)

    async def _answer_expired(
        self, proposal: Proposal, identity: ChannelIdentity, now: datetime
    ) -> None:
        """Flow 9: a late answer reopens the proposal if it still makes sense. It
        goes back to the queue and is shown again: the late press confirms nothing."""
        zone = await self._zone(proposal.account_id)
        if proposal.type is ProposalType.KEY_DATE:
            day = date.fromisoformat(proposal.payload["date"])
            if now.astimezone(zone).date() > day:
                await self._reply(
                    identity, "expired.past", about=proposal, fecha_larga=format_date_es(day)
                )
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
        location_id = _lane_of(proposal)
        if await self._lane_active(proposal.account_id, location_id) is not None:
            await self._reply(identity, "queue.wait", about=proposal)
            return
        # Shown at once: it answers what the owner just did.
        await self._deliver_next(proposal.account_id, location_id, now, answering=True)

    # ---------------------------------------------------------------------- text

    async def _text(self, event: InboundEvent, identity: ChannelIdentity, now: datetime) -> None:
        text = event.text
        conversation = await self._store.get_conversation(identity.account_id)
        open_ones = {p.id: p for p in await self._visible_active(identity)}
        candidates = [
            rules.TextCandidate(
                proposal_id=p.id,
                message_ref=p.message_refs.get(identity.id, ""),
                any_text=p.step == REVIEW_EDIT_WAIT,
                hours=STEPS[p.step].hours,
            )
            for p in open_ones.values()
        ]
        focus = await self._store.get_focus(identity.id) if len(candidates) > 1 else None
        target = rules.resolve_text_target(text, candidates, event.reply_to, focus, now)
        active = open_ones.get(target.proposal_id or "")
        await self._record_in(event, identity, text, now, active.id if active else None)
        reply = rules.classify_reply(text)
        if target.kind is rules.TargetKind.IGNORE:
            return  # a greeting or a thanks, with several things open: no answer
        if target.kind is rules.TargetKind.QUOTE:
            await self._reply(identity, "text.quote")
            return
        if target.kind is rules.TargetKind.WHICH:
            await self._ask_which(identity, [open_ones[pid] for pid in target.choices])
            return
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
                await self._reply(identity, key, about=proposal)
        else:
            # A step with closed options: they are shown again.
            await self._show_step(proposal, [identity], MessageKind.REPLY, keep_token=True)

    async def _ask_which(self, identity: ChannelIdentity, proposals: list[Proposal]) -> None:
        """A text that could be about several open proposals: "¿De qué negocio me
        hablas?", with a button for each business (docs/multinegocio.md, 6.3). The text
        is not kept anywhere but in the record of messages."""
        labels = [await self._store.location_label(p.account_id, _lane_of(p)) for p in proposals]
        buttons = tuple(
            cm.Button(cm.encode_button_id(p.id, p.step_token, WHICH_OPTION), title)
            for p, title in zip(proposals, cm.choice_titles(labels), strict=True)
        )
        await self._send(
            identity, "text.which", cm.render("text.which"), MessageKind.REPLY, buttons
        )

    async def _offer_owner_text(
        self, proposal: Proposal, text: str, step: str, identity: ChannelIdentity, **slots: Any
    ) -> None:
        """Show the owner their own text, exactly, and ask whether to send it."""
        text = text.strip()  # what is shown is what is kept, and what is sent
        if len(text) > OWNER_TEXT_MAX:
            await self._reply(identity, "review.edit_too_long", about=proposal, **self._links())
            return
        if not _showable(text):
            await self._reply(
                identity,
                "google.rejected",
                about=proposal,
                motivo="el texto tiene caracteres que no admite",
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
            await self._reply(identity, "generic.panel_link", about=proposal, **self._links())
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
        await self._reply(identity, key, about=proposal, **values)

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
        await self._reply(identity, key, about=proposal)

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
        if not rules.may_act(identity, proposal.account_id, proposal.location_id):
            return
        if not event.event_id:
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
            location_id=proposal.location_id or str(proposal.payload.get("location_id", "")),
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
                identity,
                "reconnect.needed",
                about=proposal,
                enlace_reconectar=self._config.reconnect_url,
            )
        else:
            await self._reply(identity, "google.failed", about=proposal, **self._links())

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
        """A new review of one active profile, with a suggested reply. Returns the
        proposal id, or None if that review already had one or the profile is not an
        active profile of the account. Raises ValueError if it cannot be shown whole."""
        if not review.location_id:
            raise ValueError("reseña sin ficha")
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
            location_id=review.location_id,
        )
        # Raises now if it would not fit, even with the longest header.
        self._first_message(proposal, _LONGEST_HEADER)
        return await self._submit(proposal, now)

    async def submit_key_date(self, account_id: str, key_date: KeyDateInput) -> str | None:
        """A holiday or a commercial date to ask about, for one active profile. None if
        it already had a proposal, it is too late for it or the profile is not an active
        profile of the account."""
        if not key_date.location_id:
            raise ValueError("fecha clave sin ficha")
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
                "location_id": key_date.location_id,
                **({"ask": "hours"} if key_date.ask_hours else {}),
            },
            step=HOLIDAY_ASK
            if key_date.kind == rules.KIND_HOLIDAY or key_date.ask_hours
            else KEYDATE_ASK,
            step_token=_new_token(),
            remind_at=remind_at,
            expires_at=expires_at,
            location_id=key_date.location_id,
        )
        self._first_message(proposal, _LONGEST_HEADER)
        return await self._submit(proposal, now)

    async def _submit(self, proposal: Proposal, now: datetime) -> str | None:
        async with self._store.lock(proposal.account_id):
            # With a leave scheduled nothing new is proposed (B12).
            if await self._store.on_leave(proposal.account_id):
                return None
            try:
                added = await self._store.add_proposal(proposal)
            except LocationInactive:
                return None  # not an active profile of the account: nothing is proposed
            if not added:
                return await self._revive(proposal, now)
            await self._pump(proposal.account_id, now)
        return proposal.id

    async def _revive(self, proposal: Proposal, now: datetime) -> str | None:
        """The same origin already had a proposal. If a leave cancelled it and the leave
        was restored, it is offered again as new, under its own id and with what it was
        created with. Otherwise it is not proposed twice. Called with the lock held."""
        earlier = await self._store.get_proposal_by_source(
            proposal.account_id, proposal.type, proposal.source_ref
        )
        if (
            earlier is None
            or earlier.state is not S.CANCELLED
            or earlier.outcome_reason != OUTCOME_OFFBOARDING
        ):
            return None
        await self._store.save_proposal(
            replace(proposal, id=earlier.id, payload=earlier.payload, created_at=earlier.created_at)
        )
        await self._pump(proposal.account_id, now)
        return earlier.id

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
            # With a leave scheduled the outcome is recorded and nothing is told (B12).
            if await self._store.on_leave(proposal.account_id):
                return
            key, values = self._result_message(proposal, outcome)
            # Only to the chats that see that business.
            for identity in await self._identities(proposal):
                await self._reply(identity, key, kind=MessageKind.RESULT, about=proposal, **values)
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
        the quiet hours to end. To be run every few minutes. Lane by lane, each under the
        lock of its account."""
        now = self._clock()
        for account_id, location_id in await self._store.lanes_with_open_work():
            async with self._store.lock(account_id):
                if not await self._store.on_leave(account_id):
                    await self._pump_lane(account_id, location_id, now)

    # ======================================================== queue and delivery
    # One thread per account (the lock, the streak of texts not understood) and one lane
    # per profile: at most one active proposal per profile (docs/multinegocio.md, 6.1).
    # The queue, the interruption by a review of 1 or 2 stars and the weekly cap work
    # inside each lane; the quiet hours are the account's.

    async def _pump(self, account_id: str, now: datetime) -> None:
        """Bring every lane of an account up to date. Called with its lock held.
        With a leave scheduled it does nothing: no proposal, reminder or expiry is sent
        until the leave is restored (B12)."""
        if await self._store.on_leave(account_id):
            return
        unfinished = await self._store.list_unfinished_proposals(account_id)
        for location_id in sorted({_lane_of(p) for p in unfinished}):
            await self._pump_lane(account_id, location_id, now)

    async def _pump_lane(self, account_id: str, location_id: str, now: datetime) -> None:
        """Bring one lane up to date. Called with the lock of the account held and
        without a leave scheduled."""
        if not await self._store.location_active(account_id, location_id):
            await self._cancel_inactive(account_id, location_id)
            return
        lane = await self._store.get_lane(account_id, location_id)
        zone = await self._zone(account_id)
        may_write = rules.may_send_proactively(now, zone, self._config.quiet)
        identities = list(await self._store.identities_for(account_id, location_id))
        for proposal in await self._lane_unfinished(account_id, location_id):
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
                if lane.active_proposal_id == proposal.id:
                    lane = await self._release(proposal)
            elif due is E.REMIND and may_write and identities:
                if lane.active_proposal_id == proposal.id:
                    await self._remind(proposal, identities, zone, now)
        active = await self._lane_active(account_id, location_id)
        waiting = rules.queue_order(await self._lane_unfinished(account_id, location_id), now)
        if active and waiting and may_write and rules.should_interrupt(active, waiting[0]):
            # A review of 1 or 2 stars takes the place of what was open in its lane.
            await self._store.save_proposal(replace(active, parked_at=now))
            await self._release(active)
        await self._deliver_next(account_id, location_id, now)

    async def _cancel_inactive(self, account_id: str, location_id: str) -> None:
        """Lazy cleanup (docs/multinegocio.md, 6.5): the profile stopped being active
        (removed by the worker, or by a reconnection, which cannot touch proposals).
        What was not confirmed is cancelled with `location_inactive`, without telling
        anybody, and its lane is freed. What is executing keeps its course."""
        for proposal in await self._lane_unfinished(account_id, location_id):
            if proposal.state in CANCELLED_BY_DEACTIVATION:
                await self._store.save_proposal(
                    replace(
                        proposal,
                        state=S.CANCELLED,
                        outcome_reason=OUTCOME_LOCATION_INACTIVE,
                        parked_at=None,
                        message_refs={},
                    )
                )
        lane = await self._store.get_lane(account_id, location_id)
        if lane.active_proposal_id is not None:
            await self._store.save_lane(replace(lane, active_proposal_id=None))

    async def _deliver_next(
        self, account_id: str, location_id: str, now: datetime, answering: bool = False
    ) -> None:
        if await self._store.on_leave(account_id):
            return
        if await self._lane_active(account_id, location_id) is not None:
            return
        identities = list(await self._store.identities_for(account_id, location_id))
        if not identities:
            return  # nobody sees this business: nothing is sent, the panel has it
        zone = await self._zone(account_id)
        if not answering and not rules.may_send_proactively(now, zone, self._config.quiet):
            return
        proposals = await self._lane_unfinished(account_id, location_id)
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
                    # The weekly cap is per business: each one gets its service.
                    recent = await self._store.count_capped_sent_since(
                        account_id, now - rules.CAP_WINDOW, location_id=location_id
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
                await self._store.save_lane(Lane(account_id, location_id, proposal.id))
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

    def _first_message(self, proposal: Proposal, header: str = "") -> tuple[str, str]:
        """(key, text) of the message that opens a proposal, without its header. With a
        `header` (the line of the business that will go above it), the excerpt of a review
        leaves room for it."""
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
        room = cm.review_excerpt_room(fixed, header)
        if room < min(cm.REVIEW_EXCERPT_MIN, len(whole)):
            raise ValueError("la respuesta propuesta no deja sitio para la reseña")
        excerpt = cm.quoted(data["review_text"], room)
        return key, cm.render(key, estrellas=stars, extracto=excerpt, respuesta=data["reply"])

    def _step_message(self, proposal: Proposal, header: str = "") -> tuple[str, str]:
        """(key, text) of the message of the step the proposal is in, without its header."""
        step, slots = proposal.step, proposal.slots
        if step in (REVIEW_ASK, HOLIDAY_ASK, KEYDATE_ASK):
            return self._first_message(proposal, header)
        if step == REVIEW_OPTIONS:
            # With the suggested reply in the same message as its "Publicar".
            body = self._first_message(proposal, header)[1]
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
        proposal. False (and nothing stored) if no address received it. With several
        active businesses, the message opens with the one it is about."""
        if not keep_token and not cleared:
            await self._clear_buttons(proposal)
        if not keep_token:
            proposal = replace(proposal, step_token=_new_token())
        header = await self._header(proposal)
        step_key, text = self._step_message(proposal, header)
        if prefix:
            text = f"{prefix}\n{text}"
        if header:
            text = cm.with_header(header, text)
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
        identities = {i.id: i for i in await self._identities(proposal)}
        for identity_id, ref in proposal.message_refs.items():
            identity = identities.get(identity_id)
            if identity is not None:
                await self._channels[identity.channel].clear_buttons(identity.address, ref, shown)

    async def _reply(
        self,
        identity: ChannelIdentity,
        key: str,
        kind: MessageKind = MessageKind.REPLY,
        *,
        about: Proposal | None = None,
        **values: str,
    ) -> None:
        """A message of the catalog. `about`: the proposal it answers within, whose
        business opens it when the account has several active ones."""
        text = cm.render(key, **values)
        header = await self._header(about) if about is not None else ""
        if header:
            text = cm.with_header(header, text)
        await self._send(identity, key, text, kind)

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

    async def _lane_active(self, account_id: str, location_id: str) -> Proposal | None:
        """The open proposal of that lane, if any."""
        lane = await self._store.get_lane(account_id, location_id)
        if lane.active_proposal_id is None:
            return None
        proposal = await self._store.get_proposal(lane.active_proposal_id)
        return proposal if proposal is not None and proposal.is_open else None

    async def _lane_unfinished(self, account_id: str, location_id: str) -> list[Proposal]:
        return [
            p
            for p in await self._store.list_unfinished_proposals(account_id)
            if _lane_of(p) == location_id
        ]

    async def _visible_active(self, identity: ChannelIdentity) -> list[Proposal]:
        """The open proposals of the lanes of the account that this address sees: what a
        text of it may be about (the candidates of docs/multinegocio.md, 6.3)."""
        unfinished = await self._store.list_unfinished_proposals(identity.account_id)
        lanes = {_lane_of(p) for p in unfinished}
        # In the order of the businesses (the oldest activation first): the order of the
        # buttons of "¿De qué negocio?".
        ordered = [loc for loc in await self._store.active_locations(identity.account_id)]
        ordered += sorted(lanes - set(ordered))
        found: list[Proposal] = []
        for location_id in (loc for loc in ordered if loc in lanes):
            if not rules.sees(identity, location_id or None):
                continue
            active = await self._lane_active(identity.account_id, location_id)
            if active is not None:
                found.append(active)
        return found

    async def _identities(self, proposal: Proposal) -> list[ChannelIdentity]:
        """The linked addresses that see the business of that proposal."""
        return list(await self._store.identities_for(proposal.account_id, _lane_of(proposal)))

    async def _header(self, proposal: Proposal) -> str:
        """The name of the business a message about that proposal opens with, or empty
        when the account has a single active business: its messages stay as they always
        were (docs/multinegocio.md, 6.2)."""
        active = await self._store.active_locations(proposal.account_id)
        if len(active) < 2:
            return ""
        return await self._store.location_label(proposal.account_id, _lane_of(proposal))

    async def _release(self, proposal: Proposal) -> Lane:
        """Free the lane of that proposal, if it holds it."""
        lane = await self._store.get_lane(proposal.account_id, _lane_of(proposal))
        if lane.active_proposal_id == proposal.id:
            lane = replace(lane, active_proposal_id=None)
            await self._store.save_lane(lane)
        return lane

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


def _lane_of(proposal: Proposal) -> str:
    """The lane of a proposal: its profile. A key date written before proposals had a
    column for it names its profile in its payload."""
    if proposal.location_id:
        return proposal.location_id
    return str(proposal.payload.get("location_id", ""))


def _new_id() -> str:
    return f"prop_{secrets.token_hex(8)}"


def _new_token() -> str:
    return secrets.token_hex(3)
