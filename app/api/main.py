import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from app.adapters.google_oauth.fake import FakeGoogleOAuth
from app.adapters.google_oauth.real import RealGoogleOAuth
from app.adapters.places.fake import FakePlaces
from app.adapters.places.google import GooglePlaces
from app.adapters.store.memory import MemoryStore
from app.adapters.store.postgres import PostgresStore
from app.api.google_oauth import build_router as build_google_router
from app.api.waitlist import SOURCE_PATTERN, build_router
from app.application.diagnosis_service import DiagnosisService, RateLimited
from app.application.rate_limit import SlidingWindowLimiter
from app.config import Settings
from app.ports.google_oauth import GoogleOAuth
from app.ports.places import PlacesGateway, PlacesUnavailable
from app.ports.store import Store

log = logging.getLogger("fichaitor.api")


class DiagnosisRequest(BaseModel):
    query: str = Field(min_length=3, max_length=120)
    source: str | None = Field(default=None, pattern=SOURCE_PATTERN)


class FindingOut(BaseModel):
    code: str
    title: str
    detail: str
    impact: str


class LockedCheckOut(BaseModel):
    code: str
    title: str


class DiagnosisResponse(BaseModel):
    place_id: str
    name: str
    address: str
    category: str
    score: int
    label: str
    findings: list[FindingOut]
    locked_checks: list[LockedCheckOut]
    maps_url: str


def _client_ip(request: Request, trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def create_app(
    settings: Settings | None = None,
    places: PlacesGateway | None = None,
    store: Store | None = None,
    google: GoogleOAuth | None = None,
) -> FastAPI:
    settings = settings or Settings()
    if google is None:
        if settings.fake_google:
            google = FakeGoogleOAuth()
        elif settings.google_client_id:
            if not (settings.google_client_secret and settings.google_redirect_uri):
                raise RuntimeError(
                    "Faltan FICHAITOR_GOOGLE_CLIENT_SECRET o FICHAITOR_GOOGLE_REDIRECT_URI"
                )
            google = RealGoogleOAuth(
                settings.google_client_id,
                settings.google_client_secret,
                settings.google_redirect_uri,
            )
    if google is not None:
        missing = [
            name
            for name, value in (
                ("FICHAITOR_OAUTH_STATE_SECRET", settings.oauth_state_secret),
                ("FICHAITOR_TOKEN_PUBLIC_KEY", settings.token_public_key),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"Variable vacía en el .env: {', '.join(missing)}. "
                "La clave pública se genera con scripts/gen_token_keys.py"
            )
    if store is None:
        if settings.database_url:
            store = PostgresStore(settings.database_url)
        elif settings.fake_places:
            store = MemoryStore()
        else:
            raise RuntimeError("Falta FICHAITOR_DATABASE_URL")
    if places is None:
        if settings.fake_places:
            places = FakePlaces()
        elif settings.places_api_key:
            places = GooglePlaces(settings.places_api_key)
        else:
            raise RuntimeError(
                "Falta FICHAITOR_PLACES_API_KEY (o FICHAITOR_FAKE_PLACES=true para desarrollo)"
            )
    service = DiagnosisService(
        places,
        rate_limit=settings.diagnosis_rate_limit,
        rate_window_seconds=settings.diagnosis_rate_window_seconds,
        cache_seconds=settings.diagnosis_cache_seconds,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if hasattr(store, "connect"):
            await store.connect()
        yield
        if hasattr(store, "close"):
            await store.close()

    app = FastAPI(title="Fichaitor API", version="0.1.0", lifespan=lifespan)

    def client_ip(request: Request) -> str:
        return _client_ip(request, settings.trust_proxy_headers)

    async def track(kind: str, source: str | None, place_id: str | None, score: int | None):
        try:
            await store.record_event(kind, source, place_id, score)
        except Exception as exc:  # measuring must never break the diagnosis
            log.warning("evento de embudo no registrado: %s", type(exc).__name__)

    app.include_router(
        build_router(
            store,
            SlidingWindowLimiter(
                settings.waitlist_rate_limit, settings.waitlist_rate_window_seconds
            ),
            client_ip,
        )
    )

    if google is not None:
        app.include_router(
            build_google_router(
                google=google,
                store=store,
                state_secret=settings.oauth_state_secret,
                token_public_key=settings.token_public_key,
                token_key_version=settings.token_key_version,
                post_connect_url=settings.post_connect_url,
                cookie_secure=settings.cookie_secure,
                limiter=SlidingWindowLimiter(settings.oauth_start_rate_limit, 3600),
                client_ip=client_ip,
            )
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/public/diagnosis", response_model=DiagnosisResponse)
    async def public_diagnosis(body: DiagnosisRequest, request: Request) -> DiagnosisResponse:
        client = _client_ip(request, settings.trust_proxy_headers)
        try:
            result = await service.run(body.query, client)
        except RateLimited as exc:
            raise HTTPException(
                429, "Has hecho muchas consultas. Prueba dentro de un rato."
            ) from exc
        except PlacesUnavailable as exc:
            raise HTTPException(
                503, "Google no responde ahora mismo. Prueba en un minuto."
            ) from exc
        if result is None:
            await track("not_found", body.source, None, None)
            raise HTTPException(
                404, "No encontramos ese negocio. Prueba con el nombre y la localidad."
            )
        await track("diagnosis", body.source, result.place_id, result.score)
        return DiagnosisResponse(
            place_id=result.place_id,
            name=result.name,
            address=result.address,
            category=result.category,
            score=result.score,
            label=result.label,
            findings=[
                FindingOut(code=f.code, title=f.title, detail=f.detail, impact=f.impact.value)
                for f in result.findings
            ],
            locked_checks=[
                LockedCheckOut(code=c.code, title=c.title) for c in result.locked_checks
            ],
            maps_url=result.maps_url,
        )

    return app
