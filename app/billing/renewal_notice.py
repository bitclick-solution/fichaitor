"""Job billing_renewal_notice, once a day: the notice 30 days before each renewal, with
its price frozen (docs/admin-facturacion.md, 3.3; decision 9.0, 4).

For each seat charged by subscription, not cancelled, whose year ends within
RENEWAL_NOTICE_DAYS and not noticed yet for that year:

1. Its price for the renewal: `billing.seat_price` over the seats that will count on
   that day (`seats_counting_at`), the 5 % from the second one. A seat without an active
   profile does not renew, so it counts for nobody.
2. The price is set on its subscription (`set_renewal_price`, without proration), with
   an idempotency key of the seat and the year: what the email says is what Stripe
   charges. What changes in the account after the notice counts for the next renewal.
3. The notice is noted and its email enqueued in one transaction, once per year:
   `renewal_notice`, or `renewal_notice_unassigned` for a seat without an active profile
   ("it will not renew nor be charged").

And for each seat still without an active profile UNASSIGNED_CANCEL_DAYS before its
renewal: `cancel_at_period_end`, so that it is not charged. Its profile keeps its year.

A seat a beta account bought in its window (30 days from the launch) is still in its
Stripe trial: its first charge is not a renewal. Its price was fixed at the Checkout, so
there is no T-30 notice; instead, BETA_WINDOW_NOTICE_DAYS before the first charge, the
email `trial_ending` (the price with VAT, the date and how to cancel), once per trial
end. A seat in trial without an active profile gets no email, and is cancelled at
T-UNASSIGNED_CANCEL_DAYS as any other.
"""

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.domain import billing_mail
from app.domain.billing import (
    BETA_WINDOW_NOTICE_DAYS,
    RENEWAL_NOTICE_DAYS,
    TAX_PERCENT,
    UNASSIGNED_CANCEL_DAYS,
    Seat,
    price_cents,
    seat_price,
    seats_counting_at,
)
from app.domain.jobs import Job, JobError
from app.ports.billing_store import MAIL_JOB, BillingJobsStore, SeatRow
from app.ports.payments import Payments, PaymentsRejected, PaymentsUnavailable

log = logging.getLogger("fichaitor.billing")

KIND = "billing_renewal_notice"


