"""Step 7 of docs/admin-facturacion.md: an account blocked for non-payment, in the worker
and in the conversation, with everything in memory. Google, the model and Telegram are
fakes: no real call is ever made.

Decisions of the owner (sections 1.1 and 9.0): the whole account is blocked, the free
profile too, with no time limit, until it pays; and the pause deletes nothing.

- A confirmed action of a blocked account fails as `billing_blocked`, without calling
  Google, with its audit row and its result to the channel.
- The sync does not call Google and the sentiment analysis does not call the model.
- The conversation proposes nothing and delivers nothing; the owner who writes is told
  «Tu cuenta está en pausa…» at most once a day; a button answers the same and does
  nothing; /ayuda and /privacidad still answer; open proposals wait and resume.
"""

import asyncio

from app.adapters.sentiment_model.fake import FakeSentimentModel
from app.application.conversation_service import OUTCOME_BILLING_BLOCKED
from app.domain import actions
from app.domain import channel_messages as cm
from app.domain.channel_jobs import RESULT_BILLING_BLOCKED, result_error
from app.domain.channel_messages import InboundKind
from app.domain.conversation import BILLING_NOTICE_EVERY, may_send_billing_notice
from app.domain.jobs import DONE, Job
from app.domain.proposals import ProposalState as S
from app.domain.sentiment import SentimentPolicy
from app.worker.analyze_sentiment import AnalyzeSentiment
from app.worker.sync_profile import KIND as SYNC_KIND
from tests.test_conversation_service import ACC, OWNER, World
from tests.test_multinegocio_worker import LOCATION, NOW, SUB
from tests.test_multinegocio_worker import World as WorkerWorld

PAUSED = (
    "Tu cuenta está en pausa porque no hemos podido cobrar. Paga desde Facturación, en el "
    "panel, y todo vuelve a funcionar."
)


def run(coro):
    return asyncio.run(coro)


# =============================================================================
# The texts
# =============================================================================


def test_the_pause_text_is_the_one_of_the_owner_with_the_link_to_the_panel():
    text = cm.render("billing.paused", enlace_panel="https://fichaitor.es/app")
    assert text == f"{PAUSED} https://fichaitor.es/app"
    assert len(text) <= cm.CORE_LIMITS.max_body


def test_an_action_not_sent_for_non_payment_is_explained_in_the_panel_and_the_channel():
    for action_type in (actions.REPLY_REVIEW, actions.SET_SPECIAL_HOURS):
        message = actions.status_message(actions.FAILED, actions.ERROR_BILLING_BLOCKED, action_type)
        assert "en pausa" in message and "Facturación" in message
    assert result_error(actions.ERROR_BILLING_BLOCKED) == RESULT_BILLING_BLOCKED
    assert RESULT_BILLING_BLOCKED == OUTCOME_BILLING_BLOCKED


def test_the_pause_is_told_at_most_once_a_day():
    assert may_send_billing_notice(None, NOW)
    assert not may_send_billing_notice(NOW, NOW + BILLING_NOTICE_EVERY / 2)
    assert may_send_billing_notice(NOW, NOW + BILLING_NOTICE_EVERY)


# =============================================================================
# The worker
# =============================================================================


def test_a_confirmed_action_of_a_blocked_account_fails_without_calling_google():
    for kind in ("reply", "hours"):
        w = WorkerWorld()
        if kind == "reply":
            # Synced while it was paid; blocked between the confirmation and the job.
            run(w.queue.enqueue(SYNC_KIND, {"google_sub": SUB, "location": LOCATION}))
            run(w.drain())
            row = next(r for r in w.tables.reviews.values() if r.gbp_review_id == "g-low")
            action = w.tables.create_action(
                "00000000-0000-0000-0000-000000000001", row, "Lo sentimos.", w.now
            )
            w.store.blocked.add(SUB)
            refreshed = len(w.google.refreshed_with)
            run(w.queue.enqueue("reply_review", {"action_id": action.id}))
            run(w.drain())
        else:
            w.store.blocked.add(SUB)
            refreshed = 0
            action = run(w.hours())
        assert (action.state, action.error_code) == (actions.FAILED, actions.ERROR_BILLING_BLOCKED)
        assert w.google.write_attempts == 0 and not w.google.special_writes
        assert len(w.google.refreshed_with) == refreshed  # not even a token
        audit = [a for a in w.tables.audit if a.action_id == action.id]
        assert audit[-1].reason == actions.ERROR_BILLING_BLOCKED
        assert w.told() == [{"action_id": action.id, "ok": False, "error": "billing_blocked"}]
        # Not an error of the job: nothing to retry; the owner pays and confirms again.
        assert w.job_states("reply_review" if kind == "reply" else "set_special_hours") == [DONE]


def test_a_blocked_account_is_not_synced_and_is_synced_again_once_it_pays():
    w = WorkerWorld()
    w.store.blocked.add(SUB)
    run(w.queue.enqueue(SYNC_KIND, {"google_sub": SUB, "location": LOCATION}))
    run(w.drain())
    assert w.google.refreshed_with == [] and w.google.reads == []
    assert (SUB, LOCATION) not in w.store.summaries  # nothing written, nothing lost
    assert w.job_states(SYNC_KIND) == [DONE]
    # It pays: the next sync of the planner works as always.
    w.store.blocked.discard(SUB)
    run(w.queue.enqueue(SYNC_KIND, {"google_sub": SUB, "location": LOCATION}))
    run(w.drain())
    assert w.google.refreshed_with and w.store.summaries[(SUB, LOCATION)].status == "ok"


