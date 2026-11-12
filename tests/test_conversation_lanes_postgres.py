"""Integration: step 6a of docs/multinegocio.md (the store of the conversation with
lanes), with the four roles, against the TEST database only.

Skipped unless the four URLs are set. They must point to a database whose name ends in
`_test` (tests/conftest.py stops the run otherwise). Create it with
scripts/preparar_bd_pruebas.sh.

What it covers:
- the trigger `proposal_location_guard`: a new proposal is of an active profile of its
  account, and the old code (without a profile) still works through the selected one;
- the new methods of `PostgresConversationStore`: lanes, the filter of each chat, the
  focus, the header, the quoted message and the weekly cap per profile;
- `linked_profiles`, `key_date_profiles` and `review_targets` over active profiles;
- the backfill of the lanes (3.8.4), which db/schema_channels.sql runs at its end since
  the deployment of step 6b, and applying the schemas again over real data, which only
  moves open threads to their lanes once.

Rows are written by the owner, as the old code and the other roles left them, and
deleted at the end. Nothing here uses the API's store.
"""

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest

from app.adapters.conversation_store.postgres import PostgresConversationStore
from app.domain.profile_summary import ProfileFacts, facts_to_json
from app.domain.proposals import Proposal, ProposalState, ProposalType
from app.ports.conversation_store import Lane, LocationInactive

API_URL = os.environ.get("FICHAITOR_TEST_DATABASE_URL")
OWNER_URL = os.environ.get("FICHAITOR_TEST_OWNER_URL")
WORKER_URL = os.environ.get("FICHAITOR_TEST_WORKER_URL")
CONVERSATION_URL = os.environ.get("FICHAITOR_TEST_CONVERSATION_URL")

pytestmark = pytest.mark.skipif(
    not (API_URL and OWNER_URL and WORKER_URL and CONVERSATION_URL),
    reason="sin base de datos de pruebas configurada",
)

# Not "pytest-...": other test files clean every account whose user starts so, and the
# test database is shared by runs at the same time.
PREFIX = "carriles-pytest-"
ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ("db/schema.sql", "db/schema_worker.sql", "db/schema_channels.sql")
NOW = datetime.now(UTC)


def run(coro):
    return asyncio.run(coro)


async def owner(sql: str, *args):
    conn = await asyncpg.connect(OWNER_URL)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def attempt(url: str, sql: str, *args):
    """Run a statement as the role of `url` in a transaction that is always rolled back.
    Returns its status; raises what the database raised."""
    conn = await asyncpg.connect(url)
    try:
        tx = conn.transaction()
        await tx.start()
        try:
            return await conn.execute(sql, *args)
        finally:
            await tx.rollback()
    finally:
        await conn.close()


def refusal(url: str, sql: str, *args) -> str | None:
    try:
        run(attempt(url, sql, *args))
    except asyncpg.PostgresError as exc:
        return str(exc)
    return None


async def cleanup():
    mine = f"(SELECT id FROM account WHERE google_sub LIKE '{PREFIX}%')"
    for sql in (
        f"DELETE FROM conversation_lane WHERE account_id IN {mine}",
        f"DELETE FROM conversation WHERE account_id IN {mine}",
        f"DELETE FROM proposal WHERE account_id IN {mine}",
        f"DELETE FROM channel_identity WHERE account_id IN {mine}",
        f"DELETE FROM offboarding WHERE account_id IN {mine}",
        f"DELETE FROM review WHERE google_sub LIKE '{PREFIX}%'",
        f"DELETE FROM profile_summary WHERE google_sub LIKE '{PREFIX}%'",
        f"UPDATE account SET selected_location_id = NULL WHERE id IN {mine}",
        f"DELETE FROM location WHERE account_id IN {mine}",
        f"DELETE FROM location_activation_log WHERE account_id IN {mine}",
        f"DELETE FROM plan_entitlement WHERE account_id IN {mine}",
        f"DELETE FROM account WHERE id IN {mine}",
    ):
        await owner(sql)


