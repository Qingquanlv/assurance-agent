from __future__ import annotations

import pytest

from bootstrap_fixtures import synthetic_invocation_started

from graph_engine.canonical import canonical_digest
from graph_engine.plugin_api import TaskFailure
from graph_engine.runtime.events import (
    EventEnvelope,
    GraphCompleted,
    GraphStarted,
    NodeActivated,
    NodeCompleted,
    NodeFailed,
    TokenConsumed,
    TokenOffered,
)
from graph_engine.runtime.models import ProjectionError, fold_events
from graph_engine.runtime.planner import subgraph_instance_id


def _envelopes(*events: object) -> tuple[EventEnvelope, ...]:
    return tuple(
        EventEnvelope.from_event(index, event)  # type: ignore[arg-type]
        for index, event in enumerate(events, start=1)
    )


def _bootstrap() -> tuple[object, ...]:
    start_token = canonical_digest(
        {"graph_instance_id": "root", "kind": "graph_start", "target": "child_call"}
    )
    return (
        synthetic_invocation_started(),
        GraphStarted(graph_instance_id="root", graph_id="root", input={"change_id": "C-1"}),
        TokenOffered(
            token_id=start_token,
            graph_instance_id="root",
            source=None,
            target="child_call",
            payload={"change_id": "C-1"},
        ),
        TokenConsumed(token_id=start_token, graph_instance_id="root", node_id="child_call"),
        NodeActivated(
            activation_id="act-child",
            graph_instance_id="root",
            node_id="child_call",
            token_ids=(start_token,),
        ),
    )


@pytest.mark.parametrize("kind", ("invalid_input", "invalid_output"))
def test_fold_admits_active_zero_attempt_structural_contract_failure(kind: str) -> None:
    events = (
        *_bootstrap(),
        NodeFailed(
            activation_id="act-child",
            failure=TaskFailure(kind=kind, message="contract", retryable=False),  # type: ignore[arg-type]
        ),
    )

    folded = fold_events(_envelopes(*events))

    activation = folded.activations[0]
    assert activation.status == "failed"
    assert activation.attempts == ()
    assert activation.structural_failure is True
    assert activation.failure is not None
    assert activation.failure.kind == kind
    if kind == "invalid_input":
        assert all(graph.parent_activation_id != activation.activation_id for graph in folded.graph_instances)


def test_fold_admits_invalid_output_after_completed_child() -> None:
    child_id = subgraph_instance_id("act-child", "child")
    child_start = canonical_digest({"graph_instance_id": child_id, "kind": "graph_start", "target": "done"})
    events = (
        *_bootstrap(),
        GraphStarted(
            graph_instance_id=child_id,
            graph_id="child",
            parent_graph_instance_id="root",
            parent_node_id="child_call",
            parent_activation_id="act-child",
            input={"change_id": "C-1"},
        ),
        TokenOffered(
            token_id=child_start,
            graph_instance_id=child_id,
            source=None,
            target="done",
            payload={"change_id": "C-1"},
        ),
        TokenConsumed(token_id=child_start, graph_instance_id=child_id, node_id="done"),
        NodeActivated(
            activation_id="act-end",
            graph_instance_id=child_id,
            node_id="done",
            token_ids=(child_start,),
        ),
        NodeCompleted(activation_id="act-end", output={"status": "passed"}),
        GraphCompleted(graph_instance_id=child_id, output={"status": "passed"}),
        NodeFailed(
            activation_id="act-child",
            failure=TaskFailure(kind="invalid_output", message="public output", retryable=False),
        ),
    )

    folded = fold_events(_envelopes(*events))

    parent = next(item for item in folded.activations if item.activation_id == "act-child")
    child = next(item for item in folded.graph_instances if item.graph_instance_id == child_id)
    assert parent.structural_failure is True
    assert parent.attempts == ()
    assert parent.failure is not None
    assert parent.failure.kind == "invalid_output"
    assert child.status == "completed"


@pytest.mark.parametrize("kind", ("internal", "transient", "timeout", "configuration"))
def test_fold_rejects_other_zero_attempt_node_failed(kind: str) -> None:
    events = (
        *_bootstrap(),
        NodeFailed(
            activation_id="act-child",
            failure=TaskFailure(kind=kind, message="not structural", retryable=False),  # type: ignore[arg-type]
        ),
    )

    with pytest.raises(ProjectionError, match="failed child graph"):
        fold_events(_envelopes(*events))
