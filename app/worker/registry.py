"""Which code runs each kind of job."""

from collections.abc import Awaitable, Callable
from typing import Protocol

from app.domain.jobs import Job

Handler = Callable[[Job], Awaitable[None]]


class ExhaustedHook(Protocol):
    async def on_exhausted(self, job: Job) -> None:
        """Optional method of a handler: called when a job ends as failed after
        running out of attempts (not after a PermanentJobError, which the handler
        raises itself and can deal with before raising)."""
        ...


class HandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, kind: str, handler: Handler) -> None:
        if kind in self._handlers:
            raise ValueError(f"ya hay un manejador para {kind!r}")
        self._handlers[kind] = handler

    def get(self, kind: str) -> Handler:
        return self._handlers[kind]

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(self._handlers)
