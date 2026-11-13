"""POST /api/v1/webhooks/stripe: the events of Stripe (docs/admin-facturacion.md, 4.2).

1. The raw body is checked against `Stripe-Signature` with the signing secret (`whsec_…`)
   and a tolerance of 300 seconds. Wrong, missing or too old: 400, and nothing is kept.
2. An event of the other mode (live vs test, FICHAITOR_STRIPE_LIVEMODE): 200, discarded.
3. Only its ids are noted (`stripe_event`), never the payload, which carries names,
   addresses and emails; its processing is enqueued once per event id (a repeated event
   is a 200 that does nothing). The billing process reads the object again in Stripe.
4. 200 in milliseconds.

No session, no CSRF header: the signature is the only proof. Without the secret the
route is not mounted (404).
"""

import logging
import re
from datetime import UTC, datetime

import stripe
from fastapi import APIRouter, Request, Response

from app.ports.billing_store import BillingStore

log = logging.getLogger("fichaitor.billing.webhook")

ROUTE = "/api/v1/webhooks/stripe"
TOLERANCE_SECONDS = 300
MAX_BODY_BYTES = 256 * 1024  # Stripe's events are a few KB

_EVENT_ID = re.compile(r"evt_[A-Za-z0-9]{1,250}")
_TYPE = re.compile(r"[a-z_.]{1,100}")
_OBJECT_ID = re.compile(r"[a-z]{2,8}_[A-Za-z0-9_]{1,250}")


def build_router(*, billing: BillingStore, secret: str, livemode: bool) -> APIRouter:
    router = APIRouter()

    @router.post(ROUTE, include_in_schema=False)
    async def stripe_webhook(request: Request) -> Response:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return Response(status_code=413)
        payload = await request.body()
        if len(payload) > MAX_BODY_BYTES:
            return Response(status_code=413)
        signature = request.headers.get("stripe-signature", "")
        try:
            event = stripe.Webhook.construct_event(
                payload,
                signature,
                secret,
                tolerance=TOLERANCE_SECONDS,  # gitleaks:allow (nombres de variables)
            )
        except (ValueError, stripe.SignatureVerificationError):
            log.warning("webhook de Stripe con firma no válida")
            return Response(status_code=400)
        data = event.to_dict()
        event_id, type_ = data.get("id"), data.get("type")
        if not (
            isinstance(event_id, str)
            and _EVENT_ID.fullmatch(event_id)
            and isinstance(type_, str)
            and _TYPE.fullmatch(type_)
        ):
            return Response(status_code=400)
        if bool(data.get("livemode")) != livemode:
            log.warning("evento de Stripe de otro modo: se descarta")
            return Response(status_code=200)
        obj = (data.get("data") or {}).get("object") or {}
        object_id = obj.get("id") if isinstance(obj, dict) else None
        if not (isinstance(object_id, str) and _OBJECT_ID.fullmatch(object_id)):
            object_id = None
        created = data.get("created")
        created_at = datetime.fromtimestamp(int(created), UTC) if created else datetime.now(UTC)
        try:
            await billing.record_stripe_event(event_id, type_, object_id, livemode, created_at)
        except Exception as exc:  # Stripe retries for days: a 500 is safe
            log.error("evento de Stripe no guardado: %s", type(exc).__name__)
            return Response(status_code=500)
        return Response(status_code=200)

    return router
