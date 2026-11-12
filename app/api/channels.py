"""Messaging channels: link a Telegram chat from the panel, list and unlink the linked
addresses, and receive Telegram's webhook. Documented in docs/contrato-web-api.md,
"Canales de mensajería", and in docs/canales.md.

The API never talks to Telegram (docs/02-hld.md): the webhook checks the secret,
reads the update and enqueues it; the conversation process does the rest.
"""

import json
import logging
import re
from collections.abc import Callable
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.adapters.messaging import telegram
from app.api.session import account_guard, active_account_guard, require_csrf_header, utc
from app.channels_config import ChannelsSettings
from app.domain import channel_jobs, link_codes
from app.domain.conversation import masked_address
from app.ports.channel_links import ChannelLinks, LinkCodeTaken
from app.ports.job_queue import JobEnqueuer
from app.ports.quota import Quota
from app.ports.store import Account, Store

log = logging.getLogger("fichaitor.channels")

LINK_CODE_WINDOW_SECONDS = 3600
WEBHOOK_MAX_BYTES = 64 * 1024
WEBHOOK_PATH = "/api/v1/webhooks/telegram"
IDENTITY_ID = re.compile(r"chid_[0-9a-f]{8,32}")

MSG_LINK_CODE_LIMIT = "Has pedido muchos códigos. Prueba dentro de un rato."
MSG_LINK_CODE_UNAVAILABLE = "No hemos podido generar el código. Prueba en un minuto."
MSG_CHANNEL_NOT_FOUND = "Ese canal no está vinculado a tu cuenta."


class LinkCodeResponse(BaseModel):
    code: str  # six digits, to type in the chat
    expires_at: datetime
    telegram_url: str  # https://t.me/<bot>?start=<token>: carries the token, never the code
    whatsapp_url: str | None = None  # no WhatsApp adapter yet


class ChannelOut(BaseModel):
    id: str
    channel: str
    address: str  # masked, ready to show: "Telegram de ••••7342"
    linked_at: datetime


class ChannelsResponse(BaseModel):
    channels: list[ChannelOut]


def build_router(
    *,
    store: Store,
    links: ChannelLinks,
    jobs: JobEnqueuer,
    quota: Quota,
    settings: ChannelsSettings,
    clock: Callable[[], float],
    cookie_secure: bool = True,
) -> APIRouter:
    router = APIRouter()
    current_account = account_guard(store, clock, cookie_secure)
    # A new code is closed while a leave is scheduled, like every route of the profile.
    active_account = active_account_guard(store, clock, cookie_secure)
    pepper = settings.link_code_pepper.get_secret_value()

    @router.post(
        "/api/v1/channels/link-code",
        response_model=LinkCodeResponse,
        dependencies=[Depends(require_csrf_header)],
    )
    async def link_code(account: Account = Depends(active_account)) -> LinkCodeResponse:  # noqa: B008
        now = utc(clock())
        try:
            allowed = await quota.take_quota(
                f"link_code:{account.id}",
                settings.link_code_rate_limit,
                LINK_CODE_WINDOW_SECONDS,
                now,
            )
        except Exception as exc:  # no code without a working limit
            log.warning("tope de códigos de vinculación no disponible: %s", type(exc).__name__)
            raise HTTPException(503, MSG_LINK_CODE_UNAVAILABLE) from None
        if not allowed:
            raise HTTPException(429, MSG_LINK_CODE_LIMIT)
        # Six digits collide now and then with a stored code: draw again.
        for _ in range(5):
            issued = link_codes.issue_link(account.id, now, pepper)
            try:
                await links.save_link_code(issued.record)
            except LinkCodeTaken:
                continue
            return LinkCodeResponse(
                code=issued.code,
                expires_at=issued.record.expires_at,
                telegram_url=settings.telegram_link(issued.token),
            )
        raise HTTPException(503, MSG_LINK_CODE_UNAVAILABLE)

    @router.get("/api/v1/me/channels", response_model=ChannelsResponse)
    async def my_channels(account: Account = Depends(current_account)) -> ChannelsResponse:  # noqa: B008
        found = await links.list_channels(account.id)
        return ChannelsResponse(
            channels=[
                ChannelOut(
                    id=c.id,
                    channel=c.channel,
                    address=masked_address(c.channel, c.address),
                    linked_at=c.verified_at,
                )
                for c in found
            ]
        )

    @router.delete(
        "/api/v1/me/channels/{identity_id}",
        status_code=204,
        dependencies=[Depends(require_csrf_header)],
    )
    async def unlink(identity_id: str, account: Account = Depends(current_account)) -> Response:  # noqa: B008
        if not IDENTITY_ID.fullmatch(identity_id):
            raise HTTPException(404, MSG_CHANNEL_NOT_FOUND)
        if not await links.unlink_channel(account.id, identity_id, utc(clock())):
            raise HTTPException(404, MSG_CHANNEL_NOT_FOUND)
        return Response(status_code=204)

    secret = settings.telegram_webhook_secret.get_secret_value()
    if secret:
        router.add_api_route(
            WEBHOOK_PATH,
            _webhook(secret, jobs),
            methods=["POST"],
            include_in_schema=False,
        )
    return router


def _webhook(secret: str, jobs: JobEnqueuer):
    async def telegram_webhook(request: Request) -> Response:
        # 1. The secret, in constant time, before reading anything else.
        if not telegram.verify_webhook_secret(request.headers.get(telegram.SECRET_HEADER), secret):
            raise HTTPException(403, "Petición no permitida.")
        # 2. At most 64 KB, read as it arrives: a bigger body is refused unread.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > WEBHOOK_MAX_BYTES:
                raise HTTPException(413, "Petición demasiado grande.")
        try:
            update = json.loads(bytes(body))
        except ValueError:
            return Response(status_code=200)  # nothing Telegram could fix by retrying
        # 3. Only what the conversation attends. The rest is answered and dropped.
        if telegram.parse_update(update) is None:
            return Response(status_code=200)
        # 4. Enqueued once per update: Telegram delivers it again if this fails.
        await jobs.enqueue(
            channel_jobs.CHANNEL_INBOUND,
            {"channel": telegram.CHANNEL, "update": update},
            idempotency_key=channel_jobs.inbound_key(telegram.CHANNEL, update["update_id"]),
            max_attempts=channel_jobs.INBOUND_MAX_ATTEMPTS,
        )
        return Response(status_code=200)

    return telegram_webhook