@pytest.fixture(autouse=True)
def clean():
    run(cleanup())
    yield
    run(cleanup())


# --- the stage -------------------------------------------------------------------------


async def new_account(name: str) -> tuple[str, str]:
    sub = f"{PREFIX}{name}-{uuid.uuid4().hex[:8]}"
    rows = await owner(
        "INSERT INTO account (google_sub, email) VALUES ($1, 'x@x') RETURNING id::text", sub
    )
    return rows[0]["id"], sub


async def new_location(
    account: str,
    *,
    active: bool = True,
    title: str = "Bar",
    selected: bool = False,
    gbp: str | None = None,
) -> str:
    rows = await owner(
        "INSERT INTO location (account_id, gbp_account, gbp_location, title)"
        " VALUES ($1::uuid, 'accounts/1', $2, $3) RETURNING id",
        account,
        gbp or f"locations/{PREFIX}{uuid.uuid4().hex[:12]}",
        title,
    )
    location = rows[0]["id"]
    if active:
        # Pro: the owner is not held to the entitlements, and the same Google profile is
        # free in one account only.
        await owner(
            "UPDATE location SET active_since = now(), plan = 'pro' WHERE id = $1", location
        )
    if selected:
        await owner(
            "UPDATE account SET selected_location_id = $2 WHERE id = $1::uuid", account, location
        )
    return location


async def gbp_of(location: str) -> str:
    return (await owner("SELECT gbp_location FROM location WHERE id = $1", location))[0][
        "gbp_location"
    ]


async def new_identity(
    account: str, *, location_filter: list[str] | None = None, verified_at: datetime = NOW
) -> str:
    rows = await owner(
        "INSERT INTO channel_identity (account_id, channel, address, verified_at,"
        " location_filter) VALUES ($1::uuid, 'telegram', $2, $3, $4) RETURNING id",
        account,
        str(uuid.uuid4().int % 10**12),
        verified_at,
        location_filter,
    )
    return rows[0]["id"]


async def owner_proposal(
    account: str,
    location: str | None,
    state: str = "enviada",
    *,
    kind: str = "review_reply",
    sent_at: datetime | None = None,
    message_refs: dict | None = None,
) -> str:
    pid = f"prop_{uuid.uuid4().hex[:16]}"
    payload = {"location_id": location} if kind == "key_date" else {}
    confirmed = state in ("ejecutando", "aplicada", "fallida")
    await owner(
        "INSERT INTO proposal (id, account_id, type, source_ref, state, location_id, payload,"
        " sent_at, message_refs, confirmed_at, confirmed_event_id)"
        " VALUES ($1, $2::uuid, $3, $4, $5, $6, $7::jsonb, $8, $9::jsonb, $10, $11)",
        pid,
        account,
        kind,
        f"pytest:{pid}",
        state,
        location,
        json.dumps(payload),
        sent_at,
        json.dumps(message_refs or {}),
        NOW if confirmed else None,
        "pytest-event" if confirmed else None,
    )
    return pid


def proposal(
    account: str, location: str | None, *, kind: ProposalType = ProposalType.REVIEW_REPLY
) -> Proposal:
    pid = f"prop_{uuid.uuid4().hex[:16]}"
    payload = (
        {"location_id": location, "date": "2027-12-08"} if kind is ProposalType.KEY_DATE else {}
    )
    return Proposal(
        id=pid,
        account_id=account,
        type=kind,
        source_ref=f"pytest:{pid}",
        state=ProposalState.SCHEDULED,
        priority=10,
        created_at=NOW,
        scheduled_for=NOW,
        payload=payload,
        location_id=None if kind is ProposalType.KEY_DATE else location,
    )


