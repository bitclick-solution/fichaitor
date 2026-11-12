"""Link codes: single use, 10 minutes, 3 attempts, never stored in clear."""

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.adapters.conversation_store.memory import MemoryConversationStore
from app.domain import link_codes as lc

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


def test_code_is_six_digits_and_varies():
    codes = {lc.generate_code() for _ in range(200)}
    assert all(len(c) == 6 and c.isdigit() for c in codes)
    assert len(codes) > 150


def test_code_shape():
    assert lc.is_code_shaped("012345") and lc.is_code_shaped(" 123456 ")
    for bad in ("12345", "1234567", "12345a", "", None, "12 3456"):
        assert not lc.is_code_shaped(bad)


def test_only_the_hash_is_stored():
    code, record = lc.new_link_code("acc", NOW)
    assert record.code_hash == hashlib.sha256(code.encode()).hexdigest()
    assert len(record.code_hash) == 64
    assert code not in repr(record)
    assert record.expires_at == NOW + timedelta(minutes=10) and record.used_at is None


def test_pepper_changes_the_hash():
    assert lc.code_hash("123456", "pepper") != lc.code_hash("123456")
    assert lc.code_hash("123456", "pepper") == lc.code_hash(" 123456 ", "pepper")


def test_check_code_expires_and_is_single_use():
    _, record = lc.new_link_code("acc", NOW)
    assert lc.check_code(record, NOW + timedelta(minutes=9, seconds=59)) is lc.CodeCheck.OK
    assert lc.check_code(record, NOW + timedelta(minutes=10)) is lc.CodeCheck.EXPIRED
    assert lc.check_code(replace(record, used_at=NOW), NOW) is lc.CodeCheck.USED
    assert lc.check_code(None, NOW) is lc.CodeCheck.UNKNOWN


def test_third_failure_blocks_for_an_hour_and_then_starts_again():
    state = lc.SenderState()
    state = lc.register_failure(state, NOW)
    state = lc.register_failure(state, NOW)
    assert not lc.is_blocked(state, NOW)
    state = lc.register_failure(state, NOW)
    assert lc.is_blocked(state, NOW)
    assert lc.is_blocked(state, NOW + timedelta(minutes=59))
    later = NOW + timedelta(hours=1)
    assert not lc.is_blocked(state, later)
    assert lc.register_failure(state, later).failed_attempts == 1


def test_success_clears_the_failures():
    state = lc.register_failure(lc.register_failure(lc.SenderState(), NOW), NOW)
    assert lc.register_success(state).failed_attempts == 0


def test_unlinked_reply_once_every_24_hours():
    assert lc.may_send_unlinked_reply(lc.SenderState(), NOW)
    state = lc.SenderState(last_unlinked_reply_at=NOW)
    assert not lc.may_send_unlinked_reply(state, NOW + timedelta(hours=23))
    assert lc.may_send_unlinked_reply(state, NOW + timedelta(hours=24))


def test_address_hash_does_not_contain_the_address():
    hashed = lc.address_hash("telegram", "123456789")
    assert "123456789" not in hashed and len(hashed) == 64
    assert hashed != lc.address_hash("whatsapp", "123456789")


def test_store_consumes_a_code_once_and_a_new_code_retires_the_old():
    async def scenario():
        store = MemoryConversationStore()
        code, record = lc.new_link_code("acc", NOW)
        await store.save_link_code(record)
        assert all(code not in repr(v) for v in store.link_codes.values())
        assert await store.consume_link_code(record.code_hash, NOW) == "acc"
        assert await store.consume_link_code(record.code_hash, NOW) is None

        _, first = lc.new_link_code("acc", NOW)
        _, second = lc.new_link_code("acc", NOW)
        await store.save_link_code(first)
        await store.save_link_code(second)
        assert await store.consume_link_code(first.code_hash, NOW) is None
        late = NOW + timedelta(minutes=10)
        assert await store.consume_link_code(second.code_hash, late) is None

    asyncio.run(scenario())
