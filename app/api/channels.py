"""Messaging channels: link a Telegram chat or a WhatsApp phone from the panel, list and
unlink the linked addresses, and receive the webhooks of Telegram and WhatsApp. Documented
in docs/contrato-web-api.md, "Canales de mensajería", and in docs/canales.md.

The API never talks to Telegram nor to Meta (docs/02-hld.md): a webhook checks the secret
(WhatsApp: the signature of the raw body with the app secret), reads the update and
enqueues it; the conversation process does the rest.

Multinegocio, step 7 (docs/multinegocio.md, 3.7 and 6.4): each chat sees every active
profile of the account or only those of its filter. The filter is chosen when the code
is asked for, copied to the chat when it is linked, and can be changed afterwards.
Deactivating a profile leaves the filters as they are: a chat filtered to it gets
nothing of it while it is inactive, and gets it again if it is activated again.
"""

import json
import logging
import re
from collections.abc import Callable
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.messaging import telegram, whatsapp
from app.api.session import (
    LOCATION_ID,
    MSG_LOCATION_INACTIVE,
    MSG_LOCATION_NOT_FOUND,
    account_guard,
    active_account_guard,
    require_csrf_header,
    utc,
)
from app.channels_config import ChannelsSettings
from app.domain import channel_jobs, link_codes
from app.domain.conversation import ChannelIdentity, masked_address
from app.ports.channel_links import ChannelLinks, LinkCodeTaken
from app.ports.job_queue import JobEnqueuer
from app.ports.quota import Quota
from app.ports.store import Account, LocationOverview, Store

log = logging.getLogger("fichaitor.channels")

LINK_CODE_WINDOW_SECONDS = 3600
WEBHOOK_MAX_BYTES = 64 * 1024
WEBHOOK_PATH = "/api/v1/webhooks/telegram"
WHATSAPP_WEBHOOK_PATH = "/api/v1/webhooks/whatsapp"
# Meta's deliveries of messages are small (one text is about 1 KB); Meta allows up to 3 MB,
# but nginx takes 64 KB at most on /api/v1/webhooks/ (web/nginx.conf). The same here.
WHATSAPP_WEBHOOK_MAX_BYTES = 64 * 1024
IDENTITY_ID = re.compile(r"chid_[0-9a-f]{8,32}")

MSG_LINK_CODE_LIMIT = "Has pedido muchos códigos. Prueba dentro de un rato."
MSG_LINK_CODE_UNAVAILABLE = "No hemos podido generar el código. Prueba en un minuto."
MSG_CHANNEL_NOT_FOUND = "Ese canal no está vinculado a tu cuenta."
LABEL_ALL = "Todos tus negocios"
LABEL_NONE = "Ninguno de tus negocios"
LABEL_INACTIVE = " (desactivada)"
MAX_FILTER = 50  # as the CHECK of location_filter (db/schema_channels.sql)


class LocationsFilter(BaseModel):
    """Which profiles a chat sees: None, every active profile of the account (also
    those activated later); a list, only those. Each one must be an active profile of
    the account. Required here: send null to see them all."""

    model_config = ConfigDict(extra="forbid")

    locations: list[str] | None = Field(min_length=1, max_length=MAX_FILTER)


class LinkCodeRequest(LocationsFilter):
    """The optional body of POST /channels/link-code: without `locations`, every
    profile."""

    locations: list[str] | None = Field(None, min_length=1, max_length=MAX_FILTER)


class LinkCodeResponse(BaseModel):
    code: str  # six digits, to type in the chat
    expires_at: datetime
    telegram_url: str  # https://t.me/<bot>?start=<token>: carries the token, never the code
    # https://wa.me/<number>?text=Vincular%20Fichaitor%20<token>, or None while WhatsApp is
    # not configured. Carries the token, never the code.
    whatsapp_url: str | None = None


