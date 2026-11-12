"""Public demo from a screenshot of the reviews tab of the owner's Google listing.

It answers how many of the visible reviews are unanswered and proposes a reply for one
of them. The names of the people who wrote the reviews are never returned.

The upload is parsed in memory: Starlette's default form parser moves any file over
1 MB to a temporary file on disk, and the image must never touch the disk.
"""

import logging
import re
import time
from collections.abc import AsyncGenerator, Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.adapters.screenshot_reader.fake import FakeScreenshotReader
from app.adapters.screenshot_reader.gemini import GeminiScreenshotReader
from app.api.waitlist import SOURCE_PATTERN
from app.application.screenshot_service import (
    DailyLimitReached,
    ImageTooLarge,
    RateLimited,
    ScreenshotResult,
    ScreenshotService,
    UnsupportedImage,
)
from app.domain.screenshot_diagnosis import (
    SCREENSHOT_ATTRIBUTION,
    SCREENSHOT_LOCKED_CHECKS,
    NoReviewsVisible,
    NotABusinessProfile,
    NotReviewsTab,
)
from app.ports.quota import Quota
from app.ports.screenshot_reader import ScreenshotReader, ScreenshotReaderUnavailable
from app.screenshot_config import ScreenshotSettings

log = logging.getLogger("fichaitor.screenshot")

ROUTE = "/api/v1/public/diagnosis/screenshot"
# Room for the multipart boundaries, the part headers and the `source` field.
FORM_OVERHEAD_BYTES = 16 * 1024
_SOURCE = re.compile(SOURCE_PATTERN)

MSG_HOW_TO = (
    "Abre tu negocio en Google Maps, toca en «Reseñas» y haz una captura donde se vean las reseñas."
)
MSG_NOT_A_PROFILE = f"Eso no parece la ficha de un negocio en Google. {MSG_HOW_TO}"
MSG_NOT_REVIEWS = (
    f"Esa captura es de tu ficha, pero no de sus reseñas, que es lo que necesitamos. {MSG_HOW_TO}"
)
MSG_NO_REVIEWS = (
    "No distinguimos ninguna reseña en la captura. Haz otra donde se vea entera al menos "
    "una reseña, con sus estrellas."
)
MSG_UNSUPPORTED = (
    "Ese archivo no es una imagen que podamos leer. Envía una captura en PNG, JPEG o WebP."
)
MSG_MISSING = "Falta la captura. Envía la imagen en el campo «image»."
MSG_BAD_SOURCE = "El origen indicado no es válido."
MSG_RATE_LIMITED = "Has enviado muchas capturas. Prueba dentro de un rato."
MSG_DAILY_LIMIT = "Hoy hemos recibido muchas capturas y no podemos leer más. Prueba mañana."
MSG_UNAVAILABLE = "No hemos podido leer la captura ahora mismo. Prueba en un minuto."

# (kind, source, place_id, score). The same signature as `track` in app.api.main.
Track = Callable[[str, str | None, str | None, int | None], Awaitable[None]]


class LockedCheckOut(BaseModel):
    code: str
    title: str


class SuggestedReplyOut(BaseModel):
    stars: int
    review_excerpt: str
    reply: str


class ScreenshotReviewsResponse(BaseModel):
    business_name: str | None
    reviews_seen: int
    unanswered: int
    unanswered_low: int
    headline: str
    suggested_reply: SuggestedReplyOut | None
    locked_checks: list[LockedCheckOut]
    attribution: str


def to_response(result: ScreenshotResult) -> ScreenshotReviewsResponse:
    summary, suggested = result.summary, result.suggested_reply
    return ScreenshotReviewsResponse(
        business_name=summary.business_name,
        reviews_seen=summary.reviews_seen,
        unanswered=summary.unanswered,
        unanswered_low=summary.unanswered_low,
        headline=summary.headline,
        suggested_reply=(
            None
            if suggested is None
            else SuggestedReplyOut(
                stars=suggested.stars,
                review_excerpt=suggested.review_excerpt,
                reply=suggested.reply,
            )
        ),
        locked_checks=[
            LockedCheckOut(code=c.code, title=c.title) for c in SCREENSHOT_LOCKED_CHECKS
        ],
        attribution=SCREENSHOT_ATTRIBUTION,
    )


def _megabytes(size: int) -> str:
    return f"{size / 1_000_000:.1f}".rstrip("0").rstrip(".").replace(".", ",")


class _BodyTooLarge(Exception):
    pass


class _InMemoryMultiPartParser(MultiPartParser):
    def __init__(self, request: Request, stream: AsyncGenerator[bytes, None], keep_in_memory: int):
        # One file and one text field (`source`, 60 characters at most).
        super().__init__(request.headers, stream, max_files=1, max_fields=1, max_part_size=1024)
        # Past this size Starlette would roll the upload over to a temporary file on disk.
        self.spool_max_size = keep_in_memory


async def _capped(request: Request, limit: int) -> AsyncGenerator[bytes, None]:
    seen = 0
    async for chunk in request.stream():
        seen += len(chunk)
        if seen > limit:
            raise _BodyTooLarge
        yield chunk


