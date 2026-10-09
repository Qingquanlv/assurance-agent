from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, TypeAlias

from graph_engine.attempts.orchestration.checkpoint import AttemptCheckpoint, AttemptPhase
from graph_engine.attempts.models.resolutions import AttemptResolution
from graph_engine.persistence.attempt_checkpoint import AttemptCheckpointStore


@dataclass(frozen=True, slots=True)
class DurableProgress:
    """The handler saved progress; reload the durable record before dispatch."""


@dataclass(frozen=True, slots=True)
class ReturnResolution:
    resolution: AttemptResolution


HandlerResult: TypeAlias = DurableProgress | ReturnResolution
PhaseHandler: TypeAlias = Callable[[AttemptCheckpoint], Awaitable[HandlerResult]]


class AttemptHandlerPort(Protocol):
    checkpoints: AttemptCheckpointStore
    phases: Mapping[AttemptPhase, PhaseHandler]

    async def open_or_restore(self) -> AttemptCheckpoint: ...


class AttemptRuntime:
    """Dispatch only the saved phase. Waiting returns; progress always reloads."""

    def __init__(self, handlers: AttemptHandlerPort) -> None:
        self.handlers = handlers

    async def run(self) -> AttemptResolution:
        checkpoint = await self.handlers.open_or_restore()
        while True:
            result = await self.handlers.phases[checkpoint.phase](checkpoint)
            if isinstance(result, ReturnResolution):
                return result.resolution
            latest = await self.handlers.checkpoints.load(checkpoint.attempt_key)
            if latest is None or latest.revision <= checkpoint.revision:
                raise RuntimeError("Attempt handler made no durable progress")
            checkpoint = latest
