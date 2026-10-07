from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, TypeAlias

from graph_engine.attempts.events import AttemptSnapshot
from graph_engine.attempts.phase import AttemptPhase, derive_attempt_phase
from graph_engine.attempts.resolutions import AttemptResolution


class AttemptAction(Enum):
    OPEN = "open"
    AUTHORIZE = "authorize"
    BEGIN_WORKSPACE = "begin_workspace"
    DISPATCH = "dispatch"
    RECONCILE = "reconcile"
    ADOPT_RESULT = "adopt_result"
    COMMIT = "commit"
    TERMINATE = "terminate"
    REPLAY_TERMINAL = "replay_terminal"


@dataclass(frozen=True, slots=True)
class RuntimeProgress:
    """Completed prerequisites for this call only, never a durable checkpoint."""

    completed: frozenset[AttemptAction] = field(default_factory=frozenset)
    terminate: bool = False


@dataclass(frozen=True, slots=True)
class ContinueAttempt:
    snapshot: AttemptSnapshot
    terminate: bool = False


@dataclass(frozen=True, slots=True)
class ReturnResolution:
    resolution: AttemptResolution


HandlerResult: TypeAlias = ContinueAttempt | ReturnResolution


class AttemptHandlerPort(Protocol):
    async def handle(self, action: AttemptAction, snapshot: AttemptSnapshot | None) -> HandlerResult: ...


_ACTIVITY_ACTIONS = frozenset({AttemptAction.DISPATCH, AttemptAction.RECONCILE, AttemptAction.ADOPT_RESULT})


def select_action(snapshot: AttemptSnapshot | None, progress: RuntimeProgress) -> AttemptAction:
    """Choose one sequential action from durable state and call-local prerequisites.

    A handler can commit several records without changing the projected phase.
    Conversely, a prepared/promoted phase does not supply this call's workspace
    or authorization. Only completed local actions establish those prerequisites.
    """
    if AttemptAction.OPEN not in progress.completed:
        return AttemptAction.OPEN
    assert snapshot is not None
    phase = derive_attempt_phase(snapshot)
    if phase in {AttemptPhase.TERMINATED, AttemptPhase.RELEASED}:
        return AttemptAction.REPLAY_TERMINAL
    if progress.terminate:
        return AttemptAction.TERMINATE
    if AttemptAction.AUTHORIZE not in progress.completed:
        return AttemptAction.AUTHORIZE
    if AttemptAction.BEGIN_WORKSPACE not in progress.completed:
        return AttemptAction.BEGIN_WORKSPACE
    if not progress.completed & _ACTIVITY_ACTIONS:
        # Preserve the legacy eligibility predicate, including JSON null:
        # an observed null result dispatches again rather than being adopted.
        if snapshot.activity_state == "terminal_observed" and snapshot.activity_outcome is not None:
            return AttemptAction.ADOPT_RESULT
        if snapshot.activity_state in {"prepared", "dispatch_started", "bound"}:
            return AttemptAction.RECONCILE
        return AttemptAction.DISPATCH
    if AttemptAction.COMMIT not in progress.completed:
        return AttemptAction.COMMIT
    return AttemptAction.TERMINATE


class AttemptRuntime:
    """Bounded single-call driver; handlers own all side effects and commits."""

    def __init__(self, handlers: AttemptHandlerPort) -> None:
        self.handlers = handlers

    async def run(self) -> AttemptResolution:
        snapshot = None
        progress = RuntimeProgress()
        # Each action may run once. Pending/unknown outcomes return immediately;
        # this driver neither polls nor retries and does not catch domain errors.
        for _ in AttemptAction:
            action = select_action(snapshot, progress)
            if action in progress.completed:
                raise RuntimeError(f"Attempt handler did not finish {action.value}")
            result = await self.handlers.handle(action, snapshot)
            if isinstance(result, ReturnResolution):
                return result.resolution
            snapshot = result.snapshot
            progress = RuntimeProgress(progress.completed | {action}, result.terminate)
        raise RuntimeError("Attempt action protocol exhausted")