async def _read_form(request: Request, max_bytes: int) -> tuple[bytes, str | None]:
    """Return the image bytes and the `source` field. Nothing is written to disk."""
    too_large = HTTPException(
        413, f"La captura pesa demasiado. El máximo son {_megabytes(max_bytes)} MB."
    )
    body_limit = max_bytes + FORM_OVERHEAD_BYTES
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > body_limit:
        raise too_large  # rejected before reading a single byte
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        raise HTTPException(422, MSG_MISSING)
    parser = _InMemoryMultiPartParser(request, _capped(request, body_limit), body_limit + 1)
    try:
        form = await parser.parse()
    except _BodyTooLarge:
        raise too_large from None
    except MultiPartException:
        raise HTTPException(422, MSG_MISSING) from None
    try:
        upload = form.get("image")
        if not isinstance(upload, UploadFile):
            raise HTTPException(422, MSG_MISSING)
        source = form.get("source")
        if source is not None and (not isinstance(source, str) or not _SOURCE.fullmatch(source)):
            raise HTTPException(422, MSG_BAD_SOURCE)
        image = await upload.read()
    finally:
        await form.close()
    if not image:
        raise HTTPException(422, MSG_MISSING)
    if len(image) > max_bytes:
        raise too_large
    return image, source


def build_router(
    service: ScreenshotService,
    client_ip: Callable[[Request], str],
    track: Track | None = None,
) -> APIRouter:
    router = APIRouter()

    async def record(kind: str, source: str | None, score: int | None) -> None:
        if track is None:
            return
        try:
            await track(kind, source, None, score)
        except Exception as exc:  # measuring must never break the diagnosis
            log.warning("evento de embudo no registrado: %s", type(exc).__name__)

    @router.post(
        ROUTE,
        response_model=ScreenshotReviewsResponse,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "multipart/form-data": {
                        "schema": {
                            "type": "object",
                            "required": ["image"],
                            "properties": {
                                "image": {"type": "string", "format": "binary"},
                                "source": {"type": "string", "pattern": SOURCE_PATTERN},
                            },
                        }
                    }
                },
            }
        },
    )
    async def screenshot_diagnosis(request: Request) -> ScreenshotReviewsResponse:
        # The limits are asked, without counting, before a byte of the upload is read:
        # a client over its limit does not get to fill the memory of the API.
        try:
            await service.precheck(client_ip(request))
        except RateLimited as exc:
            raise HTTPException(429, MSG_RATE_LIMITED) from exc
        except DailyLimitReached as exc:
            raise HTTPException(503, MSG_DAILY_LIMIT) from exc
        except ScreenshotReaderUnavailable:
            raise HTTPException(503, MSG_UNAVAILABLE) from None
        image, source = await _read_form(request, service.max_bytes)
        try:
            result = await service.run(image, client_ip(request))
        except ImageTooLarge as exc:
            raise HTTPException(
                413,
                f"La captura pesa demasiado. El máximo son {_megabytes(service.max_bytes)} MB.",
            ) from exc
        except UnsupportedImage as exc:
            raise HTTPException(415, MSG_UNSUPPORTED) from exc
        except RateLimited as exc:
            raise HTTPException(429, MSG_RATE_LIMITED) from exc
        except DailyLimitReached as exc:
            raise HTTPException(503, MSG_DAILY_LIMIT) from exc
        except ScreenshotReaderUnavailable:
            # No cause attached: nothing about the provider reaches the response.
            raise HTTPException(503, MSG_UNAVAILABLE) from None
        except NotABusinessProfile as exc:
            await record("screenshot_rejected", source, None)
            raise HTTPException(422, MSG_NOT_A_PROFILE) from exc
        except NotReviewsTab as exc:
            await record("screenshot_rejected", source, None)
            raise HTTPException(422, MSG_NOT_REVIEWS) from exc
        except NoReviewsVisible as exc:
            await record("screenshot_rejected", source, None)
            raise HTTPException(422, MSG_NO_REVIEWS) from exc
        finally:
            del image  # nothing keeps the capture once the answer is ready
        # No score and no place: the event only counts that a capture was read.
        await record("screenshot_diagnosis", source, None)
        return to_response(result)

    return router


def build_reader(settings: ScreenshotSettings) -> ScreenshotReader | None:
    """The fake reader, the real one, or None when the feature is not configured."""
    if settings.fake_screenshot:
        return FakeScreenshotReader()
    api_key = settings.gemini_api_key.get_secret_value()
    if api_key:
        return GeminiScreenshotReader(
            api_key, settings.gemini_model, settings.screenshot_timeout_seconds
        )
    return None


def build_router_from_settings(
    client_ip: Callable[[Request], str],
    track: Track | None = None,
    settings: ScreenshotSettings | None = None,
    reader: ScreenshotReader | None = None,
    clock: Callable[[], float] = time.time,
    quota: Quota | None = None,
) -> APIRouter | None:
    """Router ready to mount, or None: without a key and without the fake mode the route
    does not exist (404). The rest of the API does not depend on this feature, so a
    missing key must not stop it from starting."""
    settings = settings or ScreenshotSettings()
    reader = reader or build_reader(settings)
    if reader is None:
        log.warning(
            "diagnóstico por pantallazo desactivado: falta FICHAITOR_GEMINI_API_KEY "
            "(o FICHAITOR_FAKE_SCREENSHOT=true para desarrollo)"
        )
        return None
    service = ScreenshotService(
        reader,
        max_bytes=settings.screenshot_max_bytes,
        rate_limit=settings.screenshot_rate_limit,
        rate_window_seconds=settings.screenshot_rate_window_seconds,
        daily_limit=settings.screenshot_daily_limit,
        timeout_seconds=settings.screenshot_timeout_seconds,
        min_confidence=settings.screenshot_min_confidence,
        clock=clock,
        quota=quota,
    )
    return build_router(service, client_ip, track)
