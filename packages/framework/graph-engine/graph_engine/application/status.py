from __future__ import annotations

from typing import Literal

from langgraph.errors import GraphRecursionError
from pydantic import Field

from graph_engine.plugin_api import FrozenModel


InvocationStatusName = Literal[
    "running",
    "blocked",
    "interrupted",
    "stopped",
    "failed",
    "completed",
]

InterruptKind = Literal["human", "system_wake", "system_block"]
TerminalStatusName = Literal["stopped", "failed", "completed"]


class InvocationStatus(FrozenModel):
    status: InvocationStatusName
    reason: str | None = None


class InterruptEnvelope(FrozenModel):
    kind: InterruptKind
    reason: str | None = None


class GraphSnapshotEnvelope(FrozenModel):
    next: tuple[str, ...] = ()
    interrupts: tuple[InterruptEnvelope, ...] = ()


class TerminalEnvelope(FrozenModel):
    status: TerminalStatusName
    reason: str = Field(min_length=1)


def normalize_graph_snapshot(snapshot: GraphSnapshotEnvelope) -> InvocationStatus:
    if not isinstance(snapshot, GraphSnapshotEnvelope):
        raise TypeError("status is normalized only from compiled graph snapshots")
    if snapshot.interrupts:
        human = next((item for item in snapshot.interrupts if item.kind == "human"), None)
        if human is not None:
            return InvocationStatus(status="interrupted", reason=human.reason)
        system = snapshot.interrupts[0]
        return InvocationStatus(status="blocked", reason=system.reason)
    if snapshot.next:
        return InvocationStatus(status="running")
    return InvocationStatus(status="completed")


def normalize_terminal_envelope(envelope: TerminalEnvelope) -> InvocationStatus:
    if not isinstance(envelope, TerminalEnvelope):
        raise TypeError("status is normalized only from terminal envelopes")
    return InvocationStatus(status=envelope.status, reason=envelope.reason)


def normalize_runtime_error(error: BaseException) -> InvocationStatus:
    if isinstance(error, GraphRecursionError):
        return InvocationStatus(status="failed", reason="graph_recursion_limit")
    raise TypeError("runtime status accepts GraphRecursionError only")


__all__ = [
    "GraphSnapshotEnvelope",
    "InterruptEnvelope",
    "InterruptKind",
    "InvocationStatus",
    "InvocationStatusName",
    "TerminalEnvelope",
    "TerminalStatusName",
    "normalize_graph_snapshot",
    "normalize_runtime_error",
    "normalize_terminal_envelope",
]
