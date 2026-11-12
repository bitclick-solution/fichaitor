"""Where hours confirmed by the channel become an action: the same door as the panel.

`SpecialHoursActions` implements `app.ports.proposal_actions.ProposalActions` for
`set_special_hours` with `KeyDateStore.create_special_hours_action`, the same method the
panel's answer uses:

- the hours are checked (one day; closed, regular hours, or one or two periods that open
  before they close) and kept as the canonical text, never changed after;
- the action, its `special_hours_confirmed` audit row and the `set_special_hours` job
  are created all or nothing, and only for the selected profile of that very account
  (the second barrier of ownership, after the conversation's own);
- at most one change per day and profile is on its way
  (`action_one_live_per_special_date`).

`ProposalActionsByType` sends each request to the door of its type.
"""

from collections.abc import Mapping

from app.domain.actions import SET_SPECIAL_HOURS
from app.domain.special_hours import InvalidSpecialHours, from_entries
from app.ports.key_dates import KeyDateStore, SpecialHoursInProgress
from app.ports.proposal_actions import (
    REJECT_INVALID,
    REJECT_TARGET_GONE,
    REJECT_UNAVAILABLE,
    ActionReceipt,
    ActionRejected,
    ActionRequest,
    ProposalActions,
)


class SpecialHoursActions:
    def __init__(self, store: KeyDateStore) -> None:
        self._store = store

    async def create(self, request: ActionRequest) -> ActionReceipt:
        if not request.confirmed_event_id or request.confirmed_at is None:
            raise AssertionError("acción sin confirmación registrada")
        if request.type != SET_SPECIAL_HOURS:
            raise ActionRejected(REJECT_UNAVAILABLE)
        try:
            change = from_entries(request.payload.get("special_hours"))
        except InvalidSpecialHours:
            raise ActionRejected(REJECT_INVALID) from None
        if not request.location_id:
            raise ActionRejected(REJECT_TARGET_GONE)
        try:
            action_id = await self._store.create_special_hours_action(
                request.account_id, request.location_id, change, request.confirmed_at
            )
        except SpecialHoursInProgress:
            raise ActionRejected(REJECT_UNAVAILABLE) from None
        if action_id is None:
            # Not the selected profile any more, or a leave is scheduled.
            raise ActionRejected(REJECT_TARGET_GONE)
        return ActionReceipt(action_id, created=True)


class ProposalActionsByType:
    """One door per action type; an unknown type creates nothing."""

    def __init__(self, doors: Mapping[str, ProposalActions]) -> None:
        self._doors = dict(doors)

    async def create(self, request: ActionRequest) -> ActionReceipt:
        door = self._doors.get(request.type)
        if door is None:
            raise ActionRejected(REJECT_UNAVAILABLE)
        return await door.create(request)