async def schedule_leave(account: str, sub: str) -> None:
    await owner(
        "INSERT INTO offboarding (account_id, scope, google_sub, confirmed_at, delete_at)"
        " VALUES ($1::uuid, 'ficha', $2, now(), now() + interval '2 days')",
        account,
        sub,
    )


class Conversation:
    """The store of the conversation with its own role."""

    async def __aenter__(self) -> PostgresConversationStore:
        self.pool = await asyncpg.create_pool(CONVERSATION_URL, min_size=1, max_size=3)
        return PostgresConversationStore(self.pool)

    async def __aexit__(self, *exc) -> None:
        await self.pool.close()


# =============================================================================
# The trigger: a new proposal is of an active profile of its account.
# =============================================================================

INSERT = (
    "INSERT INTO proposal (id, account_id, type, source_ref, location_id, payload)"
    " VALUES ($1, $2::uuid, $3, 'pytest:x', $4, $5::jsonb)"
)


def _pid() -> str:
    return f"prop_{uuid.uuid4().hex[:16]}"


def test_a_proposal_for_an_inactive_profile_is_refused_until_it_is_activated():
    account, _ = run(new_account("inactiva"))
    location = run(new_location(account, active=False))
    args = (_pid(), account, "review_reply", location, "{}")
    assert refusal(CONVERSATION_URL, INSERT, *args) == "ficha_inactiva"
    run(owner("UPDATE location SET active_since = now(), plan = 'pro' WHERE id = $1", location))
    assert run(attempt(CONVERSATION_URL, INSERT, *args)) == "INSERT 0 1"
    run(owner("UPDATE location SET active_since = NULL, plan = NULL WHERE id = $1", location))
    assert refusal(CONVERSATION_URL, INSERT, *args) == "ficha_inactiva"


def test_a_proposal_cannot_name_an_active_profile_of_another_account():
    mine, _ = run(new_account("mia"))
    theirs, _ = run(new_account("ajena"))
    their_loc = run(new_location(theirs))
    assert refusal(CONVERSATION_URL, INSERT, _pid(), mine, "review_reply", their_loc, "{}")


def test_the_old_code_without_a_profile_gets_the_selected_one_or_the_one_of_its_payload():
    async def go():
        account, _ = await new_account("antiguo")
        selected = await new_location(account, selected=True)
        other = await new_location(account)
        async with Conversation() as store:
            review = proposal(account, None)
            assert await store.add_proposal(review)
            key_date = proposal(account, other, kind=ProposalType.KEY_DATE)
            assert await store.add_proposal(key_date)
            stored = [await store.get_proposal(p.id) for p in (review, key_date)]
        return selected, other, stored

    selected, other, (review, key_date) = run(go())
    assert review.location_id == selected
    assert key_date.location_id == other


def test_without_a_profile_and_without_a_selected_one_nothing_is_stored():
    account, _ = run(new_account("sin-ficha"))
    run(new_location(account))
    assert (
        refusal(CONVERSATION_URL, INSERT, _pid(), account, "review_reply", None, "{}")
        == "propuesta_sin_ficha"
    )
    key_date = json.dumps({"date": "2027-12-08"})
    assert (
        refusal(CONVERSATION_URL, INSERT, _pid(), account, "key_date", None, key_date)
        == "propuesta_sin_ficha"
    )


def test_the_old_code_with_a_selected_profile_that_is_not_active_is_refused():
    account, _ = run(new_account("elegida-inactiva"))
    run(new_location(account, active=False, selected=True))
    assert (
        refusal(CONVERSATION_URL, INSERT, _pid(), account, "review_reply", None, "{}")
        == "ficha_inactiva"
    )