class RenewalNotice:
    def __init__(
        self,
        store: BillingJobsStore,
        payments: Payments,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._payments = payments
        self._clock = clock

    async def __call__(self, job: Job) -> None:
        now = self._clock()
        noticed = cancelled = trials = 0
        for seat in await self._store.seats_renewing_by(now + timedelta(days=RENEWAL_NOTICE_DAYS)):
            try:
                if seat.in_trial:
                    if await self._trial_notice(seat, now):
                        trials += 1
                elif await self._notice(seat):
                    noticed += 1
                if await self._cancel_unassigned(seat, now):
                    cancelled += 1
            except PaymentsUnavailable as exc:
                raise JobError(f"Stripe no disponible ({exc.status or 'red'})") from None
            except PaymentsRejected as exc:
                # That subscription cannot change (cancelled in Stripe, most likely): the
                # reconciliation copies its state; the others go on.
                log.warning("aviso de renovación no aplicado a una plaza: %s", exc.code)
        log.info(
            "avisos de renovación: %s; de fin de la ventana beta: %s; plazas sin ficha"
            " canceladas: %s",
            noticed,
            trials,
            cancelled,
        )

    async def _trial_notice(self, seat: SeatRow, now: datetime) -> bool:
        """`trial_ending`, BETA_WINDOW_NOTICE_DAYS before the first charge of a seat
        bought in the beta window, once per trial end. Its price is the one of the
        Checkout (the subscription's), with the VAT of the customer."""
        ends = seat.trial_ends_at
        assert ends is not None
        if ends > now + timedelta(days=BETA_WINDOW_NOTICE_DAYS) or seat.cancel_at_period_end:
            return False
        if seat.location_id is None:
            return False
        state = await self._store.location_state(seat.account_id, seat.location_id)
        if not state.active:
            return False  # not charged: cancelled at T-UNASSIGNED_CANCEL_DAYS
        customer = await self._store.customer(seat.account_id)
        if customer is None:
            log.warning("plaza sin datos de facturación: sin aviso del fin de la ventana beta")
            return False
        key = seat.next_price_key
        if key is None:
            rows, active, _ = await self._seats(seat.account_id)
            key = seat_price(
                seat.plan, seat.id, seats_counting_at(self._domain(rows, active), ends)
            )
        treatment = customer.profile.tax_treatment
        mail = billing_mail.trial_mail(
            customer.profile.invoice_email,
            plan=seat.plan,
            title=state.title,
            charge_on=billing_mail.madrid_date(ends),
            total=billing_mail.with_tax(price_cents(key), TAX_PERCENT[treatment]),
        )
        day = ends.date().isoformat()
        return await self._store.mark_trial_notice(
            seat.id, ends, mail, f"{MAIL_JOB}:trial_ending:{seat.id}:{day}"
        )

    async def _seats(self, account_id: str) -> tuple[list[SeatRow], dict[str, bool], dict]:
        """The seats of the account, with which ones hold an active profile now."""
        rows = await self._store.seats_of(account_id)
        active: dict[str, bool] = {}
        titles: dict[str, str | None] = {}
        for row in rows:
            if row.location_id is None:
                active[row.id] = False
                continue
            state = await self._store.location_state(account_id, row.location_id)
            active[row.id], titles[row.id] = state.active, state.title
        return rows, active, titles

    @staticmethod
    def _domain(rows: list[SeatRow], active: dict[str, bool]) -> list[Seat]:
        """A seat whose profile is not active counts as a seat without a profile."""
        return [
            s.domain() if active.get(s.id) else replace(s.domain(), location_id=None)
            for s in rows
            if s.current_period_end is not None
        ]

    async def _notice(self, seat: SeatRow) -> bool:
        assert seat.current_period_end is not None and seat.stripe_subscription_id is not None
        renews_at = seat.current_period_end
        if await self._store.renewal_noticed(seat.id) == renews_at:
            return False
        customer = await self._store.customer(seat.account_id)
        if customer is None:
            log.warning("plaza sin datos de facturación: sin aviso de renovación")
            return False
        rows, active, titles = await self._seats(seat.account_id)
        assigned = active.get(seat.id, False)
        key = seat_price(
            seat.plan, seat.id, seats_counting_at(self._domain(rows, active), renews_at)
        )
        day = renews_at.date().isoformat()
        renewal_key = f"renewal:{seat.id}:{day}:{key.value}"  # gitleaks:allow
        await self._payments.set_renewal_price(
            seat.stripe_subscription_id,
            key,
            idempotency_key=renewal_key,  # gitleaks:allow
        )
        treatment = customer.profile.tax_treatment
        mail = billing_mail.renewal_mail(
            customer.profile.invoice_email,
            plan=seat.plan,
            title=titles.get(seat.id),
            assigned=assigned,
            renews_on=billing_mail.madrid_date(renews_at),
            total=billing_mail.with_tax(price_cents(key), TAX_PERCENT[treatment]),
        )
        return await self._store.mark_renewal_notice(
            seat.id, renews_at, key, mail, f"{MAIL_JOB}:renewal:{seat.id}:{day}"
        )

    async def _cancel_unassigned(self, seat: SeatRow, now: datetime) -> bool:
        assert seat.current_period_end is not None and seat.stripe_subscription_id is not None
        if seat.current_period_end > now + timedelta(days=UNASSIGNED_CANCEL_DAYS):
            return False
        if seat.location_id is not None:
            state = await self._store.location_state(seat.account_id, seat.location_id)
            if state.active:
                return False
        day = seat.current_period_end.date().isoformat()
        sub = await self._payments.set_cancel_at_period_end(
            seat.stripe_subscription_id, True, idempotency_key=f"unassigned:{seat.id}:{day}"
        )
        await self._store.set_cancel_at_period_end(seat.id, sub.cancel_at_period_end)
        log.info("plaza sin ficha: no se renueva")
        return True
