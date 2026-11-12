"""Where a reply confirmed by the channel becomes an action: the same door as the panel.

`ReviewReplyActions` implements `app.ports.proposal_actions.ProposalActions` with
`ReviewReplies.confirm`, the use case of `POST /api/v1/me/reviews/{id}/reply`:

- the text is checked against what Google accepts and never changed;
- `Store.create_reply_action` creates, all or nothing, the pending action with the
  confirmation time, its `reply_confirmed` audit row and the `reply_review` job, and
  only for a review of an active profile of that very account (the second barrier of
  ownership, after the conversation's own, which also checks that the chat sees that
  profile);
- at most one reply per review is on its way (`action_one_live_per_review`).

Only the executor of the worker writes to Google, after `claim_action` checks that the
account, its Google user, the profile and the review agree (M1).
"""

from app.application.review_replies import ReviewNotFound, ReviewReplies
from app.domain.review_reply import InvalidReplyText
from app.ports.proposal_actions import (
    REJECT_INVALID,
    REJECT_TARGET_GONE,
    REJECT_UNAVAILABLE,
    ActionReceipt,
    ActionRejected,
    ActionRequest,
)
from app.ports.store import ReplyInProgress

REPLY_REVIEW = "reply_review"


class ReviewReplyActions:
    def __init__(self, replies: ReviewReplies) -> None:
        self._replies = replies

    async def create(self, request: ActionRequest) -> ActionReceipt:
        if not request.confirmed_event_id or request.confirmed_at is None:
            raise AssertionError("acción sin confirmación registrada")
        if request.type != REPLY_REVIEW:
            # set_special_hours has no executor yet: nothing is created.
            raise ActionRejected(REJECT_UNAVAILABLE)
        review_id = request.payload.get("review_id")
        if review_id != request.source_ref:
            raise ActionRejected(REJECT_INVALID)
        try:
            action_id = await self._replies.confirm(
                request.account_id, review_id, request.payload.get("text")
            )
        except InvalidReplyText:
            raise ActionRejected(REJECT_INVALID) from None
        except ReviewNotFound:
            # Gone from Google, its profile is no longer active, or a leave is scheduled.
            raise ActionRejected(REJECT_TARGET_GONE) from None
        except ReplyInProgress:
            raise ActionRejected(REJECT_UNAVAILABLE) from None
        return ActionReceipt(action_id, created=True)