def test_the_store_says_location_inactive_and_the_thread_goes_on():
    async def go():
        account, _ = await new_account("almacen")
        inactive = await new_location(account, active=False)
        active = await new_location(account)
        theirs, _ = await new_account("almacen-ajena")
        their_loc = await new_location(theirs)
        async with Conversation() as store:
            with pytest.raises(LocationInactive):
                await store.add_proposal(proposal(account, inactive))
            with pytest.raises(LocationInactive):
                await store.add_proposal(proposal(account, their_loc))
            # Inside the lock of the thread: the refusal does not spoil its transaction.
            async with store.lock(account):
                with pytest.raises(LocationInactive):
                    await store.add_proposal(proposal(account, inactive))
                good = proposal(account, active)
                assert await store.add_proposal(good)
                assert not await store.add_proposal(good)  # the same origin: once
            stored = await store.get_proposal(good.id)
        return active, stored

    active, stored = run(go())
    assert stored is not None and stored.location_id == active


# =============================================================================
# The new methods of the store.
# =============================================================================


def test_identities_for_respects_the_filter_of_each_chat_and_the_account():
    async def go():
        mine, _ = await new_account("filtro")
        a = await new_location(mine)
        b = await new_location(mine)
        theirs, _ = await new_account("filtro-ajena")
        await new_location(theirs)
        everything = await new_identity(mine, verified_at=NOW - timedelta(hours=2))
        only_b = await new_identity(mine, location_filter=[b], verified_at=NOW - timedelta(hours=1))
        unlinked = await new_identity(mine)
        await owner("UPDATE channel_identity SET unlinked_at = now() WHERE id = $1", unlinked)
        await new_identity(theirs)
        async with Conversation() as store:
            for_a = [i.id for i in await store.identities_for(mine, a)]
            for_b = await store.identities_for(mine, b)
            for_theirs = await store.identities_for(theirs, a)
            listed = {i.id: i.location_filter for i in await store.list_identities(mine)}
        return everything, only_b, b, for_a, for_b, for_theirs, listed

    everything, only_b, b, for_a, for_b, for_theirs, listed = run(go())
    assert for_a == [everything]
    assert [i.id for i in for_b] == [everything, only_b]
    assert for_b[1].location_filter == (b,)
    assert for_theirs == []  # the profile of another account: none of theirs see it
    assert listed == {everything: None, only_b: (b,)}


def test_a_lane_is_read_empty_saved_and_only_holds_a_proposal_of_its_profile():
    async def go():
        account, _ = await new_account("carril")
        a = await new_location(account)
        b = await new_location(account)
        of_a = await owner_proposal(account, a)
        of_b = await owner_proposal(account, b)
        async with Conversation() as store:
            empty = await store.get_lane(account, a)
            created = await owner(
                "SELECT count(*) AS n FROM conversation_lane WHERE account_id = $1::uuid", account
            )
            await store.save_lane(Lane(account, a, of_a))
            saved = await store.get_lane(account, a)
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await store.save_lane(Lane(account, a, of_b))
            await store.save_lane(Lane(account, a, None))
            released = await store.get_lane(account, a)
        return empty, created[0]["n"], saved, released, a, of_a

    empty, created, saved, released, a, of_a = run(go())
    assert empty.active_proposal_id is None and created == 0  # reading creates nothing
    assert saved == Lane(saved.account_id, a, of_a)
    assert released.active_proposal_id is None


def test_lanes_with_open_work_are_per_profile_and_skip_a_scheduled_leave():
    async def go():
        account, _ = await new_account("trabajo")
        a = await new_location(account)
        b = await new_location(account)
        gone = await new_location(account)
        await owner_proposal(account, a)
        await owner_proposal(account, a, "programada")
        await owner_proposal(account, b, "descartada")  # finished: no work
        await owner_proposal(account, gone)
        # Deactivated afterwards: still listed, so that the service cancels its proposals.
        await owner("UPDATE location SET active_since = NULL, plan = NULL WHERE id = $1", gone)
        leaving, sub = await new_account("trabajo-baja")
        theirs = await new_location(leaving)
        await owner_proposal(leaving, theirs)
        await schedule_leave(leaving, sub)
        async with Conversation() as store:
            lanes = await store.lanes_with_open_work()
        return account, leaving, a, gone, lanes

    account, leaving, a, gone, lanes = run(go())
    mine = [lane for lane in lanes if lane[0] in (account, leaving)]
    assert mine == sorted([(account, a), (account, gone)])


