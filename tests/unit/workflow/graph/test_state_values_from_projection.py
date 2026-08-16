"""Gate state.* comes from folded ledger projection.state_values, not YAML."""

from __future__ import annotations

from pathlib import Path

from assurance_agent.workflow.graph.checkpoint import state_values_for_import
from assurance_agent.workflow.graph.finalize import _state_values_for_gate
from assurance_agent.workflow.graph.models import GraphProjection, RuntimeContext, TaskResult


def _context(change_dir: Path) -> RuntimeContext:
    return RuntimeContext(
        project_root=change_dir.parents[2],
        repo_root=change_dir.parents[2],
        change_dir=change_dir,
        change_id="CH-1",
    )


def _projection(*, state_values: dict[str, object]) -> GraphProjection:
    return GraphProjection(
        invocation_id="inv-1",
        entrypoint="full",
        checkpoint_ns="inv-1",
        structural_path="main",
        graph_digest="gd-1",
        contract_digests={},
        params={},
        root_tree_id="tree-0",
        current_tree_id="tree-0",
        state_values=state_values,
    )


def test_import_checkpoint_uses_projection_state_values_not_yaml(tmp_path: Path) -> None:
    """When a GraphProjection exists, gate state.* is projection.state_values."""
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    (change / "workflow-state.yaml").write_text(
        "phases:\n  stale: true\n",
        encoding="utf-8",
    )
    context = _context(change)
    projection = _projection(state_values={"from_ledger": 1})

    values = state_values_for_import(context, projection)

    assert values == {"from_ledger": 1}
    assert "phases" not in values

    fallback = state_values_for_import(context, None)
    assert fallback == {"phases": {"stale": True}}


def test_finalize_gate_prefers_folded_state_values(tmp_path: Path) -> None:
    """_state_values_for_gate must not treat projection YAML keys as phases."""
    change = tmp_path / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    (change / "workflow-state.yaml").write_text(
        "invocation_id: inv-yaml\nphases:\n  stale: true\n",
        encoding="utf-8",
    )
    result = TaskResult(status="succeeded", state_updates={"from_task": 2})
    projection = _projection(state_values={"from_ledger": 1})

    values = _state_values_for_gate(change, result, projection=projection)

    assert values == {"from_ledger": 1, "from_task": 2}
    assert "phases" not in values
    assert "invocation_id" not in values