class ChannelOut(BaseModel):
    id: str
    channel: str
    address: str  # masked, ready to show: "Telegram de ••••7342"
    linked_at: datetime
    # None: every active profile of the account. Otherwise the profiles of its filter
    # that the account still has, ordered as GET /me lists them.
    locations: list[str] | None
    # Ready to show: "Todos tus negocios", or the names ("Bar La Parra y La Parra
    # Getafe"), an inactive one with " (desactivada)".
    locations_label: str


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

    async def profiles(account: Account) -> list[LocationOverview]:
        overview = await store.locations_overview(account.id, utc(clock()).date())
        return list(overview.locations)

    async def checked_filter(
        account: Account, requested: list[str] | None
    ) -> tuple[str, ...] | None:
        """The filter as it is stored: None, or the ids without repetitions, each one an
        active profile of the account. 404 for one that is not of the account (or does
        not exist, or has another shape: the same answer); 422 for an inactive one."""
        if requested is None:
            return None
        ids = tuple(dict.fromkeys(requested))
        if not all(LOCATION_ID.fullmatch(i) for i in ids):
            raise HTTPException(404, MSG_LOCATION_NOT_FOUND)
        mine = {loc.id: loc for loc in await profiles(account)}
        if any(i not in mine for i in ids):
            raise HTTPException(404, MSG_LOCATION_NOT_FOUND)
        if any(not mine[i].active for i in ids):
            raise HTTPException(422, MSG_LOCATION_INACTIVE)
        return ids

    def channel_out(identity: ChannelIdentity, mine: list[LocationOverview]) -> ChannelOut:
        if identity.location_filter is None:
            locations, label = None, LABEL_ALL
        else:
            chosen = set(identity.location_filter)
            seen = [loc for loc in mine if loc.id in chosen]
            locations = [loc.id for loc in seen]
            label = names_label(
                [loc.title + ("" if loc.active else LABEL_INACTIVE) for loc in seen]
            )
        return ChannelOut(
            id=identity.id,
            channel=identity.channel,
            address=masked_address(identity.channel, identity.address),
            linked_at=identity.verified_at,
            locations=locations,
            locations_label=label,
        )

    @router.post(
        "/api/v1/channels/link-code",
        response_model=LinkCodeResponse,
        dependencies=[Depends(require_csrf_header)],
    )
    async def link_code(
        body: LinkCodeRequest | None = Body(None),  # noqa: B008
        account: Account = Depends(active_account),  # noqa: B008
    ) -> LinkCodeResponse:
        # Checked before the quota: a wrong filter does not spend a code.
        location_filter = await checked_filter(account, body.locations if body else None)
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
            issued = link_codes.issue_link(account.id, now, pepper, location_filter)
            try:
                await links.save_link_code(issued.record)
            except LinkCodeTaken:
                continue
            return LinkCodeResponse(
                code=issued.code,
                expires_at=issued.record.expires_at,
                telegram_url=settings.telegram_link(issued.token),
                whatsapp_url=settings.whatsapp_link(issued.token),
            )
        raise HTTPException(503, MSG_LINK_CODE_UNAVAILABLE)

    @router.get("/api/v1/me/channels", response_model=ChannelsResponse)
    async def my_channels(account: Account = Depends(current_account)) -> ChannelsResponse:  # noqa: B008
        found = await links.list_channels(account.id)
        filtered = any(c.location_filter is not None for c in found)
        mine = await profiles(account) if filtered else []
        return ChannelsResponse(channels=[channel_out(c, mine) for c in found])

    @router.put(
        "/api/v1/me/channels/{identity_id}/locations",
        response_model=ChannelOut,
        dependencies=[Depends(require_csrf_header)],
    )
    async def set_locations(
        identity_id: str,
        body: LocationsFilter,
        account: Account = Depends(active_account),  # noqa: B008
    ) -> ChannelOut:
        # The same 404 for a chat of another account, an unlinked one and a made-up id.
        if not IDENTITY_ID.fullmatch(identity_id):
            raise HTTPException(404, MSG_CHANNEL_NOT_FOUND)
        if not any(c.id == identity_id for c in await links.list_channels(account.id)):
            raise HTTPException(404, MSG_CHANNEL_NOT_FOUND)
        location_filter = await checked_filter(account, body.locations)
        changed = await links.set_channel_filter(account.id, identity_id, location_filter)
        if changed is None:  # unlinked, or a profile gone, a moment ago
            raise HTTPException(404, MSG_CHANNEL_NOT_FOUND)
        return channel_out(changed, await profiles(account))

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
    if settings.whatsapp_webhook_enabled:
        # Without the app secret, the verify token and the number, the routes do not exist.
        verify, receive = _whatsapp_webhook(
            settings.whatsapp_phone_number_id,
            settings.whatsapp_app_secret.get_secret_value(),
            settings.whatsapp_verify_token.get_secret_value(),
            jobs,
        )
        router.add_api_route(
            WHATSAPP_WEBHOOK_PATH, verify, methods=["GET"], include_in_schema=False
        )
        router.add_api_route(
            WHATSAPP_WEBHOOK_PATH, receive, methods=["POST"], include_in_schema=False
        )
    return router