def test_the_focus_of_a_chat_is_written_and_read_by_the_conversation():
    async def go():
        account, _ = await new_account("foco")
        location = await new_location(account)
        pid = await owner_proposal(account, location)
        identity = await new_identity(account)
        async with Conversation() as store:
            before = await store.get_focus(identity)
            await store.set_focus(identity, pid, NOW)
            after = await store.get_focus(identity)
            await owner("UPDATE channel_identity SET unlinked_at = now() WHERE id = $1", identity)
            unlinked = await store.get_focus(identity)
        return pid, before, after, unlinked

    pid, before, after, unlinked = run(go())
    assert before is None
    assert after is not None and after.proposal_id == pid and after.at == NOW
    assert unlinked is None


def test_a_quoted_message_finds_its_unfinished_proposal_of_that_account_only():
    async def go():
        account, _ = await new_account("cita")
        location = await new_location(account)
        identity = await new_identity(account)
        open_ = await owner_proposal(account, location, message_refs={identity: "501"})
        await owner_proposal(account, location, "aplicada", message_refs={identity: "502"})
        theirs, _ = await new_account("cita-ajena")
        async with Conversation() as store:
            found = await store.proposal_by_message_ref(account, identity, "501")
            finished = await store.proposal_by_message_ref(account, identity, "502")
            other_account = await store.proposal_by_message_ref(theirs, identity, "501")
            other_chat = await store.proposal_by_message_ref(account, "chid_x", "501")
            empty = await store.proposal_by_message_ref(account, identity, "")
        return open_, found, finished, other_account, other_chat, empty

    open_, found, finished, other_account, other_chat, empty = run(go())
    assert found is not None and found.id == open_
    assert finished is None and other_account is None and other_chat is None and empty is None


def test_the_weekly_cap_and_the_unfinished_proposals_per_profile():
    async def go():
        account, _ = await new_account("tope")
        a = await new_location(account)
        b = await new_location(account)
        await owner_proposal(account, a, kind="key_date", sent_at=NOW)
        await owner_proposal(account, b, kind="key_date", sent_at=NOW)
        await owner_proposal(account, b, kind="key_date", sent_at=NOW - timedelta(days=8))
        await owner_proposal(account, b, sent_at=NOW)  # a review: not in the cap
        since = NOW - timedelta(days=7)
        async with Conversation() as store:
            counts = (
                await store.count_capped_sent_since(account, since),
                await store.count_capped_sent_since(account, since, location_id=a),
                await store.count_capped_sent_since(account, since, location_id=b),
            )
            unfinished = (
                len(await store.list_unfinished_proposals(account)),
                {p.location_id for p in await store.list_unfinished_proposals(account, b)},
            )
        return counts, unfinished, b

    counts, unfinished, b = run(go())
    assert counts == (2, 1, 1)
    assert unfinished == (4, {b})


def test_the_header_names_the_business_and_adds_the_town_only_for_a_twin():
    async def go():
        account, sub = await new_account("cabecera")
        parra = await new_location(account, title="Bar La Parra")
        twin = await new_location(account, title="bar la parra ", active=False)
        alone = await new_location(account, title="Café Central")
        theirs, _ = await new_account("cabecera-ajena")
        facts = ProfileFacts(address="Calle Mayor 3, 28901 Getafe, Madrid")
        await owner(
            "INSERT INTO profile_summary (google_sub, location, status, facts)"
            " VALUES ($1, $2, 'ok', $3::jsonb)",
            sub,
            await gbp_of(parra),
            json.dumps(facts_to_json(facts)),
        )
        async with Conversation() as store:
            before = await store.location_label(account, parra)
            await owner(
                "UPDATE location SET active_since = now(), plan = 'pro' WHERE id = $1", twin
            )
            after = await store.location_label(account, parra)
            other = await store.location_label(account, alone)
            foreign = await store.location_label(theirs, parra)
        return before, after, other, foreign

    before, after, other, foreign = run(go())
    assert before == "Bar La Parra"  # the namesake is not active: no town
    assert after == "Bar La Parra · Getafe"
    assert other == "Café Central"
    assert foreign == "tu negocio"  # never the name of another account's business