def test_a_blocked_account_keeps_what_was_stored_before():
    w = WorkerWorld()
    run(w.queue.enqueue(SYNC_KIND, {"google_sub": SUB, "location": LOCATION}))
    run(w.drain())
    before = w.store.summaries[(SUB, LOCATION)]
    reviews = dict(w.tables.reviews)
    w.store.blocked.add(SUB)
    w.now = NOW.replace(hour=12)
    run(w.queue.enqueue(SYNC_KIND, {"google_sub": SUB, "location": LOCATION}))
    run(w.drain())
    assert w.store.summaries[(SUB, LOCATION)] is before
    assert before.status == "ok" and w.tables.reviews == reviews


def test_the_sentiment_analysis_of_a_blocked_account_does_not_call_the_model():
    w = WorkerWorld()
    run(w.queue.enqueue(SYNC_KIND, {"google_sub": SUB, "location": LOCATION}))
    run(w.drain())
    model = FakeSentimentModel()
    job = AnalyzeSentiment(w.store, model, SentimentPolicy(), lambda: w.now)
    payload = {"google_sub": SUB, "location": LOCATION}
    w.store.blocked.add(SUB)
    run(job(Job("s", "analyze_sentiment", payload)))
    assert model.classify_calls == [] and model.summary_calls == []
    assert w.tables.analyses == {}
    # It pays: the same job reaches the model.
    w.store.blocked.discard(SUB)
    run(job(Job("s2", "analyze_sentiment", payload)))
    assert model.classify_calls and (SUB, LOCATION) in w.tables.analyses


# =============================================================================
# The conversation
# =============================================================================


def blocked_world() -> World:
    w = World()
    w.link()
    w.store.blocked.add(ACC)
    return w


def test_a_blocked_account_gets_no_new_proposal_and_no_delivery():
    w = blocked_world()
    sent = len(w.sent())
    assert w.review(stars=1) is None
    assert w.holiday() is None
    w.tick()
    assert len(w.sent()) == sent and not w.store.proposals


def test_the_owner_who_writes_is_told_the_pause_once_a_day_with_the_link():
    w = blocked_world()
    w.say("¿hay reseñas nuevas?")
    assert w.keys()[-1] == "billing.paused"
    assert w.last().text == f"{PAUSED} https://panel.test/app"
    count = len(w.sent())
    w.say("¿hola?")
    w.start("")
    w.advance(hours=23)
    w.say("¿sigues ahí?")
    assert len(w.sent()) == count  # once a day
    w.advance(hours=1)
    w.say("hola")
    assert w.keys()[-1] == "billing.paused" and len(w.sent()) == count + 1
    # What the owner wrote stays in the record of messages, as always.
    assert [m.body for m in w.store.messages if m.direction == "in"][1] == "¿hay reseñas nuevas?"


def test_the_pause_is_told_once_a_day_to_the_account_not_to_each_chat():
    w = World()
    w.link()
    w.link(address="1002")
    w.store.blocked.add(ACC)
    w.say("hola")
    w.say("hola", "1002")
    assert w.keys()[-1] == "billing.paused" and w.keys("1002")[-1] != "billing.paused"


def test_help_and_privacy_still_answer_while_blocked():
    w = blocked_world()
    for command, key in (
        (cm.COMMAND_HELP, "command.help"),
        (cm.COMMAND_PRIVACY, "command.privacy"),
    ):
        event = w._event(InboundKind.COMMAND, OWNER, text=command)
        w.run(w.service.handle_inbound(event))
        assert w.keys()[-1] == key
        w.run(w.service.handle_inbound(w._event(InboundKind.COMMAND, OWNER, text=command)))
        assert w.keys()[-1] == key  # every time: not counted as the daily pause


def test_open_proposals_wait_and_resume_once_the_account_pays():
    w = World()
    w.link()
    proposal_id = w.review(stars=5)
    assert w.active() == proposal_id
    buttons = w.last_with_buttons()
    w.store.blocked.add(ACC)
    sent = len(w.sent())
    # Its buttons answer the pause and do nothing.
    w.press("Publicar", sent=buttons)
    assert not w.requests
    assert w.proposal(proposal_id).state is S.SENT
    assert w.keys()[-1] == "billing.paused" and w.notice() == ""
    w.press("Publicar", sent=buttons)
    assert w.notice() == cm.render("billing.paused", enlace_panel="https://panel.test/app")
    assert not w.requests and len(w.sent()) == sent + 1
    # Days pass: no reminder, no expiry, nothing cancelled.
    w.advance(days=3)
    w.tick()
    assert len(w.sent()) == sent + 1
    assert w.proposal(proposal_id).state is S.SENT and w.active() == proposal_id
    # It pays: the same proposal goes on where it was.
    w.store.blocked.discard(ACC)
    w.press("Publicar", sent=buttons)
    assert len(w.requests) == 1
    assert w.proposal(proposal_id).state is S.EXECUTING


def test_a_waiting_proposal_is_delivered_once_the_account_pays():
    w = World()
    w.link()
    w.store.blocked.add(ACC)
    assert w.review(stars=5) is None
    w.store.blocked.discard(ACC)
    proposal_id = w.review(stars=4)
    assert proposal_id is not None and w.active() == proposal_id


def test_an_action_not_sent_for_non_payment_is_told_with_the_pause_text():
    w = World()
    w.link()
    proposal_id = w.review(stars=5)
    w.press("Publicar")
    assert w.proposal(proposal_id).state is S.EXECUTING
    w.store.blocked.add(ACC)
    w.result(proposal_id, ok=False, error=OUTCOME_BILLING_BLOCKED)
    assert w.keys()[-1] == "billing.paused"
    assert w.proposal(proposal_id).state is S.FAILED
