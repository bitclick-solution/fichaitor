"""The limiter per client in process memory (B2 of docs/seguridad/revision-2026-11-12.md):
it forgets the keys whose hits have expired and never holds more than its cap of keys."""

from app.application.rate_limit import SlidingWindowLimiter


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def test_a_key_whose_hits_have_expired_is_forgotten():
    clock = Clock()
    limiter = SlidingWindowLimiter(2, 60, clock)
    assert limiter.allow("203.0.113.7")
    clock.now += 61
    assert limiter.peek("203.0.113.7")
    assert "203.0.113.7" not in limiter._hits


def test_a_denied_request_does_not_keep_a_key():
    limiter = SlidingWindowLimiter(0, 60, Clock())
    assert not limiter.allow("203.0.113.7")
    assert limiter._hits == {}


def test_the_number_of_keys_is_bounded():
    clock = Clock()
    limiter = SlidingWindowLimiter(5, 3600, clock, max_keys=100)
    for n in range(1_000):
        assert limiter.allow(f"key-{n}")
    assert len(limiter._hits) <= 100
    assert "key-999" in limiter._hits  # the newest are kept


def test_peek_does_not_count():
    limiter = SlidingWindowLimiter(1, 60, Clock())
    for _ in range(5):
        assert limiter.peek("ip")
    assert limiter.allow("ip")
    assert not limiter.peek("ip")
    assert not limiter.allow("ip")
