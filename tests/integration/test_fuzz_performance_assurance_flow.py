"""Minimum no-bypass integration coverage for wired Fuzz/Performance assurance cycles."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers_four_layer_runtime import make_fixture, run_codegen_only
from assurance_agent.workflow.graph.compiler import compile_packaged_workflow, resolve_params
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.graph.models import (
    GraphProjection,
    RuntimeContext,
    TaskProjection,
)
from assurance_agent.workflow.graph.planner import plan_superstep
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2_with_origin


class _EmptyArtifacts:
    def read_json(self, tree_id: str, logical_path: str):  # noqa: ARG002
        raise KeyError(logical_path)


def _load_compiled():
    loaded = load_workflow_v2_with_origin(Path.cwd())
    contracts = load_execution_contracts(Path.cwd())
    return compile_packaged_workflow(loaded.schema, contracts), contracts


def _context(tmp_path: Path) -> RuntimeContext:
    return RuntimeContext(
        project_root=tmp_path,
        repo_root=tmp_path,
        change_dir=tmp_path / "qa" / "changes" / "CH-1",
        change_id="CH-1",
    )


def _cycle_projection(
    compiled,
    *,
    graph_id: str,
    layer: str,
    tasks: list[TaskProjection] | None = None,
    supersteps: int = 0,
) -> GraphProjection:
    return GraphProjection(
        invocation_id=f"inv-{graph_id}",
        entrypoint=graph_id,
        checkpoint_ns=f"inv-{graph_id}",
        parent_invocation_id="inv-parent",
        parent_task_id="parent-task",
        structural_path=f"assurance/{graph_id}",
        graph_digest=compiled.digest,
        contract_digests=dict(compiled.contract_digests),
        params=resolve_params(compiled.schema, {"run_mode": "full", "test_types": [layer]}),
        root_tree_id="tree-0",
        current_tree_id="tree-0",
        supersteps=supersteps,
        tasks={task.task_id: task for task in tasks or []},
    )


def _task(
    node_id: str,
    *,
    status: str = "succeeded",
    value: object | None = None,
    gate_report: dict[str, object] | None = None,
) -> TaskProjection:
    payload: dict[str, object] = {
        "task_id": f"task-{node_id}",
        "node_id": node_id,
        "status": status,
        "attempts_used": 1,
        "latest_attempt_id": f"task-{node_id}-a1",
        "outputs_committed": status == "succeeded",
    }
    if value is not None:
        payload["value"] = value
    if gate_report is not None:
        payload["gate_report"] = gate_report
    return TaskProjection(**payload)  # type: ignore[arg-type]


def _plan_cycle(
    compiled,
    tmp_path: Path,
    *,
    graph_id: str,
    layer: str,
    tasks: list[TaskProjection] | None = None,
    supersteps: int = 0,
):
    return plan_superstep(
        compiled,
        _cycle_projection(compiled, graph_id=graph_id, layer=layer, tasks=tasks, supersteps=supersteps),
        _context(tmp_path),
        _EmptyArtifacts(),
    )


@pytest.mark.parametrize(
    ("layer", "graph_id"),
    [("fuzz", "fuzz-plan-cycle"), ("performance", "performance-plan-cycle")],
)
def test_plan_cycle_starts_at_applicability(tmp_path: Path, layer: str, graph_id: str) -> None:
    compiled, _ = _load_compiled()
    plan = _plan_cycle(compiled, tmp_path, graph_id=graph_id, layer=layer)
    assert [task.node_id for task in plan.tasks] == ["applicability"]


@pytest.mark.parametrize(
    ("layer", "graph_id"),
    [("fuzz", "fuzz-plan-cycle"), ("performance", "performance-plan-cycle")],
)
def test_inapplicable_branch_skips_reviewer(tmp_path: Path, layer: str, graph_id: str) -> None:
    compiled, _ = _load_compiled()
    applicability = _task(
        "applicability",
        value={"layer": layer, "applicable": False, "reason_code": "no_automated_cases", "case_ids": []},
    )
    plan = _plan_cycle(
        compiled, tmp_path, graph_id=graph_id, layer=layer, tasks=[applicability], supersteps=1
    )
    assert [task.node_id for task in plan.tasks] == ["review-gate"]
    assert "review" not in {task.node_id for task in plan.tasks}


@pytest.mark.parametrize(
    ("layer", "graph_id"),
    [("fuzz", "fuzz-plan-cycle"), ("performance", "performance-plan-cycle")],
)
def test_needs_fix_routes_to_human_review(tmp_path: Path, layer: str, graph_id: str) -> None:
    compiled, _ = _load_compiled()
    tasks = [
        _task(
            "applicability",
            value={"layer": layer, "applicable": True, "reason_code": "has_cases", "case_ids": ["C1"]},
        ),
        _task("review"),
        _task(
            "review-gate",
            gate_report={"verdict": "needs_fix", "gate_id": f"{layer}-plan-review-gate"},
        ),
    ]
    plan = _plan_cycle(compiled, tmp_path, graph_id=graph_id, layer=layer, tasks=tasks, supersteps=3)
    assert [task.node_id for task in plan.tasks] == ["human-review"]


@pytest.mark.parametrize(
    ("layer", "branch_id"),
    [("fuzz", "fuzz-branch"), ("performance", "performance-branch")],
)
def test_parent_branch_starts_at_cases_only_preflight(tmp_path: Path, layer: str, branch_id: str) -> None:
    compiled, _ = _load_compiled()
    projection = GraphProjection(
        invocation_id=f"inv-{branch_id}",
        entrypoint=branch_id,
        checkpoint_ns=f"inv-{branch_id}",
        parent_invocation_id="inv-assurance",
        parent_task_id="parent-task",
        structural_path=f"assurance/{branch_id}",
        graph_digest=compiled.digest,
        contract_digests=dict(compiled.contract_digests),
        params=resolve_params(compiled.schema, {"run_mode": "full", "test_types": [layer]}),
        root_tree_id="tree-0",
        current_tree_id="tree-0",
        supersteps=0,
        tasks={},
    )
    plan = plan_superstep(compiled, projection, _context(tmp_path), _EmptyArtifacts())
    assert [task.node_id for task in plan.tasks] == ["applicability-preflight"]


@pytest.mark.parametrize(
    ("layer", "branch_id"),
    [("fuzz", "fuzz-branch"), ("performance", "performance-branch")],
)
@pytest.mark.parametrize(
    ("run_mode", "applicable", "expected_next"),
    [
        ("full", True, "plan"),
        ("full", False, "review-cycle"),
        ("codegen-only", True, "review-cycle"),
        ("codegen-only", False, "review-cycle"),
    ],
)
def test_specialty_parent_preflight_routing_matrix(
    tmp_path: Path,
    layer: str,
    branch_id: str,
    run_mode: str,
    applicable: bool,
    expected_next: str,
) -> None:
    """All four (run_mode × applicable) cells must activate exactly one successor."""
    compiled, _ = _load_compiled()
    preflight = _task(
        "applicability-preflight",
        value={
            "layer": layer,
            "applicable": applicable,
            "reason_code": "has_cases" if applicable else "no_automated_cases",
            "case_ids": ["C1"] if applicable else [],
        },
    )
    projection = GraphProjection(
        invocation_id=f"inv-{branch_id}",
        entrypoint=branch_id,
        checkpoint_ns=f"inv-{branch_id}",
        parent_invocation_id="inv-assurance",
        parent_task_id="parent-task",
        structural_path=f"assurance/{branch_id}",
        graph_digest=compiled.digest,
        contract_digests=dict(compiled.contract_digests),
        params=resolve_params(compiled.schema, {"run_mode": run_mode, "test_types": [layer]}),
        root_tree_id="tree-0",
        current_tree_id="tree-0",
        supersteps=1,
        tasks={preflight.task_id: preflight},
    )
    plan = plan_superstep(compiled, projection, _context(tmp_path), _EmptyArtifacts())
    assert [task.node_id for task in plan.tasks] == [expected_next]


@pytest.mark.parametrize("layer", ["fuzz", "performance"])
def test_real_runtime_codegen_only_applicable_smoke(tmp_path: Path, layer: str) -> None:
    """Thin real-runtime smoke; exhaustive matrix lives in test_four_layer_codegen_only."""
    from assurance_agent.artifacts.models.assurance import LayerName

    selected: tuple[LayerName, ...] = (layer,)  # type: ignore[assignment]
    fixture = make_fixture(tmp_path, selected_layers=selected, applicable_layers=selected)
    result = run_codegen_only(fixture)
    status = fixture.bundle.runtime.status(result.invocation_id).status
    assert status == "completed"
    assert any(inv.target.endswith("-codegen") for inv in fixture.adapter.invocations)
