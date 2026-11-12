"""Job plan_key_dates: once a day, ask every profile about the days ahead on which its
hours may change (docs/calendario.md, "Preguntar por el horario"). A job of the
conversation process: it creates proposals and talks to nobody but the conversation.

Payload: {}. Its planner enqueues it once per local day, to run at 9:05
(`plan_key_dates:<date>`).

For each profile (`KeyDateProfiles.key_date_profiles`): its region (stored by the sync,
or from the postal code of its address; none known, national holidays only), the days of
`hours_questions` within the lead days, skipping those that already have special hours in
Google, and one proposal per profile and day (`<location>:<date>`): asking twice creates
nothing. When the proposals go out is the conversation's business: quiet hours (9:00 to
21:00 by default), the weekly cap and the queue.

A failure with one profile does not stop the others; the job is tried again at the end
(asking again is harmless).
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, tzinfo

from app.application.conversation_service import ConversationService, KeyDateInput
from app.domain.calendar import Calendar
from app.domain.channel_jobs import PLAN_KEY_DATES
from app.domain.conversation import DEFAULT_TIMEZONE, timezone_for
from app.domain.jobs import Job, JobError
from app.domain.key_dates import DEFAULT_LEAD_DAYS, hours_questions, region_of, source_ref
from app.ports.conversation_store import KeyDateProfiles
from app.worker import log

KIND = PLAN_KEY_DATES


class PlanKeyDates:
    def __init__(
        self,
        profiles: KeyDateProfiles,
        service: ConversationService,
        calendar: Calendar,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        zone: tzinfo | None = None,
        lead_days: int = DEFAULT_LEAD_DAYS,
    ) -> None:
        self._profiles = profiles
        self._service = service
        self._calendar = calendar
        self._clock = clock
        # The zone of the accounts does not exist yet: the peninsula for every one.
        self._zone = zone or timezone_for(DEFAULT_TIMEZONE)
        self._lead_days = lead_days

    async def __call__(self, job: Job) -> None:
        today = self._clock().astimezone(self._zone).date()
        profiles = await self._profiles.key_date_profiles()
        created = asked = failed = 0
        for profile in profiles:
            region = region_of(profile.region, profile.address)
            questions = hours_questions(
                region,
                today,
                self._calendar,
                lead_days=self._lead_days,
                skip=profile.special_dates,
            )
            for question in questions:
                asked += 1
                key_date = KeyDateInput(
                    key=source_ref(profile.location_id, question.day),
                    kind=question.kind,
                    day=question.day,
                    name=question.name,
                    location_id=profile.location_id,
                    ask_hours=True,
                )
                try:
                    if await self._service.submit_key_date(profile.account_id, key_date):
                        created += 1
                except Exception as exc:
                    failed += 1
                    log.event("key_date_not_proposed", logging.WARNING, error=type(exc).__name__)
        log.event(
            "key_dates_planned",
            job_id=job.id,
            profiles=len(profiles),
            asked=asked,
            created=created,
            failed=failed,
        )
        if failed:
            raise JobError(f"{failed} fechas clave sin proponer")