# =============================================================================
# The planners and the reviews read active profiles.
# =============================================================================


def test_linked_profiles_are_the_active_ones_a_linked_chat_sees():
    async def go():
        account, sub = await new_account("sinc")
        a = await new_location(account)
        b = await new_location(account)
        await new_location(account, active=False)
        await new_identity(account, location_filter=[a])
        leaving, leaving_sub = await new_account("sinc-baja")
        await new_location(leaving)
        await new_identity(leaving)
        await schedule_leave(leaving, leaving_sub)
        async with Conversation() as store:
            filtered = await store.linked_profiles()
            await new_identity(account)
            everything = await store.linked_profiles()
        subs = (sub, leaving_sub)
        return (
            [p for p in filtered if p[0] in subs],
            [p for p in everything if p[0] in subs],
            sub,
            await gbp_of(a),
            await gbp_of(b),
        )

    filtered, everything, sub, gbp_a, gbp_b = run(go())
    assert filtered == [(sub, gbp_a)]
    assert everything == sorted([(sub, gbp_a), (sub, gbp_b)])


def test_key_date_profiles_are_every_active_profile_with_facts():
    async def go():
        account, sub = await new_account("fechas")
        a = await new_location(account)
        b = await new_location(account)
        inactive = await new_location(account, active=False)
        leaving, leaving_sub = await new_account("fechas-baja")
        theirs = await new_location(leaving)
        await schedule_leave(leaving, leaving_sub)
        facts = json.dumps(facts_to_json(ProfileFacts(address="x", region="AN")))
        for google_sub, location in (
            (sub, a),
            (sub, b),
            (sub, inactive),
            (leaving_sub, theirs),
        ):
            await owner(
                "INSERT INTO profile_summary (google_sub, location, status, facts)"
                " VALUES ($1, $2, 'ok', $3::jsonb)",
                google_sub,
                await gbp_of(location),
                facts,
            )
        async with Conversation() as store:
            found = [
                p.location_id
                for p in await store.key_date_profiles()
                if p.account_id in (account, leaving)
            ]
        return found, a, b

    found, a, b = run(go())
    assert found == sorted([a, b])


