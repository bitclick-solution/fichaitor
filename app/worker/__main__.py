"""Worker process: uv run python -m app.worker"""

import asyncio
import logging
import signal
import sys

import asyncpg
from pydantic import ValidationError

from app.adapters.google_profile.fake import FakeGoogleProfile
from app.adapters.google_profile.real import RealGoogleProfile
from app.adapters.job_queue.postgres import PostgresJobQueue
from app.adapters.sentiment_model.build import build_sentiment_model
from app.adapters.worker_store.postgres import PostgresWorkerStore
from app.domain.sentiment import SentimentPolicy
from app.ports.google_profile import GoogleProfile
from app.ports.job_queue import JobEnqueuer
from app.ports.sentiment_model import SentimentModel
from app.ports.worker_store import WorkerStore
from app.screenshot_config import ScreenshotSettings
from app.sentiment_config import SentimentSettings
from app.worker import analyze_sentiment, log, reply_review, sync_profile
from app.worker.config import ConfigError, WorkerSettings
from app.worker.loop import Worker
from app.worker.registry import HandlerRegistry


def build_registry(
    settings: WorkerSettings,
    store: WorkerStore,
    google: GoogleProfile,
    jobs: JobEnqueuer | None = None,
    sentiment_model: SentimentModel | None = None,
    sentiment_policy: SentimentPolicy | None = None,
) -> HandlerRegistry:
    """jobs: where a finished sync enqueues the sentiment analysis. Without it, no
    analysis is chained (the job kind is still registered)."""
    policy = sentiment_policy or SentimentPolicy()
    trigger = analyze_sentiment.SentimentTrigger(store, jobs, policy) if jobs is not None else None
    registry = HandlerRegistry()
    registry.register(
        sync_profile.KIND,
        sync_profile.SyncProfile(store, google, settings.token_private_key, sentiment=trigger),
    )
    registry.register(
        analyze_sentiment.KIND,
        analyze_sentiment.AnalyzeSentiment(store, sentiment_model, policy),
    )
    registry.register(
        reply_review.KIND,
        reply_review.ReplyReview(store, google, settings.token_private_key),
    )
    return registry


def install_stop_signals(stop: asyncio.Event) -> None:
    """First SIGINT or SIGTERM: finish the job in progress and leave."""
    loop = asyncio.get_running_loop()

    def request_stop(signum: int) -> None:
        log.event("stop_requested", signal=signal.Signals(signum).name)
        stop.set()

    # SIGBREAK (Ctrl+Break) exists only on Windows, where SIGTERM is never delivered.
    signals = [signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)]
    for signum in filter(None, signals):
        try:
            loop.add_signal_handler(signum, request_stop, signum)
        except NotImplementedError:  # Windows: no signal handlers on the event loop
            signal.signal(
                signum, lambda received, _frame: loop.call_soon_threadsafe(request_stop, received)
            )


async def run(settings: WorkerSettings) -> None:
    pool = await asyncpg.create_pool(settings.worker_database_url, min_size=1, max_size=3)
    real = None
    sentiment_model = None
    try:
        if settings.fake_google:
            google: GoogleProfile = FakeGoogleProfile()
            log.event("fake_google_enabled", logging.WARNING)
        else:
            google = real = RealGoogleProfile(
                settings.google_client_id, settings.google_client_secret
            )
        sentiment_settings = SentimentSettings()
        model_settings = ScreenshotSettings()
        sentiment_model = build_sentiment_model(model_settings, sentiment_settings.timeout_seconds)
        if sentiment_model is None:
            log.event("sentiment_model_missing", logging.WARNING)
        elif model_settings.fake_screenshot:
            log.event("fake_sentiment_model_enabled", logging.WARNING)
        queue = PostgresJobQueue(pool)
        worker = Worker(
            queue,
            build_registry(
                settings,
                PostgresWorkerStore(pool),
                google,
                jobs=queue,
                sentiment_model=sentiment_model,
                sentiment_policy=sentiment_settings.policy(),
            ),
            worker_id=settings.worker_id,
            poll_interval=settings.worker_poll_interval_seconds,
            job_timeout=settings.worker_job_timeout_seconds,
            lock_timeout=settings.worker_lock_timeout_seconds,
        )
        stop = asyncio.Event()
        install_stop_signals(stop)
        await worker.run(stop)
    finally:
        if real is not None:
            await real.close()
        if hasattr(sentiment_model, "close"):
            await sentiment_model.close()
        await pool.close()


def main() -> int:
    log.configure()
    try:
        settings = WorkerSettings()
        settings.check()
    except ValidationError as exc:
        # Only the names of the offending variables: the values may be secrets.
        names = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
        log.event("config_error", logging.ERROR, error=f"valores no válidos: {', '.join(names)}")
        return 2
    except ConfigError as exc:
        log.event("config_error", logging.ERROR, error=str(exc))
        return 2
    try:
        asyncio.run(run(settings))
    except (OSError, asyncpg.PostgresError) as exc:
        # Connection strings carry a password: log the kind of failure only.
        log.event("startup_error", logging.ERROR, error=type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
