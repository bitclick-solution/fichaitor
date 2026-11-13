import logging
import time
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.adapters.billing_store.memory import MemoryBillingStore
from app.adapters.billing_store.postgres import PostgresBillingStore
from app.adapters.calendar.loader import CalendarDataError, load_calendar
from app.adapters.google_oauth.fake import FakeGoogleOAuth
from app.adapters.google_oauth.real import RealGoogleOAuth
from app.adapters.google_profile.fake import demo_facts
from app.adapters.job_queue.demo import DemoSyncQueue
from app.adapters.job_queue.memory import MemoryJobQueue
from app.adapters.mailer.fake import FakeMailer
from app.adapters.places.fake import FakePlaces
from app.adapters.places.google import GooglePlaces
from app.adapters.quota.memory import MemoryQuota
from app.adapters.screenshot_reader.prompt import PROMPT_VERSION
from app.adapters.sentiment_model.build import build_sentiment_model
from app.adapters.store.memory import MemoryStore
from app.adapters.store.postgres import PostgresStore
from app.adapters.worker_store.memory import MemoryWorkerStore
from app.api.billing import build_router as build_billing_router
from app.api.channels import build_router as build_channels_router
from app.api.diagnosis_out import DiagnosisResponse, to_response
from app.api.google_oauth import build_router as build_google_router
from app.api.key_dates import build_router as build_key_dates_router
from app.api.locations import build_router as build_locations_router
from app.api.me import build_router as build_me_router
from app.api.offboarding import build_router as build_offboarding_router
from app.api.reviews import build_router as build_reviews_router
from app.api.screenshot import build_reader as build_reply_writer
from app.api.screenshot import build_router_from_settings as build_screenshot_router
from app.api.session import AccountBlocked, account_blocked_response
from app.api.visit import build_router as build_visit_router
from app.api.waitlist import SOURCE_PATTERN, build_router
from app.api.webhooks_stripe import build_router as build_stripe_webhook_router
from app.application.diagnosis_service import DailyLimitReached, DiagnosisService, RateLimited
from app.application.key_dates import KeyDates
from app.application.offboarding import Offboarding
from app.application.profile_sync import ProfileSync
from app.application.rate_limit import SlidingWindowLimiter
from app.application.review_replies import ReviewReplies
from app.channels_config import ChannelsSettings
from app.config import Settings
from app.domain.calendar import NATIONAL, Holiday
from app.domain.jobs import Job
from app.domain.offboarding import SEND_MAIL_KIND
from app.mail_config import MailSettings
from app.ports.billing_store import BillingStore
from app.ports.google_oauth import GoogleOAuth
from app.ports.job_queue import JobEnqueuer
from app.ports.places import PlacesGateway, PlacesUnavailable
from app.ports.quota import Quota
from app.ports.reply_writer import ReplyWriter
from app.ports.store import Store
from app.screenshot_config import ScreenshotSettings
from app.sentiment_config import SentimentSettings
from app.worker.analyze_sentiment import KIND as SENTIMENT_KIND
from app.worker.analyze_sentiment import AnalyzeSentiment
from app.worker.send_mail import SendMail

log = logging.getLogger("fichaitor.api")


class DiagnosisRequest(BaseModel):
    query: str = Field(min_length=3, max_length=120)
    source: str | None = Field(default=None, pattern=SOURCE_PATTERN)


