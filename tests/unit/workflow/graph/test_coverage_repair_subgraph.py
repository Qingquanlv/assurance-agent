"""Task 7: coverage-repair subgraph wiring in the packaged workflow schema."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models.coverage_repair import COVERAGE_REPAIR_STATUS_REL
from assurance_agent.workflow.graph.handlers.operation import default_operations
from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext
from assurance_agent.workflow.graph.schema_v2 import WorkflowSchemaV2, load_workflow_v2
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.orchestration.gates import GateEvaluationContext, check_gate_in_view
from tests.unit.workflow.metrics.test_coverage_repair_probe import (
    CHANGE_ID,
    _seed_batch,
    _seed_low_risk,
)


def load_packaged_workflow_schema() -> WorkflowSchemaV2:
    return load_workflow_v2(Path.cwd())


def _assurance():
    return load_packaged_workflow_schema().graphs["assurance"]


def _coverage_repair():
    return load_packaged_workflow_schema().graphs["coverage-repair"]


def _gate_ctx(project_root: Path, change_dir: Path, **params: object) -> GateEvaluationContext:
    return GateEvaluationContext(
        project_root=project_root,
        repo_root=project_root,
        change_dir=change_dir,
        change_id=CHANGE_ID,
        params=dict(params),
        state_values={},
        node_results={},
    )


def _neighbors(graph) -> dict[str, set[str]]:
    """Outgoing adjacency: edges + route cases (excluding START)."""
    out: dict[str, set[str]] = {node_id: set() for node_id in graph.nodes}
    out["START"] = set()
    out["END"] = set()
    out["STOP"] = set()
    for edge in graph.edges:
        out.setdefault(edge.from_, set()).add(edge.to)
    for route in graph.routes:
        for target in route.cases.values():
            out.setdefault(route.from_, set()).add(target)
        if route.default:
            out.setdefault(route.from_, set()).add(route.default)
    return out


def _reachable_from(graph, start: str) -> set[str]:
    adj = _neighbors(graph)
    seen: set[str] = set()
    queue: deque[str] = deque([start])
    while queue:
        node = queue.popleft()
        if node in seen:
            continue
        seen.add(node)
        for nxt in adj.get(node, ()):
            if nxt not in seen:
                queue.append(nxt)
    return seen


def _all_paths(graph, start: str, goal: str) -> list[list[str]]:
    """Simple paths from start to goal (no cycles)."""
    adj = _neighbors(graph)
    paths: list[list[str]] = []

    def walk(node: str, trail: list[str]) -> None:
        if node == goal:
            paths.append(trail + [node])
            return
        for nxt in adj.get(node, ()):
            if nxt in trail or nxt in {"STOP"}:
                continue
            walk(nxt, trail + [node])

    walk(start, [])
    return paths


# ---------------------------------------------------------------------------
# Step 1 / 1b: first-pass STOP bug + schema loadability
# ---------------------------------------------------------------------------


def test_packaged_schema_loads_with_the_coverage_repair_subgraph() -> None:
    schema = load_packaged_workflow_schema()
    graph = schema.graphs["coverage-repair"]
    assert graph.max_supersteps > 0  # required field, no default (schema_v2.py:203)


def test_entry_gate_reads_do_not_include_status_json() -> None:
    gate = load_packaged_workflow_schema().gates["coverage-repair-entry-gate"]
    paths = {entry.path for entry in gate.reads}
    assert "coverage-repair/status.json" not in paths
    assert paths == {"coverage-repair/brief.json"}


def test_first_pass_with_nothing_to_repair_skips_instead_of_stopping(tmp_path: Path) -> None:
    """Design v3-1: entry gate must skip when brief says ineligible and status is absent.

    Gate evaluation applies ``missing_field_is`` before ``missing_file_is``. If the
    entry gate also read ``status.json``, a first pass with nothing to repair would
    STOP instead of skip.
    """
    project_root, change_dir = _seed_low_risk(tmp_path)
    _seed_batch(change_dir, constraint_covered=4)

    ops = default_operations()
    probe = ops["operation:probe-coverage-repair-need"]
    workspace = TaskWorkspace(
        task_id="t1",
        root=project_root,
        project_root=project_root,
        repo_root=project_root,
        change_dir=change_dir,
        base_tree_id="tree-0",
    )
    context = RuntimeContext.model_construct(
        project_root=project_root,
        repo_root=project_root,
        change_dir=change_dir,
        change_id=CHANGE_ID,
        params={},
    )
    task = ExecutableTask.model_construct(
        task_id="t1",
        node_id="probe-entry",
        graph_id="coverage-repair",
        target="operation:probe-coverage-repair-need",
        input={"with": {}},
    )
    result = probe(task, workspace, context)
    assert result.status == "succeeded"
    assert not (change_dir / COVERAGE_REPAIR_STATUS_REL).exists()

    schema = load_packaged_workflow_schema()
    report = check_gate_in_view(
        schema.gates,
        "coverage-repair-entry-gate",
        _gate_ctx(project_root, change_dir),
    )
    assert report.verdict.value == "skip", (
        f"expected skip on first pass with nothing to repair; got {report.verdict.value} "
        f"(matched={report.matched_rule!r}). A stop here is the v3-1 bug."
    )


# ---------------------------------------------------------------------------
# Step 2: wiring
# ---------------------------------------------------------------------------


def test_coverage_repair_sits_between_healing_and_materialize() -> None:
    assurance = _assurance()
    edge_pairs = {(e.from_, e.to) for e in assurance.edges}
    assert assurance.nodes["coverage-repair"].uses == "graph:coverage-repair"
    assert ("healing", "coverage-repair") in edge_pairs
    assert ("coverage-repair", "materialize-pr-metrics") in edge_pairs
    assert ("healing", "materialize-pr-metrics") not in edge_pairs


def test_subgraph_budget_limit_is_params_max_coverage_repair_attempts() -> None:
    graph = _coverage_repair()
    assert "coverage_repair_attempts" in graph.budgets
    assert graph.budgets["coverage_repair_attempts"].limit == "params.max_coverage_repair_attempts"
    assert load_packaged_workflow_schema().params["max_coverage_repair_attempts"].default == 1


def test_repair_node_declares_apply_summary_with_change_root() -> None:
    repair = _coverage_repair().nodes["repair"]
    assert "change:coverage-repair/apply-summary.json" in repair.outputs


def test_every_path_from_repair_to_rerun_passes_through_compute_safety_then_safety() -> None:
    graph = _coverage_repair()
    paths = _all_paths(graph, "repair", "rerun")
    assert paths, "expected at least one path from repair to rerun"
    for path in paths:
        assert "compute-safety" in path
        assert "safety" in path
        assert path.index("compute-safety") < path.index("safety") < path.index("rerun")


def test_end_predecessors_reachable_from_repair_are_downstream_of_rerun() -> None:
    graph = _coverage_repair()
    from_repair = _reachable_from(graph, "repair")
    from_rerun = _reachable_from(graph, "rerun")
    end_preds = {edge.from_ for edge in graph.edges if edge.to == "END"}
    for pred in end_preds & from_repair:
        assert pred in from_rerun, (
            f"{pred} is reachable from repair and edges to END, but is not downstream of rerun"
        )


def test_subgraph_never_declares_inspect_metrics_output() -> None:
    graph = _coverage_repair()
    for node_id, node in graph.nodes.items():
        assert "change:inspect/metrics.json" not in node.outputs, (
            f"node {node_id} must not declare change:inspect/metrics.json"
        )


def test_metrics_sufficiency_review_actions_unchanged() -> None:
    interrupt = _assurance().nodes["metrics-sufficiency-review"].interrupt
    assert interrupt is not None
    assert list(interrupt.actions) == ["accept_risk", "stop"]


def test_safety_gate_reads_no_healing_path() -> None:
    gate = load_packaged_workflow_schema().gates["coverage-repair-safety-gate"]
    for entry in gate.reads:
        assert not entry.path.startswith("healing/"), entry.path
    assert {entry.path for entry in gate.reads} == {"coverage-repair/safety-check.json"}


# ---------------------------------------------------------------------------
# Step 3: gate truth
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_yaml(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


@pytest.mark.parametrize(
    ("final_status", "eligible", "expected"),
    [
        ("FAIL", True, "reject"),
        ("PASS", False, "exit"),
        ("PASS", True, "continue"),
    ],
    ids=["fail-reject", "ineligible-exit", "eligible-continue"],
)
def test_loop_gate_three_states(tmp_path: Path, final_status: str, eligible: bool, expected: str) -> None:
    project_root = tmp_path
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    _write_json(
        change_dir / "coverage-repair" / "brief.json",
        {
            "schema_version": "1",
            "change_id": CHANGE_ID,
            "batch_id": "b1",
            "probe_verdict": "needs_human" if eligible else "pass",
            "eligible": eligible,
            "shortboards": [],
            "repair_items": (
                [
                    {
                        "kind": "constraint_without_property",
                        "locator": {"constraint_key": "k"},
                        "metric": "constraint_coverage",
                    }
                ]
                if eligible
                else []
            ),
            "deferred_to_intake": [],
        },
    )
    _write_json(
        change_dir / "coverage-repair" / "status.json",
        {
            "schema_version": "1",
            "change_id": CHANGE_ID,
            "status": "in_progress",
            "attempts_used": 1,
            "last_batch_id": "b1",
            "deferred_to_intake": [],
        },
    )
    _write_yaml(
        change_dir / "execution" / "execution-manifest.yaml",
        {"final_status": final_status, "batch_id": "b1"},
    )
    (project_root / ".aa").mkdir(parents=True, exist_ok=True)

    report = check_gate_in_view(
        load_packaged_workflow_schema().gates,
        "coverage-repair-loop-gate",
        _gate_ctx(project_root, change_dir),
    )
    assert report.verdict.value == expected


@pytest.mark.parametrize(
    ("safety", "expected"),
    [
        (
            {
                "passed": True,
                "needs_review": False,
                "product_code_modified": False,
                "declaration_files_modified": False,
                "skip_or_xfail_added": False,
                "stale_summary": False,
                "unbriefed_files_modified": [],
            },
            "pass",
        ),
        (
            {
                "passed": True,
                "needs_review": False,
                "product_code_modified": False,
                "declaration_files_modified": False,
                "skip_or_xfail_added": False,
                "stale_summary": True,
                "unbriefed_files_modified": [],
            },
            "needs_human_review",
        ),
        (
            {
                "passed": False,
                "needs_review": True,
                "product_code_modified": False,
                "declaration_files_modified": False,
                "skip_or_xfail_added": False,
                "stale_summary": False,
                "unbriefed_files_modified": ["tests/unrelated.py"],
            },
            "needs_human_review",
        ),
    ],
    ids=["pass", "stale-summary", "unbriefed"],
)
def test_safety_gate_pass_and_needs_human_review(
    tmp_path: Path, safety: dict[str, object], expected: str
) -> None:
    project_root = tmp_path
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    payload = {
        "schema_version": "1",
        "change_id": CHANGE_ID,
        "attempt": 1,
        **safety,
    }
    _write_json(change_dir / "coverage-repair" / "safety-check.json", payload)
    (project_root / ".aa").mkdir(parents=True, exist_ok=True)

    report = check_gate_in_view(
        load_packaged_workflow_schema().gates,
        "coverage-repair-safety-gate",
        _gate_ctx(project_root, change_dir),
    )
    assert report.verdict.value == expected
