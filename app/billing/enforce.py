"""Job billing_enforce, once a day: the end of the beta (docs/admin-facturacion.md, 4.6;
decision 9.0, 10).

Both dates are the owner's, in `billing_setting`: `launch_at` (seats are sold from then)
and `beta_ends_at` (the beta rights end). Nothing happens while either is empty.

- From BETA_NOTICE_DAYS before `launch_at`, once per `beta_ends_at`: the email
  `beta_ending` to every account with an active profile in a paid plan. The offer for
  the beta accounts is not decided (decision 9.0, 10): FICHAITOR_BILLING_BETA_OFFER is
  empty and the email goes without it.
- From `beta_ends_at`: each active paid profile that no seat (nor a manual, courtesy or
  referral entitlement) covers is settled by `billing.enforce_after_beta`: the oldest
  one moves to Gratis if the account has its free profile left and that Google profile
  is not Gratis in another account; the rest are deactivated (their 30 days to deletion
  start, as any deactivation). No block: nothing was left unpaid. The database logs each
  change with reason 'billing'.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.domain import billing_mail
from app.domain.billing import BetaProfile, EnforceAction, enforce_after_beta
from app.domain.jobs import Job
from app.domain.plans import PAID_PLANS
from app.ports.billing_store import MAIL_JOB, ActiveProfile, BillingJobsStore

log = logging.getLogger("fichaitor.billing")

KIND = "billing_enforce"
BETA_NOTICE_DAYS = 30


def covered_by_grants(profiles: list[ActiveProfile], granted: dict) -> set[str]:
    """The paid profiles without a seat that other entitlements cover (manual, courtesy,
    referral), the oldest first, as many as each plan has."""
    left = dict(granted)
    covered = set()
    for profile in sorted(profiles, key=lambda p: (p.active_since, p.location_id)):
        if profile.plan in PAID_PLANS and not profile.has_seat and left.get(profile.plan, 0) > 0:
            left[profile.plan] -= 1
            covered.add(profile.location_id)
    return covered


class Enforce:
    def __init__(
        self,
        store: BillingJobsStore,
        offer: str = "",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._offer = offer
        self._clock = clock

    async def __call__(self, job: Job) -> None:
        now = self._clock()
        setting = await self._store.beta_setting()
        if setting.launch_at is None or setting.beta_ends_at is None:
            return
        if (
            now >= setting.launch_at - timedelta(days=BETA_NOTICE_DAYS)
            and now < setting.beta_ends_at
            and setting.beta_notice_for != setting.beta_ends_at
        ):
            await self._notice(setting.beta_ends_at)
        if now >= setting.beta_ends_at:
            await self._enforce(now)

    async def _notice(self, ends_at: datetime) -> None:
        day = billing_mail.madrid_date(ends_at)
        sent = 0
        for account_id, email in await self._store.beta_recipients():
            mail = billing_mail.beta_mail(email, ends_on=day, offer=self._offer)
            key = f"{MAIL_JOB}:beta_ending:{account_id}:{day.isoformat()}"
            if await self._store.enqueue(MAIL_JOB, mail, key):
                sent += 1
        await self._store.set_beta_notice(ends_at)
        log.info("aviso del fin de la beta: %s correos", sent)

    async def _enforce(self, now: datetime) -> None:
        moved = deactivated = refused = 0
        for account_id in await self._store.accounts_with_paid_profiles():
            profiles, granted = await self._store.active_profiles(account_id, now)
            covered = covered_by_grants(profiles, granted)
            beta = [
                BetaProfile(
                    p.location_id,
                    p.plan,
                    p.active_since,
                    p.gbp_location,
                    has_seat=p.has_seat or p.location_id in covered,
                )
                for p in profiles
            ]
            unpaid = [b.gbp_location for b in beta if b.plan in PAID_PLANS and not b.has_seat]
            if not unpaid:
                continue
            elsewhere = await self._store.gratis_elsewhere(account_id, unpaid)
            actions = enforce_after_beta(beta, gratis_elsewhere=elsewhere)
            for location_id, action in actions.items():
                # False: the database refused (a leave pending...) or it had changed.
                if action is EnforceAction.TO_GRATIS:
                    if await self._store.move_to_gratis(account_id, location_id):
                        moved += 1
                    else:
                        refused += 1
                elif action is EnforceAction.DEACTIVATE:
                    if await self._store.deactivate(account_id, location_id):
                        deactivated += 1
                    else:
                        refused += 1
        log.info(
            "fin de la beta: %s fichas a Gratis, %s desactivadas, %s sin cambiar",
            moved,
            deactivated,
            refused,
        )
