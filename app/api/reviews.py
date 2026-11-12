"""Reviews of the connected profile and the replies to them.
Documented in docs/contrato-web-api.md, "Reseñas y respuestas".

Every route works on the selected profile of the account of the session and on nothing
else: a review or an action id from the request is only ever looked up among that
account's own. The API never talks to Google here. Publishing is a pending action plus
a job; the worker's executor does the write.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.session import active_account_guard, require_csrf_header
from app.api.summary_out import STATUS_PENDING, seconds
from app.application.profile_sync import ProfileSync
from app.application.review_replies import (
    DraftRateLimited,
    DraftRejected,
    DraftUnavailable,
    ReviewNotFound,
    ReviewReplies,
)
from app.domain.actions import ACTION_ID, PENDING, REVIEW_ID, status_label, status_message
from app.domain.profile_summary import STATUS_OK
from app.domain.review_reply import GOOGLE_REPLY_MAX_BYTES, InvalidReplyText
from app.ports.store import Account, ReplyInProgress, Store, StoredAction, StoredReview

log = logging.getLogger("fichaitor.reviews")

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

MSG_NO_PROFILE = "Primero elige con qué ficha quieres trabajar."
MSG_REVIEW_NOT_FOUND = "Esa reseña no está en tu ficha."
MSG_ACTION_NOT_FOUND = "No encontramos ese envío."
MSG_DRAFT_REJECTED = (
    "No hemos conseguido una respuesta que podamos proponerte para esta reseña. Escribe la tuya."
)
MSG_DRAFT_UNAVAILABLE = (
    "Ahora mismo no podemos redactar la respuesta. Escribe la tuya o prueba en un minuto."
)
MSG_DRAFT_RATE_LIMITED = "Has pedido muchas respuestas redactadas. Escribe la tuya o prueba mañana."
MSG_REPLY_IN_PROGRESS = "Ya hay una respuesta enviándose para esa reseña. Espera a que termine."
MSG_REPLY_NOT_SAVED = "No hemos podido enviar tu respuesta. Prueba otra vez."


class ReviewReplyOut(BaseModel):
    text: str
    updated_at: datetime | None


class ReviewOut(BaseModel):
    # Deliberately no field for the author of the review.
    id: str
    stars: int | None
    text: str
    created_at: datetime | None
    replied: bool
    reply: ReviewReplyOut | None


class ReviewsResponse(BaseModel):
    status: str
    synced_at: datetime | None
    stale: bool
    total: int
    reviews: list[ReviewOut]


class DraftResponse(BaseModel):
    review_id: str
    text: str
    cached: bool


class ReplyRequest(BaseModel):
    # The real limit is Google's, in bytes, and is checked with a message in Spanish.
    # This one only bounds what is accepted at all.
    text: str = Field(max_length=GOOGLE_REPLY_MAX_BYTES * 4)


class ActionResponse(BaseModel):
    id: str
    type: str
    review_id: str
    status: str
    message: str
    error: str | None
    confirmed_at: datetime | None
    finished_at: datetime | None


def _review_out(review: StoredReview) -> ReviewOut:
    replied = review.reply_text is not None
    return ReviewOut(
        id=review.id,
        stars=review.rating,
        text=review.text,
        created_at=seconds(review.created_at),
        replied=replied,
        reply=(
            ReviewReplyOut(text=review.reply_text or "", updated_at=seconds(review.reply_at))
            if replied
            else None
        ),
    )


def _action_out(action: StoredAction) -> ActionResponse:
    return ActionResponse(
        id=action.id,
        type=action.type,
        review_id=action.review_id,
        status=status_label(action.state),
        message=status_message(action.state, action.error_code),
        error=action.error_code,
        confirmed_at=seconds(action.confirmed_at),
        finished_at=seconds(action.finished_at),
    )


def build_router(
    *,
    store: Store,
    sync: ProfileSync,
    replies: ReviewReplies,
    clock: Callable[[], float],
) -> APIRouter:
    router = APIRouter()
    # Closed while a leave is scheduled: nothing is listed, drafted or published.
    current_account = active_account_guard(store, clock)

    def review_id_or_404(review_id: str) -> str:
        # The shape is checked before anything is looked up.
        if not REVIEW_ID.fullmatch(review_id):
            raise HTTPException(404, MSG_REVIEW_NOT_FOUND)
        return review_id

    @router.get("/api/v1/me/reviews", response_model=ReviewsResponse)
    async def my_reviews(
        filter: Literal["unanswered", "all"] = "unanswered",  # noqa: A002
        limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
        account: Account = Depends(current_account),  # noqa: B008
    ) -> ReviewsResponse:
        # Reading the list asks for a sync under the same rules as the summary, and
        # also when the reviews of the profile have never been stored.
        view = await sync.view(account.id, need_reviews=True)
        if view is None:
            raise HTTPException(409, MSG_NO_PROFILE)
        summary = view.summary
        synced_at = summary.reviews_synced_at if summary is not None else None
        if summary is None or (synced_at is None and summary.status == STATUS_OK):
            status = STATUS_PENDING
        else:
            status = summary.status
        page = await store.list_reviews(
            account.id, unanswered_only=filter == "unanswered", limit=limit
        )
        return ReviewsResponse(
            status=status,
            synced_at=seconds(synced_at),
            stale=view.stale,
            total=page.total,
            reviews=[_review_out(r) for r in page.reviews],
        )

    @router.post(
        "/api/v1/me/reviews/{review_id}/draft",
        response_model=DraftResponse,
        dependencies=[Depends(require_csrf_header)],
    )
    async def draft_reply(
        review_id: str,
        account: Account = Depends(current_account),  # noqa: B008
    ) -> DraftResponse:
        try:
            draft = await replies.draft(account.id, review_id_or_404(review_id))
        except ReviewNotFound:
            raise HTTPException(404, MSG_REVIEW_NOT_FOUND) from None
        except DraftRejected:
            raise HTTPException(422, MSG_DRAFT_REJECTED) from None
        except DraftRateLimited:
            raise HTTPException(429, MSG_DRAFT_RATE_LIMITED) from None
        except DraftUnavailable:
            raise HTTPException(503, MSG_DRAFT_UNAVAILABLE) from None
        return DraftResponse(review_id=draft.review_id, text=draft.text, cached=draft.cached)

    @router.post(
        "/api/v1/me/reviews/{review_id}/reply",
        response_model=ActionResponse,
        status_code=202,
        dependencies=[Depends(require_csrf_header)],
    )
    async def publish_reply(
        review_id: str,
        body: ReplyRequest,
        account: Account = Depends(current_account),  # noqa: B008
    ) -> ActionResponse:
        # This request IS the owner's confirmation: a session, the protection header
        # and the exact text. Nothing else in the product creates an action.
        review_id = review_id_or_404(review_id)
        try:
            action_id = await replies.confirm(account.id, review_id, body.text)
        except InvalidReplyText as exc:
            raise HTTPException(422, str(exc)) from None
        except ReviewNotFound:
            raise HTTPException(404, MSG_REVIEW_NOT_FOUND) from None
        except ReplyInProgress:
            raise HTTPException(409, MSG_REPLY_IN_PROGRESS) from None
        except Exception as exc:  # the reply must never reach the logs
            log.error("respuesta no registrada: %s", type(exc).__name__)
            raise HTTPException(503, MSG_REPLY_NOT_SAVED) from None
        try:
            action = await store.get_action(account.id, action_id)
        except Exception as exc:  # the action exists: answer with what is known
            log.warning("acción no leída tras crearla: %s", type(exc).__name__)
            action = None
        if action is not None:
            return _action_out(action)
        return ActionResponse(
            id=action_id,
            type="reply_review",
            review_id=review_id,
            status=status_label(PENDING),
            message=status_message(PENDING),
            error=None,
            confirmed_at=None,
            finished_at=None,
        )

    @router.get("/api/v1/me/actions/{action_id}", response_model=ActionResponse)
    async def my_action(
        action_id: str,
        account: Account = Depends(current_account),  # noqa: B008
    ) -> ActionResponse:
        action = None
        if ACTION_ID.fullmatch(action_id):
            # Looked up by the account of the session: another account's action does
            # not exist for this one.
            action = await store.get_action(account.id, action_id)
        if action is None:
            raise HTTPException(404, MSG_ACTION_NOT_FOUND)
        return _action_out(action)

    return router
