"""The conversation process: uv run python -m app.worker.conversation

Option A of docs/canales.md ("Paso 0"): the conversation runs apart from the worker that
writes to Google, with its own database user (`fichaitor_conversation`).

- It talks to the owner (Telegram) and, when the owner confirms, creates the action the
  same way the panel does (`ReviewReplyActions` -> `ReviewReplies.confirm`).
- It never has the private key of the tokens and cannot read `google_connection`: it
  can create an action but never carry it out. The executor of the worker does, after
  checking that the account, its Google user, the profile and the review agree (M1).
- It takes only the jobs of the channels (app/domain/channel_jobs.py) and runs the
  planner (app/worker/channel_planner.py).
- With FICHAITOR_TELEGRAM_POLLING=true it receives by long polling (local development,
  no public URL); otherwise by the API's webhook.

`--set-webhook <url>` registers the webhook of the bot with the secret and leaves: it is
run once per deployment, by hand (docs/despliegue.md).
"""

import asyncio
import logging
import os
import socket
import sys
import time
from datetime import UTC, datetime

import asyncpg
import httpx
from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.adapters.calendar.loader import CalendarDataError, load_calendar
from app.adapters.conversation_store.postgres import PostgresConversationStore
from app.adapters.job_queue.postgres import PostgresJobQueue
from app.adapters.messaging import telegram
from app.adapters.messaging.offline import OfflineChannel
from app.adapters.proposal_actions.review_replies import ReviewReplyActions
from app.adapters.proposal_actions.special_hours import (
    ProposalActionsByType,
    SpecialHoursActions,
)
from app.adapters.screenshot_reader.prompt import PROMPT_VERSION
from app.adapters.store.postgres import PostgresStore
from app.application.conversation_service import ConversationService
from app.application.review_replies import ReviewReplies
from app.channels_config import ChannelsSettings
from app.config import Settings
from app.domain.actions import REPLY_REVIEW, SET_SPECIAL_HOURS
from app.domain.calendar import Calendar
from app.domain.key_dates import DEFAULT_LEAD_DAYS, MAX_LEAD_DAYS
from app.ports.messaging import MessagingChannel, MessagingError
from app.ports.reply_writer import ReplyWriter
from app.screenshot_config import ScreenshotSettings
from app.worker import (
    channel_action_result,
    channel_inbound,
    conversation_tick,
    log,
    plan_key_dates,
    propose_review,
)
from app.worker.__main__ import install_stop_signals
from app.worker.channel_planner import ChannelPlanner
from app.worker.config import ConfigError
from app.worker.loop import Worker
from app.worker.registry import HandlerRegistry


def _default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-conversation"


class ConversationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FICHAITOR_", env_file=".env", extra="ignore")

    conversation_database_url: str = ""
    # Read only to refuse to start with it: this process must never have it.
    token_private_key: str = ""

    worker_poll_interval_seconds: float = Field(default=2.0, gt=0)
    worker_job_timeout_seconds: float = Field(default=30.0, gt=0)
    worker_lock_timeout_seconds: float = Field(default=120.0, gt=0)
    worker_id: str = Field(default_factory=_default_worker_id)
    planner_interval_seconds: float = Field(default=60.0, gt=0)
    # Key dates (docs/calendario.md): ask about the hours of holidays and of Nochebuena
    # and Nochevieja, and how many days before a holiday.
    key_dates_enabled: bool = True
    key_dates_lead_days: int = Field(default=DEFAULT_LEAD_DAYS, ge=1, le=MAX_LEAD_DAYS)

    def check(self) -> None:
        problems: list[str] = []
        if not self.conversation_database_url:
            problems.append("falta FICHAITOR_CONVERSATION_DATABASE_URL")
        if self.token_private_key:
            problems.append(
                "FICHAITOR_TOKEN_PRIVATE_KEY no puede llegar al proceso de la conversación"
            )
        if self.worker_job_timeout_seconds >= self.worker_lock_timeout_seconds:
            problems.append(
                "FICHAITOR_WORKER_JOB_TIMEOUT_SECONDS debe ser menor que "
                "FICHAITOR_WORKER_LOCK_TIMEOUT_SECONDS"
            )
        if problems:
            raise ConfigError("; ".join(problems))


