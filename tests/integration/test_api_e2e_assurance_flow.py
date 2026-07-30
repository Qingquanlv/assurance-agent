"""Minimum no-bypass integration coverage for wired API/E2E assurance cycles."""

from __future__ import annotations

from pathlib import Path

import pytest

from assurance_agent.workflow.core.graph_events import NodeSkippedEvent
from assurance_agent.workflow.graph.compiler import compile_workflow, resolve_params
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.graph.models import (
    ExecutableTask,
    GraphProjection,
    RuntimeContext,
    TaskProjection,
)
from assurance_agent.workflow.graph.planner import plan_superstep
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2
from assurance_agent.workflow.orchestration.gates import GateEvaluationContext, check_gate_in_view
from assurance_agent.workflow.orchestration.schema import Verdict


class _EmptyArtifacts:
    def read_json(self, tree_id: str, logical_path: str):  # noqa: ARG002
        raise KeyError(logical_path)


def _load_compiled():
    schema = load_workflow_v2(Path.cwd())
    contracts = load_execution_contracts(Path.cwd())
    return compile_workflow(schema, contracts), contracts


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
        params=resolve_params(compiled.schema, {"run_mode": "full", "test_types": ["api"]}),
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
    }
    if value is not None:
        payload["value"] = value
    if gate_report is not None:
        payload["gate_report"] = gate_report
    elif status == "succeeded" and node_id == "review-gate":
        payload["gate_report"] = {"verdict": "pass", "gate_id": "api-plan-review-gate"}
    return TaskProjection(**payload)  # type: ignore[arg-type]


def _plan_cycle(compiled, tmp_path: Path, *, graph_id: str, tasks: list[TaskProjection] | None = None, supersteps: int = 0):
    return plan_superstep(
        compiled,
        _cycle_projection(compiled, graph_id=graph_id, tasks=tasks, supersteps=supersteps),
        _context(tmp_path),
        _EmptyArtifacts(),
    )


@pytest.mark.parametrize(("layer", "graph_id"), [("api", "api-plan-cycle"), ("e2e", "e2e-plan-cycle")])
def test_plan_cycle_starts_at_applicability(tmp_path: Path, layer: str, graph_id: str) -> None:
    compiled, _ = _load_compiled()
    _ = layer
    plan = _plan_cycle(compiled, tmp_path, graph_id=graph_id)
    assert [task.node_id for task in plan.tasks] == ["applicability"]


@pytest.mark.parametrize(("layer", "graph_id"), [("api", "api-plan-cycle"), ("e2e", "e2e-plan-cycle")])
def test_inapplicable_branch_skips_reviewer(tmp_path: Path, layer: str, graph_id: str) -> None:
    compiled, _ = _load_compiled()
    applicability = _task("applicability", value={"layer": layer, "applicable": False, "reason_code": "no_automated_cases", "case_ids": []})
    plan = _plan_cycle(compiled, tmp_path, graph_id=graph_id, tasks=[applicability], supersteps=1)
    assert [task.node_id for task in plan.tasks] == ["mechanical-plan-checks"]
    assert "review" not in {task.node_id for task in plan.tasks}


@pytest.mark.parametrize(("layer", "graph_id"), [("api", "api-plan-cycle"), ("e2e", "e2e-plan-cycle")])
def test_applicable_branch_reaches_review_before_mechanical(tmp_path: Path, layer: str, graph_id: str) -> None:
    compiled, _ = _load_compiled()
    applicability = _task("applicability", value={"layer": layer, "applicable": True, "reason_code": "automated_cases_present", "case_ids": ["TC-1"]})
    plan = _plan_cycle(compiled, tmp_path, graph_id=graph_id, tasks=[applicability], supersteps=1)
    assert [task.node_id for task in plan.tasks] == ["review"]


def test_fix_loop_reenters_review_not_mechanical(tmp_path: Path) -> None:
    compiled, _ = _load_compiled()
    tasks = [
        _task("applicability", value={"layer": "api", "applicable": True, "reason_code": "x", "case_ids": ["TC-1"]}),
        _task("review"),
        _task("mechanical-plan-checks"),
        _task(
            "review-gate",
            gate_report={"verdict": "needs_fix", "gate_id": "api-plan-review-gate"},
        ),
        _task("fix"),
    ]
    plan = _plan_cycle(compiled, tmp_path, graph_id="api-plan-cycle", tasks=tasks, supersteps=4)
    assert [task.node_id for task in plan.tasks] == ["review"]


def test_knowledge_remediation_route_targets_mechanical() -> None:
    compiled, _ = _load_compiled()
    cycle = compiled.schema.graphs["api-plan-cycle"]
    route = next(item for item in cycle.routes if item.from_ == "knowledge-remediation")
    assert route.cases["fix_and_proceed"] == "mechanical-plan-checks"


def test_codegen_precheck_skips_inapplicable_branch_without_codegen(tmp_path: Path) -> None:
    from tests.unit.workflow.orchestration.test_plan_check_gate import _inapplicable_checks

    compiled, _ = _load_compiled()
    schema = compiled.schema
    inapplicable_checks = _inapplicable_checks("api")
    context = GateEvaluationContext(
        project_root=tmp_path,
        repo_root=tmp_path,
        change_dir=tmp_path / "qa" / "changes" / "CH-1",
        change_id="CH-1",
        params={"force_continue": False},
        state_values={},
        node_results={"review-cycle": {"status": "succeeded"}},
        artifact_overrides={
            "review/api-plan-checks.json": inapplicable_checks,
            "repo:.aa/data-knowledge.yaml": {"version": 1, "capabilities": {"domain_factories": {}}},
        },
    )
    report = check_gate_in_view(schema.gates, "api-codegen-precondition-gate", context)
    assert report.verdict == Verdict.SKIP
    branch_route = next(route for route in schema.graphs["api-branch"].routes if route.from_ == "codegen-precheck")
    assert branch_route.cases["skip"] == "END"
    assert "codegen" not in branch_route.cases.values() or branch_route.cases.get("skip") == "END"


def test_codegen_precheck_stops_on_stale_child_without_success(tmp_path: Path) -> None:
    compiled, _ = _load_compiled()
    schema = compiled.schema
    applicable_checks = {
        "schema_version": "2",
        "layer": "api",
        "status": "pass",
        "applicability": {"layer": "api", "applicable": True, "reason_code": "automated_cases_present", "case_ids": ["TC-1"]},
        "checks": [],
    }
    context = GateEvaluationContext(
        project_root=tmp_path,
        repo_root=tmp_path,
        change_dir=tmp_path / "qa" / "changes" / "CH-1",
        change_id="CH-1",
        params={"force_continue": False},
        state_values={},
        node_results={"review-cycle": {"status": "failed"}},
        artifact_overrides={
            "review/api-plan-checks.json": applicable_checks,
            "review/api-plan-review.json": {
                "schema_version": "1.0",
                "decision": "pass",
                "review_type": "api-plan",
                "change_id": "CH-1",
                "codegen_readiness": "ready",
                "required_capabilities": [],
                "auto_fix_allowed": False,
                "human_review_required": False,
                "risk_level": "low",
                "findings": [],
                "auto_fix_plan": [],
                "next_action": "continue",
            },
            "repo:.aa/data-knowledge.yaml": {"version": 1, "capabilities": {"domain_factories": {}}},
        },
    )
    report = check_gate_in_view(schema.gates, "api-codegen-precondition-gate", context)
    assert report.verdict == Verdict.STOP
