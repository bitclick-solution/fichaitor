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


class LostHook(Protocol):
    async def on_lost(self, kind: str) -> int:
        """Optional method of a handler: called now and then so that it can record
        the jobs of its kind that the queue closed as failed because their worker
        died during the last attempt. No handler ran for them, so on_exhausted was
        never called. Must be safe to repeat. Returns how many it recorded."""
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
