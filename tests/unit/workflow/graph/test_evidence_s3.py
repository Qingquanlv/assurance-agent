"""S3：EvidenceBinding 解析与 TaskProjection commit 标记。"""

from __future__ import annotations

import pytest

from assurance_agent.workflow.graph.checkpoint import fold_invocation_events
from assurance_agent.workflow.graph.evidence import (
    EvidenceResolutionError,
    binding_from_task,
    resolve_committed_value,
    resolve_evidence_bindings,
)
from assurance_agent.workflow.graph.frozen_output import FrozenOutput, frozen_outputs_wire
from assurance_agent.workflow.graph.models import GraphProjection, TaskProjection
from assurance_agent.workflow.graph.schema_v2 import EvidenceRef, NodeDef


def _frozen(symbol: str = "api_plan_review") -> dict[str, object]:
    fo = FrozenOutput(
        value={"decision": "approve"},
        source_path="change:review/api-plan-review.json",
        source_sha256="abc123",
        model_id="review@1",
        model_schema_digest="schema-digest",
        catalog_symbol=symbol,
    )
    return frozen_outputs_wire({symbol: fo})


def _projection(*, committed: bool = True) -> GraphProjection:
    wire = _frozen()
    task = TaskProjection(
        task_id="task-producer",
        node_id="review",
        status="succeeded",
        frozen_outputs=wire,
        outputs_committed=committed,
    )
    return GraphProjection(
        invocation_id="inv-1",
        entrypoint="full",
        checkpoint_ns="inv-1",
        structural_path="main",
        graph_digest="gd",
        contract_digests={},
        params={},
        root_tree_id="tree-0",
        current_tree_id="tree-0",
        tasks={"task-producer": task},
    )


def test_binding_from_task_freezes_source_sha256() -> None:
    binding = binding_from_task(
        _projection(), alias="ev", producer_task_id="task-producer", symbol="api_plan_review"
    )
    assert binding.alias == "ev"
    assert binding.producer_task_id == "task-producer"
    assert binding.symbol == "api_plan_review"
    assert binding.source_sha256 == "abc123"


def test_resolve_committed_value_rejects_uncommitted() -> None:
    binding = binding_from_task(
        _projection(), alias="ev", producer_task_id="task-producer", symbol="api_plan_review"
    )
    uncommitted = _projection(committed=False)
    with pytest.raises(EvidenceResolutionError, match="not committed"):
        resolve_committed_value(uncommitted, binding)


def test_resolve_evidence_bindings_from_node_def() -> None:
    node = NodeDef(
        uses="skill:noop",
        evidence={"plan": EvidenceRef(node="review", symbol="api_plan_review")},
    )
    bindings = resolve_evidence_bindings(_projection(), node)
    assert len(bindings) == 1
    assert bindings[0].alias == "plan"
    assert bindings[0].symbol == "api_plan_review"


def test_resolve_evidence_values_maps_alias_to_committed_value() -> None:
    from assurance_agent.workflow.graph.evidence import resolve_evidence_values

    node = NodeDef(
        uses="skill:noop",
        evidence={"plan": EvidenceRef(node="review", symbol="api_plan_review")},
    )
    projection = _projection()
    bindings = resolve_evidence_bindings(projection, node)
    values = resolve_evidence_values(projection, bindings)
    assert values == {"plan": {"decision": "approve"}}


def test_fold_marks_fan_out_child_outputs_committed_on_superstep_commit() -> None:
    events = [
        {
            "source": "graph",
            "type": "graph_invocation_started",
            "invocation_id": "inv-1",
            "entrypoint": "full",
            "graph_id": "main",
            "graph_digest": "gd",
            "event_schema_version": 2,
            "ir_digest": "gd",
            "ingest_catalog_digest": "cat",
            "contract_digests": {},
            "params": {},
            "params_sha256": "ps",
            "root_tree_id": "tree-0",
            "max_parallel_tasks": 2,
            "checkpoint_ns": "inv-1",
            "structural_path": "main",
        },
        {
            "source": "graph",
            "type": "superstep_planned",
            "invocation_id": "inv-1",
            "checkpoint_ns": "inv-1",
            "superstep_id": "ss-1",
            "checkpoint_id": "cp-0",
            "task_ids": ["child-task"],
        },
        {
            "source": "graph",
            "type": "task_attempt_started",
            "invocation_id": "inv-1",
            "checkpoint_ns": "inv-1",
            "superstep_id": "ss-1",
            "task_id": "child-task",
            "attempt_id": "att-1",
            "node_id": "fan",
            "input_sha256": "in",
            "graph_digest": "gd",
            "contract_digest": "cd",
            "attempt_number": 1,
            "lease_expires_at": "2026-01-01T00:00:00Z",
            "started_at": "2026-01-01T00:00:00Z",
        },
        {
            "source": "graph",
            "type": "task_attempt_succeeded",
            "invocation_id": "inv-1",
            "checkpoint_ns": "inv-1",
            "superstep_id": "ss-1",
            "task_id": "child-task",
            "attempt_id": "att-1",
            "frozen_outputs": _frozen(),
            "outputs_sha256": {},
            "state_updates": {},
        },
        {
            "source": "graph",
            "type": "superstep_committed",
            "invocation_id": "inv-1",
            "checkpoint_ns": "inv-1",
            "superstep_id": "ss-1",
            "checkpoint_id": "cp-1",
            "write_set_ids": [],
            "target_tree_id": "tree-0",
            "state_values": {},
            "committed_task_ids": [],
        },
    ]
    projection = fold_invocation_events("inv-1", events)
    child = projection.tasks["child-task"]
    assert child.frozen_outputs
    assert child.outputs_committed is True
