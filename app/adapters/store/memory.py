"""In-memory store for tests and local development without a database."""

from app.ports.store import WaitlistSignup


class MemoryStore:
    def __init__(self, fail: bool = False) -> None:
        self.signups: dict[tuple[str, str], WaitlistSignup] = {}
        self.events: list[tuple[str, str | None, str | None, int | None]] = []
        self._fail = fail

    async def add_signup(self, signup: WaitlistSignup) -> None:
        if self._fail:
            raise RuntimeError("fallo simulado")
        self.signups.setdefault((signup.channel, signup.contact), signup)

    async def record_event(
        self,
        kind: str,
        source: str | None = None,
        place_id: str | None = None,
        score: int | None = None,
    ) -> None:
        if self._fail:
            raise RuntimeError("fallo simulado")
        self.events.append((kind, source, place_id, score))