def test_review_targets_are_the_active_profiles_with_a_chat_that_sees_them():
    async def go():
        account, sub = await new_account("resenas")
        bar = await new_location(account, selected=True)
        gbp = await gbp_of(bar)
        twin_account, twin_sub = await new_account("resenas-gemela")
        twin = await new_location(twin_account, gbp=gbp)  # another user, same profile
        rows = await owner(
            "INSERT INTO review (google_sub, location, gbp_review_id, rating, created_at)"
            " VALUES ($1, $2, 'g1', 5, now()), ($3, $2, 'g1', 5, now()) RETURNING id",
            sub,
            gbp,
            twin_sub,
        )
        review, twin_review = rows[0]["id"], rows[1]["id"]
        for google_sub in (sub, twin_sub):
            await owner(
                "INSERT INTO profile_summary (google_sub, location, status) VALUES ($1, $2, 'ok')",
                google_sub,
                gbp,
            )
        yesterday = NOW - timedelta(days=1)
        manager = await new_identity(account, location_filter=[bar], verified_at=yesterday)
        await new_identity(twin_account, verified_at=yesterday)
        async with Conversation() as store:
            seen = []
            seen.append(await store.review_targets(review))
            # A chat filtered to another profile does not count.
            other = await new_location(account)
            await owner(
                "UPDATE channel_identity SET location_filter = $2 WHERE id = $1", manager, [other]
            )
            seen.append(await store.review_targets(review))
            await owner("UPDATE channel_identity SET location_filter = NULL WHERE id = $1", manager)
            # The last sync lost the access: not proposed.
            await owner(
                "UPDATE profile_summary SET status = 'needs_reconnect' WHERE google_sub = $1", sub
            )
            seen.append(await store.review_targets(review))
            await owner("UPDATE profile_summary SET status = 'ok' WHERE google_sub = $1", sub)
            # Inactive: neither the old list nor the new one.
            await owner("UPDATE location SET active_since = NULL, plan = NULL WHERE id = $1", bar)
            seen.append(await store.review_targets(review))
            await owner("UPDATE location SET active_since = now(), plan = 'pro' WHERE id = $1", bar)
            await schedule_leave(account, sub)
            seen.append(await store.review_targets(review))
            seen.append(await store.review_targets(twin_review))
        return account, bar, twin_account, twin, seen

    account, bar, twin_account, twin, seen = run(go())
    first, filtered, lost, inactive, leaving, twin_target = seen
    assert first.accounts == (account,) and first.targets == ((account, bar),)
    assert filtered.targets == () and filtered.accounts == (account,)
    assert lost.targets == () and lost.accounts == (account,)
    assert inactive.targets == () and inactive.accounts == ()
    assert leaving.targets == () and leaving.accounts == ()
    # The twin account has the profile active but not selected: only the new list.
    assert twin_target.targets == ((twin_account, twin),) and twin_target.accounts == ()


# =============================================================================
# The backfill of the lanes (3.8.4) and applying the schemas again.
# =============================================================================


async def _threads_as_the_old_code_left_them():
    """Two accounts with a thread each: one open proposal active (and one waiting),
    and one whose active proposal is already finished."""
    account, _ = await new_account("relleno")
    a = await new_location(account)
    b = await new_location(account)
    active = await owner_proposal(account, a)
    waiting = await owner_proposal(account, b, "programada")
    await owner(
        "INSERT INTO conversation (account_id, active_proposal_id) VALUES ($1::uuid, $2)",
        account,
        active,
    )
    done_account, _ = await new_account("relleno-terminada")
    c = await new_location(done_account)
    finished = await owner_proposal(done_account, c, "aplicada")
    await owner(
        "INSERT INTO conversation (account_id, active_proposal_id) VALUES ($1::uuid, $2)",
        done_account,
        finished,
    )
    return account, done_account, a, active, waiting


async def _state(accounts: list[str], conn: asyncpg.Connection | None = None) -> dict[str, list]:
    tables = {
        "conversation": "SELECT account_id::text, active_proposal_id, unknown_streak"
        " FROM conversation WHERE account_id = ANY($1::uuid[]) ORDER BY 1",
        "lanes": "SELECT account_id::text, location_id, active_proposal_id"
        " FROM conversation_lane WHERE account_id = ANY($1::uuid[]) ORDER BY 1, 2",
        "proposal": "SELECT to_jsonb(p)::text AS row FROM proposal p"
        " WHERE account_id = ANY($1::uuid[]) ORDER BY 1",
        "channel_identity": "SELECT to_jsonb(i)::text AS row FROM channel_identity i"
        " WHERE account_id = ANY($1::uuid[]) ORDER BY 1",
        "location": "SELECT to_jsonb(l)::text AS row FROM location l"
        " WHERE account_id = ANY($1::uuid[]) ORDER BY 1",
    }
    found = {}
    for name, sql in tables.items():
        rows = await (conn.fetch(sql, accounts) if conn is not None else owner(sql, accounts))
        found[name] = [tuple(r) for r in rows]
    return found