def build_writer(settings: ScreenshotSettings) -> ReplyWriter | None:
    """The model that drafts the replies, as in the panel: the fake one, Gemini, or none
    (then no review is proposed by the channel: they stay in the panel)."""
    if settings.fake_screenshot:
        from app.adapters.screenshot_reader.fake import FakeScreenshotReader

        return FakeScreenshotReader()
    from app.adapters.gemini_provider import gemini_credentials

    credentials = gemini_credentials(settings)  # AI Studio or Vertex AI
    if credentials:
        from app.adapters.screenshot_reader.gemini import GeminiScreenshotReader

        return GeminiScreenshotReader(
            credentials, settings.gemini_model, settings.screenshot_timeout_seconds
        )
    return None


def build_replies(store: PostgresStore, writer: ReplyWriter | None, api: Settings) -> ReviewReplies:
    """Drafts and confirmations exactly as in the panel: same store queries, same cap per
    account (counted in the same table), same validation."""
    return ReviewReplies(
        store,
        writer,
        draft_limit=api.review_draft_rate_limit,
        draft_window_seconds=api.review_draft_rate_window_seconds,
        draft_timeout_seconds=api.review_draft_timeout_seconds,
        clock=time.time,
        prompt_version=PROMPT_VERSION,
        quota=store,
    )


def build_service(
    store: PostgresConversationStore,
    channels: dict[str, MessagingChannel],
    replies: ReviewReplies,
    settings: ChannelsSettings,
    hours_store: PostgresStore | None = None,
) -> ConversationService:
    """The actions are created by the same doors as in the panel: a reply with
    `ReviewReplies.confirm`, the hours of a day with `create_special_hours_action`."""
    doors: dict = {REPLY_REVIEW: ReviewReplyActions(replies)}
    if hours_store is not None:
        doors[SET_SPECIAL_HOURS] = SpecialHoursActions(hours_store)
    return ConversationService(
        store,
        channels,
        ProposalActionsByType(doors),
        settings.service_config(),
        clock=lambda: datetime.now(UTC),
    )


def build_registry(
    service: ConversationService,
    store: PostgresConversationStore,
    replies: ReviewReplies,
    calendar: Calendar | None = None,
    lead_days: int = DEFAULT_LEAD_DAYS,
) -> HandlerRegistry:
    """The jobs of the channels, and only them. Without a calendar, no key dates."""
    registry = HandlerRegistry()
    if calendar is not None:
        registry.register(
            plan_key_dates.KIND,
            plan_key_dates.PlanKeyDates(store, service, calendar, lead_days=lead_days),
        )
    registry.register(channel_inbound.KIND, channel_inbound.ChannelInbound(service, store))
    registry.register(conversation_tick.KIND, conversation_tick.ConversationTick(service))
    registry.register(propose_review.KIND, propose_review.ProposeReview(store, replies, service))
    registry.register(
        channel_action_result.KIND, channel_action_result.ChannelActionResult(service)
    )
    return registry


