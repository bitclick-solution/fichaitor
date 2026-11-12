"""The key dates in the panel: list the days Fichaitor is asking about, answer one and
dismiss one. The same proposals the chat shows (docs/canales.md), moved by the same
rules (app/domain/key_dates.py): an answer from the panel is the owner's confirmation,
and the change of hours goes to Google through the same action as one confirmed by the
chat. The API never talks to Google here.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo

from app.domain.conversation import DEFAULT_TIMEZONE, timezone_for
from app.domain.key_dates import answerable
from app.domain.special_hours import checked
from app.ports.key_dates import KeyDateNotFound, KeyDateStore, KeyDateView


@dataclass(frozen=True)
class Answer:
    kind: str  # closed | regular | periods
    periods: tuple[tuple[str, str], ...] = ()


class KeyDates:
    def __init__(
        self, store: KeyDateStore, clock: Callable[[], float], zone: tzinfo | None = None
    ) -> None:
        self._store = store
        self._clock = clock
        # The zone of the accounts does not exist yet: the peninsula for every one.
        self._zone = zone or timezone_for(DEFAULT_TIMEZONE)

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), UTC)

    def today(self) -> date:
        return self._now().astimezone(self._zone).date()

    def answerable(self, view: KeyDateView) -> bool:
        return answerable(view.state, view.day, self.today())

    async def list(self, account_id: str, *, pending_only: bool) -> Sequence[KeyDateView]:
        found = await self._store.list_key_dates(account_id, self.today())
        return [v for v in found if self.answerable(v)] if pending_only else list(found)

    async def answer(self, account_id: str, proposal_id: str, answer: Answer) -> KeyDateView:
        """Raises KeyDateNotFound, InvalidSpecialHours, PanelRefused or
        SpecialHoursInProgress."""
        view = await self._store.get_key_date(account_id, proposal_id)
        if view is None:
            raise KeyDateNotFound
        change = checked(view.day, answer.kind, answer.periods)
        await self._store.answer_key_date(
            account_id, proposal_id, change, self._now(), self.today()
        )
        return await self._found(account_id, proposal_id)

    async def dismiss(self, account_id: str, proposal_id: str) -> KeyDateView:
        if await self._store.get_key_date(account_id, proposal_id) is None:
            raise KeyDateNotFound
        await self._store.dismiss_key_date(account_id, proposal_id, self._now())
        return await self._found(account_id, proposal_id)

    async def _found(self, account_id: str, proposal_id: str) -> KeyDateView:
        view = await self._store.get_key_date(account_id, proposal_id)
        if view is None:  # it was there a moment ago
            raise KeyDateNotFound
        return view