def names_label(names: list[str]) -> str:
    """ "A", "A y B", "A, B y C". Without any, LABEL_NONE: every profile of the filter
    is gone (Google no longer lists it)."""
    if not names:
        return LABEL_NONE
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} y {names[-1]}"


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


def _whatsapp_webhook(phone_number_id: str, app_secret: str, verify_token: str, jobs: JobEnqueuer):
    async def whatsapp_verify(request: Request) -> Response:
        """Meta's check of the callback URL: echo `hub.challenge` if the token is ours."""
        params = request.query_params
        challenge = whatsapp.verify_subscription(
            params.get("hub.mode"),
            params.get("hub.verify_token"),
            params.get("hub.challenge"),
            verify_token,
        )
        if challenge is None:
            raise HTTPException(403, "Petición no permitida.")
        return PlainTextResponse(challenge)

    async def whatsapp_receive(request: Request) -> Response:
        # 1. A declared size over the cap is refused before reading anything.
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > WHATSAPP_WEBHOOK_MAX_BYTES:
            raise HTTPException(413, "Petición demasiado grande.")
        signature = request.headers.get(whatsapp.SIGNATURE_HEADER)
        if not signature or not signature.startswith("sha256="):
            raise HTTPException(403, "Petición no permitida.")
        # 2. The raw body, at most 64 KB, read as it arrives.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > WHATSAPP_WEBHOOK_MAX_BYTES:
                raise HTTPException(413, "Petición demasiado grande.")
        # 3. The signature of those exact bytes with the app secret, in constant time,
        #    before the body is parsed.
        if not whatsapp.verify_signature(bytes(body), signature, app_secret):
            raise HTTPException(403, "Petición no permitida.")
        try:
            payload = json.loads(bytes(body))
        except ValueError:
            return Response(status_code=200)  # nothing Meta could fix by retrying
        # 4. Statuses are only counted in the log (no ids, no numbers).
        for status, code in whatsapp.statuses(payload, phone_number_id):
            if status == "failed":
                log.warning("whatsapp: mensaje no entregado (código %s)", code)
        # 5. One job per message for our number. Meta delivers again what failed.
        for item in whatsapp.extract_messages(payload, phone_number_id):
            await jobs.enqueue(
                channel_jobs.CHANNEL_INBOUND,
                {"channel": whatsapp.CHANNEL, "update": item},
                idempotency_key=channel_jobs.whatsapp_inbound_key(whatsapp.message_id(item)),
                max_attempts=channel_jobs.INBOUND_MAX_ATTEMPTS,
            )
        return Response(status_code=200)

    return whatsapp_verify, whatsapp_receive