async def run(settings: ConversationSettings) -> None:
    channels_settings = ChannelsSettings()
    telegram.redact_bot_token_in_logs()
    pool = await asyncpg.create_pool(settings.conversation_database_url, min_size=1, max_size=4)
    store = PostgresStore(
        settings.conversation_database_url, allowance_pepper=Settings().allowance_pepper
    )
    writer = build_writer(ScreenshotSettings())
    if writer is None:
        log.event("reply_writer_missing", logging.WARNING)
    async with httpx.AsyncClient() as client:
        try:
            await store.connect()
            if channels_settings.telegram_enabled:
                channel: MessagingChannel = telegram.TelegramChannel(
                    channels_settings.telegram_bot_token.get_secret_value(), client
                )
            else:
                # Without a token nothing can be sent: proposals wait in the panel.
                log.event("telegram_disabled", logging.WARNING)
                channel = OfflineChannel(telegram.CHANNEL)
            conversation_store = PostgresConversationStore(pool)
            replies = build_replies(store, writer, Settings())
            service = build_service(
                conversation_store, {telegram.CHANNEL: channel}, replies, channels_settings, store
            )
            calendar = load_key_dates_calendar(settings)
            queue = PostgresJobQueue(pool)
            worker = Worker(
                queue,
                build_registry(
                    service, conversation_store, replies, calendar, settings.key_dates_lead_days
                ),
                worker_id=settings.worker_id,
                poll_interval=settings.worker_poll_interval_seconds,
                job_timeout=settings.worker_job_timeout_seconds,
                lock_timeout=settings.worker_lock_timeout_seconds,
            )
            stop = asyncio.Event()
            install_stop_signals(stop)
            tasks = [
                asyncio.create_task(
                    ChannelPlanner(
                        queue,
                        conversation_store,
                        plan_kind=plan_key_dates.KIND if calendar is not None else None,
                    ).run(stop, settings.planner_interval_seconds)
                )
            ]
            if channels_settings.telegram_polling and isinstance(channel, telegram.TelegramChannel):
                # Local development: getUpdates does not work while a webhook is set.
                await channel.delete_webhook()
                log.event("telegram_polling", logging.WARNING)
                tasks.append(
                    asyncio.create_task(telegram.poll(channel, service.handle_inbound, stop=stop))
                )
            await worker.run(stop)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if hasattr(writer, "close"):
                await writer.close()
            await store.close()
            await pool.close()


def load_key_dates_calendar(settings: ConversationSettings) -> Calendar | None:
    """The calendar of holidays, once at start. Disabled, or with data that does not
    load, there are no key dates (logged); everything else keeps working."""
    if not settings.key_dates_enabled:
        log.event("key_dates_disabled", logging.WARNING)
        return None
    try:
        data = load_calendar()
    except CalendarDataError as exc:
        log.event("key_dates_calendar_error", logging.ERROR, error=str(exc)[:200])
        return None
    return data.calendar


async def set_webhook(url: str) -> None:
    """Register the webhook of the bot with the secret. Run by hand, once per deployment."""
    settings = ChannelsSettings()
    secret = settings.telegram_webhook_secret.get_secret_value()
    if not settings.telegram_enabled or not secret:
        raise ConfigError("faltan FICHAITOR_TELEGRAM_BOT_TOKEN o FICHAITOR_TELEGRAM_WEBHOOK_SECRET")
    if not url.startswith("https://"):
        raise ConfigError("la URL del webhook tiene que ser https://")
    telegram.redact_bot_token_in_logs()
    async with httpx.AsyncClient() as client:
        channel = telegram.TelegramChannel(settings.telegram_bot_token.get_secret_value(), client)
        await channel.set_webhook(url, secret)


def main(argv: list[str] | None = None) -> int:
    log.configure()
    args = sys.argv[1:] if argv is None else argv
    try:
        if args[:1] == ["--set-webhook"] and len(args) == 2:
            asyncio.run(set_webhook(args[1]))
            log.event("webhook_set")
            return 0
        if args:
            log.event("config_error", logging.ERROR, error="uso: [--set-webhook <url>]")
            return 2
        settings = ConversationSettings()
        settings.check()
    except ValidationError as exc:
        names = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
        log.event("config_error", logging.ERROR, error=f"valores no válidos: {', '.join(names)}")
        return 2
    except ConfigError as exc:
        log.event("config_error", logging.ERROR, error=str(exc))
        return 2
    except MessagingError as exc:  # --set-webhook: method and status only
        log.event("webhook_error", logging.ERROR, error=str(exc))
        return 1
    try:
        asyncio.run(run(settings))
    except (OSError, asyncpg.PostgresError) as exc:
        log.event("startup_error", logging.ERROR, error=type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
