"""Communications by email from the backoffice (docs/admin-facturacion.md, annex
«Comunicaciones por correo desde el backoffice»; contract in docs/contrato-backoffice.md,
section 10).

Write a draft → preview it → send the test to your own address → see how many will get
it → confirm with a fresh TOTP → the worker sends, at a limited pace. Owner role only.
The database functions (admin_communication_*) check the session, the role, the fresh
TOTP of the confirmation and the audience, and audit every step; the process validates
the text first (app/domain/communication.py) to give a precise message, and renders the
preview with the same code as the worker. No route returns an email address of a client.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.admin.auth import AuthKit
from app.admin.pagination import MAX_LIMIT, decode, page
from app.admin.routes_screens import UUID_PATTERN, _uuid
from app.admin.session import Actor, current_actor
from app.admin.store import AdminError, AdminStore
from app.domain import communication, unsubscribe

# The preview is not an email to anyone: its links point to the real site, and the
# opt-out link carries the token of the test email, which opts out nobody.
PREVIEW_BASE_URL = "https://fichaitor.es"
PREVIEW_TO = "vista-previa@fichaitor.es"

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=1000)]


class CommunicationIn(BaseModel):
    kind: Literal["servicio", "comercial"]
    subject: str = Field(max_length=communication.MAX_SUBJECT + 50)
    preheader: str = Field(max_length=communication.MAX_PREHEADER + 50)
    headline: str = Field(max_length=communication.MAX_HEADLINE + 50)
    paragraphs: list[str] = Field(min_length=1, max_length=communication.MAX_PARAGRAPHS)
    button_label: str | None = Field(default=None, max_length=communication.MAX_BUTTON_LABEL + 50)
    button_url: str | None = Field(default=None, max_length=communication.MAX_BUTTON_URL)
    audience: Literal["todas", "beta", "plan", "cuenta"]
    audience_plan: Literal["gratis", "pro", "pro_whatsapp"] | None = None
    audience_account: str | None = Field(default=None, pattern=UUID_PATTERN)


class ConfirmIn(BaseModel):
    # The number of recipients shown to the person (GET .../audience). If it changed in
    # the meantime, the database refuses and the screen shows the new one.
    expected_recipients: int = Field(ge=1, le=1_000_000)


def checked(body: CommunicationIn) -> dict:
    """The fields for admin_communication_save, with the text checked against the rules
    of LEEME.md, 12 (422 `invalid` with the field, never the value)."""
    try:
        content = communication.validate(
            communication.Content(
                kind=body.kind,
                subject=body.subject,
                preheader=body.preheader,
                headline=body.headline,
                paragraphs=tuple(body.paragraphs),
                button_label=body.button_label,
                button_url=body.button_url,
            )
        )
    except communication.CommunicationInvalid as exc:
        raise AdminError(422, "invalid", exc.message) from None
    if body.audience == "plan" and body.audience_plan is None:
        raise AdminError(422, "invalid", "Elige el plan: gratis, pro o pro_whatsapp.")
    if body.audience == "cuenta" and body.audience_account is None:
        raise AdminError(422, "invalid", "Falta el id de la cuenta.")
    return {
        "kind": content.kind,
        "subject": content.subject,
        "preheader": content.preheader,
        "headline": content.headline,
        "paragraphs": content.paragraphs,
        "button_label": content.button_label,
        "button_url": content.button_url,
        "audience": body.audience,
        "audience_plan": body.audience_plan if body.audience == "plan" else None,
        "audience_account": body.audience_account if body.audience == "cuenta" else None,
    }


def render_preview(row: dict) -> dict:
    """The email as a client of that communication would get it: subject, preheader,
    HTML, plain text and, for a commercial one, its opt-out headers."""
    content = communication.Content.from_row(row)
    headers: tuple[tuple[str, str], ...] = ()
    link = None
    if content.kind == communication.COMMERCIAL:
        link = unsubscribe.page_url(PREVIEW_BASE_URL, unsubscribe.TEST_TOKEN)
        headers = unsubscribe.headers(PREVIEW_BASE_URL, unsubscribe.TEST_TOKEN)
    email = communication.communication_email(PREVIEW_TO, content, link)
    return {
        "subject": email.subject,
        "preheader": content.preheader,
        "html": email.html,
        "text": email.text,
        "headers": [{"name": n, "value": v} for n, v in headers],
    }


def build_router(kit: AuthKit, store: AdminStore, per_minute: int) -> APIRouter:
    router = APIRouter(prefix="/admin/api")

    async def actor(request: Request) -> Actor:
        return await current_actor(request, kit, store)

    @router.get("/communications")
    async def communications(request: Request, limit: Limit = 50, cursor: Cursor = None) -> dict:
        me = await actor(request)
        rows = await store.communications(me.hash, limit + 1, decode(cursor))
        shown, nxt = page(rows, limit, lambda r: (r["created_at"], r["communication_id"]))
        return {"communications": shown, "next_cursor": nxt, "per_minute": per_minute}

    @router.post("/communications", status_code=201)
    async def create(request: Request, body: CommunicationIn) -> dict:
        me = await actor(request)
        saved = await store.communication_save(me.hash, None, checked(body))
        return {"communication_id": saved}

    @router.put("/communications/{communication_id}")
    async def update(request: Request, communication_id: str, body: CommunicationIn) -> dict:
        me = await actor(request)
        _uuid(communication_id)
        saved = await store.communication_save(me.hash, communication_id, checked(body))
        return {"communication_id": saved}

    @router.get("/communications/{communication_id}")
    async def detail(request: Request, communication_id: str) -> dict:
        me = await actor(request)
        _uuid(communication_id)
        return await store.communication_get(me.hash, communication_id)

    @router.get("/communications/{communication_id}/preview")
    async def preview(request: Request, communication_id: str) -> dict:
        me = await actor(request)
        _uuid(communication_id)
        return render_preview(await store.communication_get(me.hash, communication_id))

    @router.get("/communications/{communication_id}/preview.html")
    async def preview_html(request: Request, communication_id: str) -> Response:
        """The HTML alone, for an <iframe sandbox> of the screen. Served with its own CSP
        (inline styles, nothing else; see PREVIEW_CSP in app/admin/session.py)."""
        me = await actor(request)
        _uuid(communication_id)
        shown = render_preview(await store.communication_get(me.hash, communication_id))
        return Response(shown["html"], media_type="text/html; charset=utf-8")

    @router.get("/communications/{communication_id}/audience")
    async def audience(request: Request, communication_id: str) -> dict:
        me = await actor(request)
        _uuid(communication_id)
        counts = await store.communication_audience(me.hash, communication_id)
        return {**counts, "per_minute": per_minute}

    @router.post("/communications/{communication_id}/test", status_code=202)
    async def test(request: Request, communication_id: str) -> dict:
        me = await actor(request)
        _uuid(communication_id)
        return await store.communication_test(me.hash, communication_id)

    @router.post("/communications/{communication_id}/confirm", status_code=202)
    async def confirm(request: Request, communication_id: str, body: ConfirmIn) -> dict:
        me = await actor(request)
        _uuid(communication_id)
        return await store.communication_confirm(
            me.hash, communication_id, body.expected_recipients, per_minute
        )

    return router
