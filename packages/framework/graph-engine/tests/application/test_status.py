from __future__ import annotations

from typing import get_args

import pytest
from langgraph.errors import GraphRecursionError
from pydantic import ValidationError

from graph_engine.application.status import (
    GraphSnapshotEnvelope,
    InterruptEnvelope,
    InvocationStatus,
    InvocationStatusName,
    TerminalEnvelope,
    normalize_graph_snapshot,
    normalize_runtime_error,
    normalize_terminal_envelope,
)
from graph_engine.attempts import PendingTaskResult, SystemReference


def test_invocation_status_vocabulary_is_closed() -> None:
    assert get_args(InvocationStatusName) == (
        "running",
        "blocked",
        "interrupted",
        "stopped",
        "failed",
        "completed",
    )
    with pytest.raises(ValidationError):
        InvocationStatus(status="pending")


def test_graph_recursion_error_is_failed_runtime_state() -> None:
    status = normalize_runtime_error(GraphRecursionError("recursion limit reached"))
    assert status == InvocationStatus(status="failed", reason="graph_recursion_limit")


def test_business_budget_terminal_is_distinct_from_graph_recursion() -> None:
    status = normalize_terminal_envelope(
        TerminalEnvelope(status="completed", reason="round_budget_exhausted")
    )
    assert status == InvocationStatus(status="completed", reason="round_budget_exhausted")
    assert status.reason != "graph_recursion_limit"


def test_human_interrupt_normalizes_to_interrupted() -> None:
    status = normalize_graph_snapshot(
        GraphSnapshotEnvelope(next=("approve",), interrupts=(InterruptEnvelope(kind="human"),))
    )
    assert status == InvocationStatus(status="interrupted")


def test_system_interrupt_normalizes_to_blocked() -> None:
    status = normalize_graph_snapshot(
        GraphSnapshotEnvelope(
            next=("settle-effect",),
            interrupts=(InterruptEnvelope(kind="system_wake", reason="resource_pending"),),
        )
    )
    assert status == InvocationStatus(status="blocked", reason="resource_pending")


def test_active_snapshot_without_interrupts_is_running() -> None:
    status = normalize_graph_snapshot(GraphSnapshotEnvelope(next=("execute",), interrupts=()))
    assert status == InvocationStatus(status="running")


def test_empty_snapshot_without_interrupts_is_completed() -> None:
    status = normalize_graph_snapshot(GraphSnapshotEnvelope(next=(), interrupts=()))
    assert status == InvocationStatus(status="completed")


def test_status_normalization_does_not_accept_attempt_journal() -> None:
    pending = PendingTaskResult(wakeup=SystemReference(reference_id="wake-1"))
    with pytest.raises(TypeError):
        normalize_graph_snapshot(pending)
    with pytest.raises(TypeError):
        normalize_terminal_envelope(pending)
