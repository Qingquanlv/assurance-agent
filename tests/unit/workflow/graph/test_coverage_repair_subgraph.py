"""Task 7/8: coverage-repair subgraph wiring + runtime termination proofs."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from assurance_agent.artifacts.models.coverage_repair import (
    COVERAGE_REPAIR_APPLY_SUMMARY_REL,
    COVERAGE_REPAIR_BASELINE_REL,
    COVERAGE_REPAIR_SAFETY_REL,
    COVERAGE_REPAIR_STATUS_REL,
    CoverageRepairApplySummary,
    CoverageRepairBaseline,
    CoverageRepairSafetyCheck,
    CoverageRepairStatus,
)
from assurance_agent.workflow.driver.operations_catalog import default_operations
from assurance_agent.workflow.driver.runtime_factory import assemble_graph_runtime
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.graph.handlers.operation import OperationHandler
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.runtime import GraphRuntime
from assurance_agent.workflow.graph.schema_v2 import (
    EdgeDef,
    EntrypointDef,
    GraphDef,
    NodeDef,
    WorkflowSchemaV2,
    load_workflow_v2,
)
from assurance_agent.workflow.graph.task_runner import HandlerNodeRunner, build_default_node_runner
from assurance_agent.workflow.graph.workspace import TaskWorkspace
from assurance_agent.workflow.orchestration.gates import GateEvaluationContext, check_gate_in_view
from tests.unit.workflow.metrics.test_coverage_repair_probe import (
    CHANGE_ID,
    _seed_batch,
    _seed_low_risk,
    _write_gaps,
)
from tests.unit.workflow.metrics.test_pr_metrics_materialize import BATCH_NEW

CASE_ID = "TC_API_001"
BRIEFED_TEST = f"tests/api/test_{CASE_ID.lower()}.py"
UNRELATED_TEST = "tests/unit/test_unrelated.py"
BATCH_AFTER_RERUN = "20260805-120000"
T0 = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)


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
    ("final_status", "eligible", "probe_verdict", "expected"),
    [
        ("FAIL", True, "needs_human", "reject"),
        ("PASS", False, "pass", "exit"),
        ("PASS", False, "needs_human", "skip"),
        ("PASS", False, "reject", "reject"),
        ("PASS", True, "needs_human", "continue"),
    ],
    ids=["fail-reject", "repaired-exit", "unrepairable-skip", "probe-reject", "eligible-continue"],
)
def test_loop_gate_states(
    tmp_path: Path,
    final_status: str,
    eligible: bool,
    probe_verdict: str,
    expected: str,
) -> None:
    project_root = tmp_path
    change_dir = project_root / "qa" / "changes" / CHANGE_ID
    _write_json(
        change_dir / "coverage-repair" / "brief.json",
        {
            "schema_version": "1",
            "change_id": CHANGE_ID,
            "batch_id": "b1",
            "probe_verdict": probe_verdict,
            "eligible": eligible,
            "allowed_test_files": ["tests/api/test_management.py"] if eligible else [],
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


# ---------------------------------------------------------------------------
# Task 8: runtime termination / disable / safety wiring
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self._now = start
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def sleep(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds


class ScriptedRepairInvoker:
    """Stub ``skill:aa-coverage-repair`` — never calls a live agent."""

    def __init__(self, script: Callable[[AgentRequest, int], AgentResult]) -> None:
        self._script = script
        self.calls: list[AgentRequest] = []

    def invoke(self, request: AgentRequest) -> AgentResult:
        assert request.target == "skill:aa-coverage-repair", request.target
        self.calls.append(request)
        return self._script(request, len(self.calls))


def _change_root(workspace_root: Path, change_id: str) -> Path:
    return workspace_root / "qa" / "changes" / change_id


def _read_baseline(change_root: Path) -> CoverageRepairBaseline:
    return CoverageRepairBaseline.model_validate_json(
        (change_root / COVERAGE_REPAIR_BASELINE_REL).read_text(encoding="utf-8")
    )


def _write_apply_summary(
    change_root: Path,
    *,
    baseline: CoverageRepairBaseline,
    applied: bool,
    files_modified: tuple[str, ...] = (),
) -> None:
    summary = CoverageRepairApplySummary(
        change_id=baseline.change_id,
        attempt=baseline.attempt,
        attempt_token=baseline.attempt_token,
        applied=applied,
        files_modified=files_modified,
    )
    out = change_root / COVERAGE_REPAIR_APPLY_SUMMARY_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(summary.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _write_manifest(change_dir: Path, batch_id: str, *, final_status: str = "PASS") -> None:
    _write_yaml(
        change_dir / "execution" / "execution-manifest.yaml",
        {
            "schema_version": "1.0",
            "change_id": CHANGE_ID,
            "batch_id": batch_id,
            "executed_at": "2026-08-05T11:00:00+00:00",
            "selected_targets": {"api": True, "e2e": False, "fuzz": False, "performance": False},
            "result_files": {},
            "final_status": final_status,
        },
    )


def _seed_repair_project(tmp_path: Path) -> tuple[Path, Path]:
    """Eligible shortfall + cases/ on disk (declaration roots must stay consistent)."""
    project_root, change_dir = _seed_low_risk(tmp_path)
    _seed_batch(change_dir, constraint_covered=1)
    _write_gaps(
        change_dir,
        BATCH_NEW,
        [
            {
                "kind": "constraint_without_property",
                "locator": {"constraint_key": "entities.dept.constraints.name_unique"},
                "layer": "execution",
                "batch_id": BATCH_NEW,
            }
        ],
    )
    (change_dir / "plans" / "api-codegen-plan.md").write_text(
        f"## Test Function Mapping\n\n| Case | Test |\n| --- | --- |\n| {CASE_ID} | `{BRIEFED_TEST}` |\n",
        encoding="utf-8",
    )
    _write_manifest(change_dir, BATCH_NEW)
    (project_root / "qa" / "cases").mkdir(parents=True, exist_ok=True)
    (project_root / "app").mkdir(parents=True, exist_ok=True)
    (project_root / "app" / "service.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
    (project_root / "tests" / "api").mkdir(parents=True, exist_ok=True)
    (project_root / "tests" / "unit").mkdir(parents=True, exist_ok=True)
    (project_root / BRIEFED_TEST).write_text(
        f"def test_{CASE_ID.lower()}__happy():\n    assert True\n",
        encoding="utf-8",
    )
    (project_root / UNRELATED_TEST).write_text("def test_other():\n    assert True\n", encoding="utf-8")
    return project_root, change_dir


def _compile_harness(*, with_materialize: bool = False) -> tuple[CompiledWorkflow, Any, str]:
    schema = load_packaged_workflow_schema()
    graphs = dict(schema.graphs)
    entrypoints = dict(schema.entrypoints)
    if with_materialize:
        graphs["coverage-repair-parent"] = GraphDef(
            max_supersteps=8,
            nodes={
                "coverage-repair": NodeDef(uses="graph:coverage-repair"),
                "materialize-pr-metrics": NodeDef(
                    uses="operation:materialize-pr-metrics",
                    outputs=["change:inspect/metrics.json"],
                    retry="never",
                    timeout="local-operation",
                ),
            },
            edges=[
                EdgeDef.model_validate({"from": "START", "to": "coverage-repair"}),
                EdgeDef.model_validate({"from": "coverage-repair", "to": "materialize-pr-metrics"}),
                EdgeDef.model_validate({"from": "materialize-pr-metrics", "to": "END"}),
            ],
        )
        entry = "coverage-repair-parent-ep"
        entrypoints[entry] = EntrypointDef(graph="coverage-repair-parent", restart="repeatable")
    else:
        entry = "coverage-repair-direct"
        entrypoints[entry] = EntrypointDef(graph="coverage-repair", restart="repeatable")
    schema = schema.model_copy(update={"graphs": graphs, "entrypoints": entrypoints})
    contracts = load_execution_contracts(Path.cwd())
    return compile_workflow(schema, contracts), contracts, entry


def _activated_nodes(change_dir: Path) -> list[str]:
    path = change_dir / "events.jsonl"
    if not path.is_file():
        return []
    order: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("type") == "node_activated" and event.get("node_id"):
            order.append(str(event["node_id"]))
    return order


def _repair_attempt_error_kinds(change_dir: Path) -> list[str]:
    """Join attempt-failed events to repair via task_attempt_started (failed events omit node_id)."""
    path = change_dir / "events.jsonl"
    repair_attempts: set[str] = set()
    kinds: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("type") == "task_attempt_started" and event.get("node_id") == "repair":
            repair_attempts.add(str(event["attempt_id"]))
        elif event.get("type") == "task_attempt_failed" and str(event.get("attempt_id")) in repair_attempts:
            kinds.append(str(event["error_kind"]))
    return kinds


def _stub_post_repair_ops(batch_id: str = BATCH_AFTER_RERUN) -> dict[str, Any]:
    def run_tests(task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext) -> TaskResult:
        del task, context
        # Only write under change:execution/** (run-tests authorization). Keep the
        # seeded inspect/coverage-gaps.json so the loop stays eligible.
        _seed_batch(workspace.change_dir, batch_id, constraint_covered=1)
        _write_manifest(workspace.change_dir, batch_id)
        return TaskResult(status="succeeded")

    def collect(task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext) -> TaskResult:
        del task, workspace, context
        return TaskResult(status="succeeded")

    return {
        "operation:run-tests": run_tests,
        "operation:run-tests-and-collect-pr-metrics": run_tests,
        "operation:collect-pr-metrics-batch": collect,
    }


def _build_runtime(
    project: Path,
    compiled: CompiledWorkflow,
    contracts: Any,
    invoker: Any,
    *,
    extra_ops: dict[str, Any] | None = None,
) -> GraphRuntime:
    def build(store, run_child):  # type: ignore[no-untyped-def]
        node_runner = build_default_node_runner(
            invoker,
            store,
            contracts,
            compiled=compiled,
            operations=default_operations(),
            run_child=run_child,
        )
        assert isinstance(node_runner, HandlerNodeRunner)
        if extra_ops:
            op_handler = OperationHandler({**default_operations(), **extra_ops})
            node_runner._handlers.update({target: op_handler for target in extra_ops})  # noqa: SLF001

        class _InspectStub:
            def execute(self, task, workspace, context):  # type: ignore[no-untyped-def]
                del task, workspace, context
                return TaskResult(status="succeeded")

        node_runner._handlers["graph:inspect-with-issues"] = _InspectStub()  # noqa: SLF001
        return node_runner

    return assemble_graph_runtime(
        project_root=project,
        change_dir=project / "qa" / "changes" / CHANGE_ID,
        compiled=compiled,
        contracts=contracts,
        build_node_runner=build,
        clock=FakeClock(),
    )


def _context(project: Path, *, max_attempts: int) -> RuntimeContext:
    return RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=project / "qa" / "changes" / CHANGE_ID,
        change_id=CHANGE_ID,
        params={"max_coverage_repair_attempts": max_attempts},
    )


def _declared_noop_script(request: AgentRequest, attempt_n: int) -> AgentResult:
    del attempt_n
    change = _change_root(Path(request.workspace_root), request.change_id)
    baseline = _read_baseline(change)
    _write_apply_summary(change, baseline=baseline, applied=False, files_modified=())
    return AgentResult(ok=True)


def _clean_repair_script(request: AgentRequest, attempt_n: int) -> AgentResult:
    del attempt_n
    root = Path(request.workspace_root)
    change = _change_root(root, request.change_id)
    baseline = _read_baseline(change)
    target = root / BRIEFED_TEST
    target.write_text(
        f"def test_{CASE_ID.lower()}__happy():\n    assert 1 == 1\n",
        encoding="utf-8",
    )
    _write_apply_summary(
        change,
        baseline=baseline,
        applied=True,
        files_modified=(BRIEFED_TEST,),
    )
    return AgentResult(ok=True)


def _concealment_script(request: AgentRequest, attempt_n: int) -> AgentResult:
    del attempt_n
    root = Path(request.workspace_root)
    change = _change_root(root, request.change_id)
    baseline = _read_baseline(change)
    (root / UNRELATED_TEST).write_text("def test_other():\n    assert 1 == 1\n", encoding="utf-8")
    # Report only a briefed path — conceal the unbriefed mechanical edit.
    _write_apply_summary(
        change,
        baseline=baseline,
        applied=True,
        files_modified=(BRIEFED_TEST,),
    )
    return AgentResult(ok=True)


def _stale_summary_script(request: AgentRequest, attempt_n: int) -> AgentResult:
    root = Path(request.workspace_root)
    if attempt_n == 1:
        return _clean_repair_script(request, attempt_n)
    # Attempt 2: change a file but leave the attempt-1 summary untouched.
    (root / BRIEFED_TEST).write_text(
        f"def test_{CASE_ID.lower()}__happy():\n    assert 2 == 2\n",
        encoding="utf-8",
    )
    return AgentResult(ok=True)


def _missing_summary_script(request: AgentRequest, attempt_n: int) -> AgentResult:
    del request, attempt_n
    return AgentResult(ok=True)


# Keep early: if declaration_roots diverge between allocate and safety, this fails
# and every other repair test that expects progress fails with it.
def test_clean_repair_reaches_rerun_with_safety_pass(tmp_path: Path) -> None:
    """Design v4-6: briefed test edit + cases/ on disk → safety pass → rerun."""
    project, change_dir = _seed_repair_project(tmp_path)
    compiled, contracts, entry = _compile_harness()
    invoker = ScriptedRepairInvoker(_clean_repair_script)
    runtime = _build_runtime(project, compiled, contracts, invoker, extra_ops=_stub_post_repair_ops())

    result = runtime.run(compiled, entry, _context(project, max_attempts=1))

    activated = _activated_nodes(change_dir)
    assert "rerun" in activated
    assert activated.index("safety") < activated.index("rerun")
    safety = CoverageRepairSafetyCheck.model_validate_json(
        (change_dir / COVERAGE_REPAIR_SAFETY_REL).read_text(encoding="utf-8")
    )
    assert safety.passed is True
    assert safety.needs_review is False
    assert safety.declaration_files_modified is False
    # Exhausted after one unrepaired attempt (stub keeps the shortfall).
    assert result.status.status == "completed", result.reason
    assert "complete-exhausted" in activated


def test_budget_exhaustion_reaches_complete_exhausted_with_one_allocate(tmp_path: Path) -> None:
    project, change_dir = _seed_repair_project(tmp_path)
    compiled, contracts, entry = _compile_harness()
    invoker = ScriptedRepairInvoker(_declared_noop_script)
    runtime = _build_runtime(project, compiled, contracts, invoker, extra_ops=_stub_post_repair_ops())

    result = runtime.run(compiled, entry, _context(project, max_attempts=1))

    assert result.status.status == "completed", result.reason
    activated = _activated_nodes(change_dir)
    assert activated.count("allocate") == 1
    assert "complete-exhausted" in activated
    assert "repair" in activated
    status = CoverageRepairStatus.model_validate_json(
        (change_dir / COVERAGE_REPAIR_STATUS_REL).read_text(encoding="utf-8")
    )
    assert status.status == "exhausted"
    assert status.attempts_used == 1


def test_max_attempts_zero_skips_repair_and_still_materializes(tmp_path: Path) -> None:
    """Escape hatch must be provably inert: budget 0 → no repair, materialize still runs."""
    project, change_dir = _seed_repair_project(tmp_path)
    compiled, contracts, entry = _compile_harness(with_materialize=True)
    invoker = ScriptedRepairInvoker(_clean_repair_script)
    runtime = _build_runtime(project, compiled, contracts, invoker, extra_ops=_stub_post_repair_ops())

    result = runtime.run(compiled, entry, _context(project, max_attempts=0))

    assert result.status.status == "completed", result.reason
    assert invoker.calls == []
    activated = _activated_nodes(change_dir)
    assert "repair" not in activated
    assert "allocate" not in activated
    assert "complete-exhausted" in activated
    assert "materialize-pr-metrics" in activated
    assert (change_dir / "inspect" / "metrics.json").is_file()


def test_post_fix_reprobe_reads_strictly_newer_batch_id(tmp_path: Path) -> None:
    project, change_dir = _seed_repair_project(tmp_path)
    compiled, contracts, entry = _compile_harness()
    batch_ids: list[str] = []

    def tracking_probe(task, workspace, context):  # type: ignore[no-untyped-def]
        result = default_operations()["operation:probe-coverage-repair-need"](task, workspace, context)
        if result.status == "succeeded" and isinstance(result.value, dict):
            batch_ids.append(str(result.value.get("batch_id")))
        return result

    invoker = ScriptedRepairInvoker(_declared_noop_script)
    extra = _stub_post_repair_ops()
    extra["operation:probe-coverage-repair-need"] = tracking_probe
    runtime = _build_runtime(project, compiled, contracts, invoker, extra_ops=extra)

    result = runtime.run(compiled, entry, _context(project, max_attempts=1))

    assert result.status.status == "completed", result.reason
    assert len(batch_ids) >= 2, batch_ids
    assert batch_ids[0] == BATCH_NEW
    assert batch_ids[1] == BATCH_AFTER_RERUN
    assert batch_ids[1] > batch_ids[0]


def test_declared_noop_exhausts_rather_than_stopping(tmp_path: Path) -> None:
    """Design v4-3: applied=false + no file edits is the legitimate exhausted path."""
    project, change_dir = _seed_repair_project(tmp_path)
    compiled, contracts, entry = _compile_harness()
    invoker = ScriptedRepairInvoker(_declared_noop_script)
    runtime = _build_runtime(project, compiled, contracts, invoker, extra_ops=_stub_post_repair_ops())

    result = runtime.run(compiled, entry, _context(project, max_attempts=1))

    assert result.status.status == "completed", result.reason
    activated = _activated_nodes(change_dir)
    assert "complete-exhausted" in activated
    assert result.status.status != "stopped"
    safety = CoverageRepairSafetyCheck.model_validate_json(
        (change_dir / COVERAGE_REPAIR_SAFETY_REL).read_text(encoding="utf-8")
    )
    assert safety.passed is True
    assert safety.needs_review is False
    status = CoverageRepairStatus.model_validate_json(
        (change_dir / COVERAGE_REPAIR_STATUS_REL).read_text(encoding="utf-8")
    )
    assert status.status == "exhausted"


def test_missing_apply_summary_is_invalid_output_not_exhausted(tmp_path: Path) -> None:
    """Design v4-3: a vanished declared output is skill malfunction, not exhaustion.

    ``_ensure_outputs_frozen`` → ``freeze_write_set`` (finalize.py:134-142) maps a
    missing declared output to ``invalid_output``. Laundering that into ``exhausted``
    would also let safety run without a summary to cross-check against.
    """
    project, change_dir = _seed_repair_project(tmp_path)
    compiled, contracts, entry = _compile_harness()
    invoker = ScriptedRepairInvoker(_missing_summary_script)
    runtime = _build_runtime(project, compiled, contracts, invoker, extra_ops=_stub_post_repair_ops())

    result = runtime.run(compiled, entry, _context(project, max_attempts=1))

    assert result.status.status == "failed", result.reason
    assert "repair" in (result.reason or "")
    assert "invalid_output" in (result.reason or "")
    kinds = _repair_attempt_error_kinds(change_dir)
    assert kinds, "expected repair attempt failures"
    assert all(kind == "invalid_output" for kind in kinds), kinds
    activated = _activated_nodes(change_dir)
    assert "complete-exhausted" not in activated
    assert "compute-safety" not in activated


def test_anti_concealment_routes_to_needs_human_review(tmp_path: Path) -> None:
    """Design v4-2: unbriefed edit + briefed-only summary → safety gate interrupt."""
    project, change_dir = _seed_repair_project(tmp_path)
    compiled, contracts, entry = _compile_harness()
    invoker = ScriptedRepairInvoker(_concealment_script)
    runtime = _build_runtime(project, compiled, contracts, invoker, extra_ops=_stub_post_repair_ops())

    result = runtime.run(compiled, entry, _context(project, max_attempts=1))

    assert result.status.status == "interrupted", result.reason
    activated = _activated_nodes(change_dir)
    assert "safety-interrupt" in activated
    assert "rerun" not in activated
    safety = CoverageRepairSafetyCheck.model_validate_json(
        (change_dir / COVERAGE_REPAIR_SAFETY_REL).read_text(encoding="utf-8")
    )
    assert UNRELATED_TEST in safety.unbriefed_files_modified
    assert safety.summary_mismatch is True
    assert safety.needs_review is True


def test_stale_summary_on_attempt_two_routes_to_needs_human_review(tmp_path: Path) -> None:
    """Design v4-5: attempt-2 keeps attempt-1 summary → freeze ok, stale_summary stops progress."""
    project, change_dir = _seed_repair_project(tmp_path)
    compiled, contracts, entry = _compile_harness()
    invoker = ScriptedRepairInvoker(_stale_summary_script)
    runtime = _build_runtime(
        project,
        compiled,
        contracts,
        invoker,
        extra_ops=_stub_post_repair_ops(),
    )

    result = runtime.run(compiled, entry, _context(project, max_attempts=2))

    assert result.status.status == "interrupted", result.reason
    assert len(invoker.calls) == 2
    activated = _activated_nodes(change_dir)
    assert activated.count("allocate") == 2
    assert activated.count("repair") == 2
    assert "safety-interrupt" in activated
    safety = CoverageRepairSafetyCheck.model_validate_json(
        (change_dir / COVERAGE_REPAIR_SAFETY_REL).read_text(encoding="utf-8")
    )
    assert safety.attempt == 2
    assert safety.stale_summary is True
    assert safety.needs_review is True
    # Mechanical fields describe attempt 2's diff (re-edit of the briefed test), not attempt 1's.
    assert BRIEFED_TEST in safety.test_files_changed
    assert safety.summary_applied is True  # leftover attempt-1 claim, not attempt-2 truth