async def _in_a_rolled_back_transaction(work):
    """The backfill empties every thread of the database: run it, as the owner, in a
    transaction that is always rolled back, so that other tests running at the same time
    on the test database are not touched."""
    conn = await asyncpg.connect(OWNER_URL)
    try:
        tx = conn.transaction()
        await tx.start()
        try:
            return await work(conn)
        finally:
            await tx.rollback()
    finally:
        await conn.close()


async def _apply_schemas() -> None:
    conn = await asyncpg.connect(OWNER_URL)
    try:
        for name in SCHEMAS:
            await conn.execute((ROOT / name).read_text(encoding="utf-8"))
    finally:
        await conn.close()


def test_applying_the_schemas_again_fills_the_lanes_once_and_changes_nothing_else():
    """From the deployment of step 6b, db/schema_channels.sql ends by calling the backfill:
    the open thread moves to the lane of its profile, the threads are emptied, and
    everything else stays as it was. Applying it again changes nothing more."""

    async def go():
        account, done_account, a, active, _ = await _threads_as_the_old_code_left_them()
        identity = await new_identity(account)
        await owner(
            "UPDATE channel_identity SET focus_proposal_id = $2, focus_at = now() WHERE id = $1",
            identity,
            f"prop_{'a' * 16}",
        )
        accounts = [account, done_account]
        before = await _state(accounts)
        await _apply_schemas()
        once = await _state(accounts)
        await _apply_schemas()
        return account, done_account, a, active, before, once, await _state(accounts)

    account, done_account, a, active, before, once, twice = run(go())
    assert once["lanes"] == [(account, a, active)]
    assert {(c[0], c[1]) for c in once["conversation"]} == {
        (account, None),
        (done_account, None),
    }
    for table in ("proposal", "channel_identity", "location"):
        assert once[table] == before[table]
    assert twice == once


def test_the_backfill_moves_open_threads_to_their_lane_once():
    async def go():
        account, done_account, a, active, _ = await _threads_as_the_old_code_left_them()
        accounts = [account, done_account]

        async def work(conn):
            first = await conn.fetchval("SELECT fichaitor_fill_conversation_lanes()")
            after_first = await _state(accounts, conn)
            second = await conn.fetchval("SELECT fichaitor_fill_conversation_lanes()")
            return first, after_first, second, await _state(accounts, conn)

        return (account, done_account, a, active, *await _in_a_rolled_back_transaction(work))

    account, done_account, a, active, first, after_first, second, after_second = run(go())
    assert first >= 1  # other accounts of the test database may have threads too
    # Only the open one; the finished one is not copied. Both threads are emptied.
    assert after_first["lanes"] == [(account, a, active)]
    assert {(c[0], c[1]) for c in after_first["conversation"]} == {
        (account, None),
        (done_account, None),
    }
    assert second == 0
    assert after_second == after_first


def test_the_backfill_does_not_overwrite_a_lane_that_already_has_a_proposal():
    async def go():
        account, done_account, a, active, _ = await _threads_as_the_old_code_left_them()
        newer = await owner_proposal(account, a, "recopilando")
        await owner(
            "INSERT INTO conversation_lane (account_id, location_id, active_proposal_id)"
            " VALUES ($1::uuid, $2, $3)",
            account,
            a,
            newer,
        )

        async def work(conn):
            await conn.fetchval("SELECT fichaitor_fill_conversation_lanes()")
            return await _state([account], conn)

        return newer, await _in_a_rolled_back_transaction(work)

    newer, state = run(go())
    assert [lane[2] for lane in state["lanes"]] == [newer]
    assert state["conversation"][0][1] is None


@pytest.mark.parametrize("url", ["api", "worker", "conversation"])
def test_only_the_owner_runs_the_backfill(url):
    target = {"api": API_URL, "worker": WORKER_URL, "conversation": CONVERSATION_URL}[url]
    # In a transaction that is always rolled back, even if it were let in.
    message = refusal(target, "SELECT fichaitor_fill_conversation_lanes()")
    assert message is not None and "permission denied" in message
