"""The in-memory store of the conversation follows the same rules as PostgreSQL for the
lanes of step 6a (docs/multinegocio.md): tests/test_conversation_lanes_postgres.py."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.adapters.conversation_store.memory import MemoryConversationStore, MemoryLocation
from app.domain.proposals import Proposal, ProposalState, ProposalType
from app.ports.conversation_store import Lane, LocationInactive

NOW = datetime(2027, 3, 10, 11, 0, tzinfo=UTC)
MINE, THEIRS = "acc-mine", "acc-theirs"


def run(coro):
    return asyncio.run(coro)


def proposal(
    pid: str,
    location: str | None,
    *,
    account: str = MINE,
    kind: ProposalType = ProposalType.REVIEW_REPLY,
    state: ProposalState = ProposalState.SENT,
    sent_at: datetime | None = None,
    refs: dict[str, str] | None = None,
) -> Proposal:
    return Proposal(
        id=pid,
        account_id=account,
        type=kind,
        source_ref=f"src:{pid}",
        state=state,
        priority=10,
        created_at=NOW,
        scheduled_for=NOW,
        payload={"location_id": location} if kind is ProposalType.KEY_DATE else {},
        message_refs=refs or {},
        sent_at=sent_at,
        location_id=location,
    )


def store() -> MemoryConversationStore:
    s = MemoryConversationStore()
    s.locations = {
        "loc_a": MemoryLocation(MINE, "Bar La Parra", "Calle Mayor 3, 28901 Getafe, Madrid"),
        "loc_b": MemoryLocation(MINE, "Bar La Parra", "Calle Real 1, 28001 Madrid", False),
        "loc_c": MemoryLocation(MINE, "Café Central"),
        "loc_z": MemoryLocation(THEIRS, "Ajeno"),
    }
    return s


async def _linked(s: MemoryConversationStore, account: str, chat: str, filter_=None):
    identity = (await s.link_identity(account, "telegram", chat, NOW)).identity
    if filter_ is not None:
        s.identities[identity.id] = replace(identity, location_filter=tuple(filter_))
    return identity.id


def test_identities_for_respects_the_filter_and_the_account():
    async def go():
        s = store()
        everyone = await _linked(s, MINE, "1")
        only_c = await _linked(s, MINE, "2", ["loc_c"])
        await _linked(s, THEIRS, "3")
        return (
            [i.id for i in await s.identities_for(MINE, "loc_a")],
            [i.id for i in await s.identities_for(MINE, "loc_c")],
            await s.identities_for(THEIRS, "loc_a"),
            everyone,
            only_c,
        )

    for_a, for_c, foreign, everyone, only_c = run(go())
    assert for_a == [everyone]
    assert for_c == [everyone, only_c]
    assert foreign == []


def test_a_proposal_of_an_inactive_or_foreign_profile_is_refused():
    s = store()
    with pytest.raises(LocationInactive):
        run(s.add_proposal(proposal("prop_1", "loc_b")))
    with pytest.raises(LocationInactive):
        run(s.add_proposal(proposal("prop_2", "loc_z")))
    assert run(s.add_proposal(proposal("prop_3", "loc_a")))
    key_date = proposal("prop_4", "loc_c", kind=ProposalType.KEY_DATE)
    assert run(s.add_proposal(key_date))
    # Without a profile, refused: since step 9 not even a key date takes its payload's.
    with pytest.raises(LocationInactive):
        run(
            s.add_proposal(
                replace(proposal("prop_5", "loc_c", kind=ProposalType.KEY_DATE), location_id=None)
            )
        )
    assert "prop_5" not in s.proposals


def test_lanes_hold_a_proposal_of_their_profile_and_list_the_open_work():
    async def go():
        s = store()
        await s.add_proposal(proposal("prop_a", "loc_a"))
        await s.add_proposal(proposal("prop_c", "loc_c", state=ProposalState.DISMISSED))
        empty = await s.get_lane(MINE, "loc_a")
        await s.save_lane(Lane(MINE, "loc_a", "prop_a"))
        with pytest.raises(ValueError):
            await s.save_lane(Lane(MINE, "loc_c", "prop_a"))
        lanes = await s.lanes_with_open_work()
        s.leaves.add(MINE)
        return empty, await s.get_lane(MINE, "loc_a"), lanes, await s.lanes_with_open_work()

    empty, saved, lanes, on_leave = run(go())
    assert empty.active_proposal_id is None
    assert saved.active_proposal_id == "prop_a"
    assert lanes == [(MINE, "loc_a")]
    assert on_leave == []


def test_focus_quote_cap_and_header():
    async def go():
        s = store()
        chat = await _linked(s, MINE, "1")
        await s.add_proposal(proposal("prop_a", "loc_a", refs={chat: "77"}))
        for pid, location, sent in (
            ("prop_k1", "loc_a", NOW),
            ("prop_k2", "loc_c", NOW),
            ("prop_k3", "loc_c", NOW - timedelta(days=9)),
        ):
            await s.add_proposal(proposal(pid, location, kind=ProposalType.KEY_DATE, sent_at=sent))
        await s.set_focus(chat, "prop_a", NOW)
        since = NOW - timedelta(days=7)
        s.locations["loc_b"] = MemoryLocation(MINE, "Bar La Parra", "", True)
        return (
            await s.get_focus(chat),
            await s.proposal_by_message_ref(MINE, chat, "77"),
            await s.proposal_by_message_ref(THEIRS, chat, "77"),
            await s.count_capped_sent_since(MINE, since),
            await s.count_capped_sent_since(MINE, since, location_id="loc_c"),
            await s.location_label(MINE, "loc_a"),
            await s.location_label(MINE, "loc_c"),
            await s.location_label(THEIRS, "loc_a"),
        )

    focus, quoted, foreign, cap, cap_c, label_a, label_c, label_foreign = run(go())
    assert focus is not None and focus.proposal_id == "prop_a"
    assert quoted is not None and quoted.id == "prop_a" and foreign is None
    assert (cap, cap_c) == (2, 1)
    assert label_a == "Bar La Parra · Getafe"
    assert label_c == "Café Central"
    assert label_foreign == "tu negocio"