def _client_ip(request: Request, trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class DemoProfileQueue(DemoSyncQueue):
    """The demo queue, which also stores the facts of the demo profile, as the worker
    does after a sync: without them the panel would have a summary and no diagnosis.
    Then it runs the sentiment analysis in this process, as the worker would."""

    def __init__(
        self,
        store: MemoryStore,
        clock: Callable[[], float],
        analyze: AnalyzeSentiment | None = None,
    ) -> None:
        super().__init__(store, clock)
        self._analyze = analyze

    async def enqueue(self, kind: str, payload: dict, **options) -> bool:
        created = await super().enqueue(kind, payload, **options)
        key = (payload.get("google_sub"), payload.get("location"))
        summary = self._store.summaries.get(key)
        if created and summary is not None and summary.facts is None:
            now = datetime.fromtimestamp(self._clock(), UTC)
            self._store.put_summary(*key, replace(summary, facts=demo_facts(now)))
        if created and summary is not None and self._analyze is not None:
            try:
                await self._analyze(Job("demo", SENTIMENT_KIND, dict(payload)))
            except Exception as exc:  # the demo must not break the route that enqueued
                log.warning("análisis de sentimiento de ejemplo fallido: %s", type(exc).__name__)
        return created


class InlineMail:
    """Without a database there is no worker to send the emails of a leave: with
    FICHAITOR_FAKE_MAIL they are "sent" right here (written to the log), so that the
    flow can be walked in a demo. Every other job goes to the queue it wraps."""

    def __init__(self, inner: JobEnqueuer, public_base_url: str) -> None:
        self._inner = inner
        self._send = SendMail(None, FakeMailer(), public_base_url)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def enqueue(self, kind: str, payload: dict, **options) -> bool:
        if kind != SEND_MAIL_KIND:
            return await self._inner.enqueue(kind, payload, **options)
        try:
            await self._send(Job("demo", kind, dict(payload)))
        except Exception as exc:  # the demo must not break the route that enqueued
            log.warning("correo de ejemplo no enviado: %s", type(exc).__name__)
        return True


class _DemoBar:
    """The in-memory worker store, for a demo profile whose category is "Bar"."""

    def __init__(self, store: MemoryWorkerStore) -> None:
        self._store = store

    def __getattr__(self, name: str):
        return getattr(self._store, name)

    async def sentiment_context(self, google_sub: str, location: str):
        self._store.categories.setdefault((google_sub, location), demo_facts().category)
        return await self._store.sentiment_context(google_sub, location)


def national_holidays() -> tuple[Holiday, ...]:
    """National holidays of the calendar data, for the diagnosis of connected profiles.

    The region of a business is not known yet, so regional holidays are left out.
    A missing or broken calendar does not stop the API: that check is skipped.
    """
    try:
        holidays = load_calendar().calendar.holidays
    except CalendarDataError as exc:
        log.error("calendario de festivos no disponible: %s", exc)
        return ()
    return tuple(h for h in holidays if h.scope == NATIONAL and h.verified)


def create_app(
    settings: Settings | None = None,
    places: PlacesGateway | None = None,
    store: Store | None = None,
    google: GoogleOAuth | None = None,
    clock: Callable[[], float] = time.time,
    jobs: JobEnqueuer | None = None,
    holidays: Sequence[Holiday] | None = None,
    reply_writer: ReplyWriter | None = None,
    screenshot_settings: ScreenshotSettings | None = None,
    sentiment_settings: SentimentSettings | None = None,
    mail_settings: MailSettings | None = None,
    channels_settings: ChannelsSettings | None = None,
    billing: BillingStore | None = None,
) -> FastAPI:
    settings = settings or Settings()
    screenshot_settings = screenshot_settings or ScreenshotSettings()
    sentiment_settings = sentiment_settings or SentimentSettings()
    sentiment_policy = sentiment_settings.policy()
    mail_settings = mail_settings or MailSettings()
    if holidays is None:
        holidays = national_holidays()
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
            store = PostgresStore(settings.database_url, allowance_pepper=settings.allowance_pepper)
        elif settings.fake_places:
            store = MemoryStore()
            store.allowance_pepper = settings.allowance_pepper
        else:
            raise RuntimeError("Falta FICHAITOR_DATABASE_URL")
    if jobs is None:
        if isinstance(store, PostgresStore):
            jobs = store  # enqueues on the API's own pool; the worker process does the job
        elif isinstance(store, MemoryStore) and settings.fake_google:
            # No database, no worker: demo data, and the sentiment analysis of the demo
            # profile (a bar) run right here on the tables the store reads.
            demo_worker = MemoryWorkerStore(tables=store.tables)
            jobs = DemoProfileQueue(
                store,
                clock,
                AnalyzeSentiment(
                    _DemoBar(demo_worker),
                    build_sentiment_model(screenshot_settings, sentiment_settings.timeout_seconds),
                    sentiment_policy,
                    lambda: datetime.fromtimestamp(clock(), UTC),
                ),
            )
        else:
            jobs = MemoryJobQueue()
    if isinstance(store, MemoryStore) and store.jobs is None:
        # Where a confirmed reply and the emails of a leave are enqueued; PostgreSQL
        # has `job`.
        store.jobs = (
            InlineMail(jobs, mail_settings.public_base_url) if mail_settings.fake_mail else jobs
        )
    if billing is None:
        # The paywall (docs/admin-facturacion.md): on the same database pool, or in memory
        # next to the in-memory store (tests and the demo).
        if isinstance(store, PostgresStore):
            billing = PostgresBillingStore(store)
        elif isinstance(store, MemoryStore):
            billing = MemoryBillingStore(store)
    sync = ProfileSync(
        store,
        jobs,
        window_seconds=settings.sync_window_seconds,
        stale_after_seconds=settings.summary_stale_hours * 3600,
        clock=clock,
    )
    if places is None:
        if settings.fake_places:
            places = FakePlaces()
        elif settings.places_api_key:
            places = GooglePlaces(settings.places_api_key)
        else:
            raise RuntimeError(
                "Falta FICHAITOR_PLACES_API_KEY (o FICHAITOR_FAKE_PLACES=true para desarrollo)"
            )
    # The caps on paid calls (public diagnoses and screenshots per day, drafts per
    # account) are kept by the store: in PostgreSQL they survive a restart and are
    # shared by every API process. The limits per client IP stay in memory.
    quota: Quota = store if hasattr(store, "take_quota") else MemoryQuota()
    service = DiagnosisService(
        places,
        rate_limit=settings.diagnosis_rate_limit,
        rate_window_seconds=settings.diagnosis_rate_window_seconds,
        place_id_cache_seconds=settings.place_id_cache_seconds,
        daily_limit=settings.diagnosis_daily_limit,
        quota=quota,
        clock=clock,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if hasattr(store, "connect"):
            await store.connect()
        yield
        if hasattr(store, "close"):
            await store.close()

    app = FastAPI(title="Fichaitor API", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(AccountBlocked)
    async def account_blocked(_: Request, __: AccountBlocked) -> JSONResponse:
        return account_blocked_response()

    def client_ip(request: Request) -> str:
        return _client_ip(request, settings.trust_proxy_headers)

    async def track(kind: str, source: str | None, place_id: str | None, score: int | None):
        # The place_id may be stored without limit. The score is ours but derived from
        # Google Maps content: whether it may be stored is an open question for Google
        # (docs/fase0/places-coste.md, section 5). Kept as it is until they answer.
        try:
            await store.record_event(kind, source, place_id, score)
        except Exception as exc:  # measuring must never break the diagnosis
            log.warning("evento de embudo no registrado: %s", type(exc).__name__)

    # Reviews screenshot demo. Without FICHAITOR_GEMINI_API_KEY and without
    # FICHAITOR_FAKE_SCREENSHOT the route is not mounted (404).
    # One model client for the demo and for the drafts of replies; None if not configured.
    model = build_reply_writer(screenshot_settings)
    screenshot_router = build_screenshot_router(
        client_ip, track, settings=screenshot_settings, reader=model, clock=clock, quota=quota
    )
    if screenshot_router is not None:
        app.include_router(screenshot_router)

    app.include_router(
        build_router(
            store,
            SlidingWindowLimiter(
                settings.waitlist_rate_limit, settings.waitlist_rate_window_seconds
            ),
            client_ip,
        )
    )

    async def record_visit(source: str | None) -> None:
        await store.record_event("visit", source)

    app.include_router(build_visit_router(record_visit, client_ip))

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
                clock=clock,
                sync=sync,
            )
        )

    # Always present: without a session they answer 401, which the web reads as "connect".
    app.include_router(
        build_me_router(
            store=store,
            sync=sync,
            holidays=holidays,
            cookie_secure=settings.cookie_secure,
            clock=clock,
            sentiment_policy=sentiment_policy,
            billing=billing,
        )
    )
    # Multinegocio (docs/multinegocio.md, 5.3): activate and deactivate a profile.
    app.include_router(
        build_locations_router(
            store=store,
            sync=sync,
            clock=clock,
            cookie_secure=settings.cookie_secure,
            billing=billing,
        )
    )
    # The paywall: fiscal profile, seats and the requests to the billing process, and the
    # webhook of Stripe (only with its signing secret).
    if billing is not None:
        app.include_router(
            build_billing_router(
                store=store,
                billing=billing,
                quota=quota,
                clock=clock,
                cookie_secure=settings.cookie_secure,
            )
        )
        if settings.stripe_webhook_secret:
            app.include_router(
                build_stripe_webhook_router(
                    billing=billing,
                    secret=settings.stripe_webhook_secret,
                    livemode=settings.stripe_livemode,
                )
            )

    # Reviews of the connected profile and their replies. The writer of drafts is the
    # one of the screenshot demo (Gemini, or the fake one with FICHAITOR_FAKE_SCREENSHOT).
    # Without either, drafts answer 503 and the owner writes the reply; listing,
    # publishing and following an action do not depend on it.
    if reply_writer is None:
        reply_writer = model
    if reply_writer is None:
        log.warning(
            "borradores de respuesta desactivados: falta FICHAITOR_GEMINI_API_KEY "
            "(o FICHAITOR_FAKE_SCREENSHOT=true para desarrollo)"
        )
    app.include_router(
        build_reviews_router(
            store=store,
            sync=sync,
            replies=ReviewReplies(
                store,
                reply_writer,
                draft_limit=settings.review_draft_rate_limit,
                draft_window_seconds=settings.review_draft_rate_window_seconds,
                draft_timeout_seconds=settings.review_draft_timeout_seconds,
                clock=clock,
                prompt_version=PROMPT_VERSION,
                quota=quota,
            ),
            clock=clock,
            cookie_secure=settings.cookie_secure,
        )
    )

    # Key dates (docs/calendario.md): the days Fichaitor asks about, answered from the
    # panel through the same action as from the chat.
    app.include_router(
        build_key_dates_router(
            store=store,
            key_dates=KeyDates(store, clock),
            clock=clock,
            cookie_secure=settings.cookie_secure,
        )
    )

    # Leaving: disconnect the profile or delete the account. The emails and the
    # leave itself are the worker's; without mail configured, asking for a code is a 503.
    app.include_router(
        build_offboarding_router(
            store=store,
            offboarding=Offboarding(
                store,
                secret=settings.offboarding_secret or settings.oauth_state_secret,
                mail_available=mail_settings.available,
                clock=clock,
                code_ttl_seconds=settings.offboarding_code_ttl_seconds,
                max_attempts=settings.offboarding_code_max_attempts,
                grace_hours=settings.offboarding_grace_hours,
                code_limit=settings.offboarding_code_rate_limit,
                code_window_seconds=settings.offboarding_code_rate_window_seconds,
            ),
            sync=sync,
            restore_limiter=SlidingWindowLimiter(
                settings.offboarding_restore_rate_limit, 3600, clock
            ),
            client_ip=client_ip,
            clock=clock,
            cookie_secure=settings.cookie_secure,
        )
    )

    # Messaging channels (docs/canales.md): link codes, linked addresses and, with
    # FICHAITOR_TELEGRAM_WEBHOOK_SECRET, Telegram's webhook.
    app.include_router(
        build_channels_router(
            store=store,
            links=store,
            jobs=jobs,
            quota=quota,
            settings=channels_settings or ChannelsSettings(),
            clock=clock,
            cookie_secure=settings.cookie_secure,
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
        except DailyLimitReached as exc:
            raise HTTPException(
                503, "Hoy hemos recibido muchas consultas y no podemos hacer más. Prueba mañana."
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
        return to_response(result)

    return app
