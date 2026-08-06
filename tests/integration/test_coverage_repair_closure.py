"""Task 9: coverage-repair end-to-end closure + fail-closed backstop.

Deterministic in-process GraphRuntime over the packaged ``coverage-repair``
subgraph, then real ``materialize-pr-metrics`` + ``metrics-sufficiency-gate``.
The repair skill is a scripted writer (no live agent).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from assurance_agent.artifacts.models.coverage_repair import (
    COVERAGE_REPAIR_APPLY_SUMMARY_REL,
    COVERAGE_REPAIR_BASELINE_REL,
    COVERAGE_REPAIR_BRIEF_REL,
    COVERAGE_REPAIR_SAFETY_REL,
    COVERAGE_REPAIR_STATUS_REL,
    CoverageRepairApplySummary,
    CoverageRepairBaseline,
    CoverageRepairBrief,
    CoverageRepairSafetyCheck,
    CoverageRepairStatus,
)
from assurance_agent.artifacts.models.metrics import MetricScope, MetricsDocument
from assurance_agent.artifacts.models.pr_metric_evidence import ConstraintCoverageEvidence
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from assurance_agent.workflow.graph.checkpoint import CheckpointStore
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.graph.handlers.operation import OperationHandler, default_operations
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.runtime import GraphRuntime
from assurance_agent.workflow.graph.scheduler import Scheduler
from assurance_agent.workflow.graph.schema_v2 import (
    EdgeDef,
    EntrypointDef,
    GraphDef,
    NodeDef,
    RouteDef,
    load_workflow_v2,
)
from assurance_agent.workflow.graph.task_runner import HandlerNodeRunner, build_default_node_runner
from assurance_agent.workflow.graph.workspace import TaskWorkspace, TreeStore, WorkspaceBackend
from assurance_agent.workflow.metrics.batch_io import write_batch_evidence
from assurance_agent.workflow.metrics.pr_metrics import INSPECT_METRICS_REL, METRICS_SOURCE_BATCH_REL
from assurance_agent.workflow.orchestration.gates import GateEvaluationContext, check_gate_in_view
from tests.unit.workflow.metrics.test_coverage_repair_probe import (
    CHANGE_ID,
    _seed_batch,
    _seed_low_risk,
    _write_gaps,
)
from tests.unit.workflow.metrics.test_pr_metrics_materialize import BATCH_NEW

CONSTRAINT_KEY = "name_unique"
PROPERTY_TEST = f"tests/api/test_{CONSTRAINT_KEY}_property.py"
BATCH_AFTER_RERUN = "20260805-120000"
T0 = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Harness (Task 8 GraphRuntime pattern + parent through metrics-sufficiency)
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
    def __init__(self, script: Callable[[AgentRequest, int], AgentResult]) -> None:
        self._script = script
        self.calls: list[AgentRequest] = []

    def invoke(self, request: AgentRequest) -> AgentResult:
        assert request.target == "skill:aa-coverage-repair", request.target
        self.calls.append(request)
        return self._script(request, len(self.calls))


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_yaml(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


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


def _rewrite_constraint_coverage(
    change_dir: Path,
    batch_id: str,
    *,
    covered: int,
    uncovered: tuple[str, ...],
) -> None:
    declared = MetricScope.of(total=4, covered=covered, uncovered=uncovered)
    write_batch_evidence(
        change_dir,
        batch_id,
        "constraint-coverage.json",
        ConstraintCoverageEvidence(
            schema_version="1",
            change_id=CHANGE_ID,
            batch_id=batch_id,
            declared=declared,
            value=declared.value,
        ),
    )


def _constraint_gap(*, layer: str = "execution") -> dict[str, object]:
    return {
        "kind": "constraint_without_property",
        "locator": {"constraint_key": CONSTRAINT_KEY},
        "layer": layer,
        "batch_id": BATCH_NEW,
    }


def _declaration_gap() -> dict[str, object]:
    return {
        "kind": "uncovered_required_case",
        "locator": {"case_id": "TC_DECL_001"},
        "layer": "declaration",
        "batch_id": BATCH_NEW,
    }


def _seed_shortfall_project(
    tmp_path: Path,
    *,
    gaps: list[dict[str, object]] | None = None,
    constraint_covered: int = 1,
    uncovered: tuple[str, ...] = (CONSTRAINT_KEY, "k2", "k3"),
) -> tuple[Path, Path]:
    """Low-risk shortfall + cases/ on disk (declaration roots stay consistent)."""
    project_root, change_dir = _seed_low_risk(tmp_path)
    _seed_batch(change_dir, constraint_covered=constraint_covered)
    if constraint_covered < 4:
        _rewrite_constraint_coverage(
            change_dir, BATCH_NEW, covered=constraint_covered, uncovered=uncovered
        )
    if gaps is not None:
        _write_gaps(change_dir, BATCH_NEW, gaps)
    _write_manifest(change_dir, BATCH_NEW)
    (project_root / "qa" / "cases").mkdir(parents=True, exist_ok=True)
    (project_root / "app").mkdir(parents=True, exist_ok=True)
    (project_root / "app" / "service.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
    (project_root / "tests" / "api").mkdir(parents=True, exist_ok=True)
    return project_root, change_dir


def _seed_passing_project(tmp_path: Path) -> tuple[Path, Path]:
    project_root, change_dir = _seed_low_risk(tmp_path)
    _seed_batch(change_dir, constraint_covered=4)
    _write_manifest(change_dir, BATCH_NEW)
    (project_root / "qa" / "cases").mkdir(parents=True, exist_ok=True)
    (project_root / "app").mkdir(parents=True, exist_ok=True)
    (project_root / "app" / "service.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
    (project_root / "tests" / "api").mkdir(parents=True, exist_ok=True)
    return project_root, change_dir


def _seed_collection_gap_project(tmp_path: Path) -> tuple[Path, Path]:
    project_root, change_dir = _seed_passing_project(tmp_path)
    corrupt = change_dir / "execution" / "runs" / BATCH_NEW / "coverage-diff.json"
    corrupt.write_text("{not-json", encoding="utf-8")
    return project_root, change_dir


def _compile_closure_harness() -> tuple[CompiledWorkflow, Any, str]:
    """Parent: coverage-repair → materialize → metrics-sufficiency → (report on reject)."""
    schema = load_workflow_v2(Path.cwd())
    graphs = dict(schema.graphs)
    entrypoints = dict(schema.entrypoints)
    graphs["coverage-repair-closure"] = GraphDef(
        max_supersteps=20,
        nodes={
            "coverage-repair": NodeDef(uses="graph:coverage-repair"),
            "materialize-pr-metrics": NodeDef(
                uses="operation:materialize-pr-metrics",
                outputs=["change:inspect/metrics.json"],
                retry="never",
                timeout="local-operation",
            ),
            "metrics-sufficiency": NodeDef.model_validate(
                {
                    "uses": "builtin:gate",
                    "with": {"gate": "metrics-sufficiency-gate"},
                }
            ),
            # Packaged reject routing keeps flowing toward report/archive;
            # a stub sink proves reject does not STOP the parent run.
            "report": NodeDef(uses="operation:materialize-pr-metrics", retry="never", timeout="local-operation"),
        },
        edges=[
            EdgeDef.model_validate({"from": "START", "to": "coverage-repair"}),
            EdgeDef.model_validate({"from": "coverage-repair", "to": "materialize-pr-metrics"}),
            EdgeDef.model_validate({"from": "materialize-pr-metrics", "to": "metrics-sufficiency"}),
            EdgeDef.model_validate({"from": "report", "to": "END"}),
        ],
        routes=[
            RouteDef.model_validate(
                {
                    "from": "metrics-sufficiency",
                    "select": "node('metrics-sufficiency').gate.verdict",
                    "cases": {
                        "pass": "END",
                        "skip": "END",
                        "needs_human_review": "END",
                        "reject": "report",
                        "stop": "STOP",
                    },
                    "default": "STOP",
                }
            )
        ],
    )
    entry = "coverage-repair-closure-ep"
    entrypoints[entry] = EntrypointDef(graph="coverage-repair-closure", restart="repeatable")
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


def _stub_post_repair_ops(
    *,
    constraint_covered: int,
    uncovered: tuple[str, ...] | None = None,
    batch_id: str = BATCH_AFTER_RERUN,
) -> dict[str, Any]:
    def run_tests(task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext) -> TaskResult:
        del task, context
        # Only write under change:execution/** (run-tests authorization).
        _seed_batch(workspace.change_dir, batch_id, constraint_covered=constraint_covered)
        if uncovered is not None:
            _rewrite_constraint_coverage(
                workspace.change_dir,
                batch_id,
                covered=constraint_covered,
                uncovered=uncovered,
            )
        _write_manifest(workspace.change_dir, batch_id)
        return TaskResult(status="succeeded")

    def collect(task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext) -> TaskResult:
        del task, workspace, context
        return TaskResult(status="succeeded")

    return {
        "operation:run-tests": run_tests,
        "operation:collect-pr-metrics-batch": collect,
    }


def _tracking_probe(briefs: list[CoverageRepairBrief]) -> Callable[..., TaskResult]:
    real = default_operations()["operation:probe-coverage-repair-need"]

    def probe(task: ExecutableTask, workspace: TaskWorkspace, context: RuntimeContext) -> TaskResult:
        result = real(task, workspace, context)
        brief_path = workspace.change_dir / COVERAGE_REPAIR_BRIEF_REL
        if brief_path.is_file():
            briefs.append(
                CoverageRepairBrief.model_validate_json(brief_path.read_text(encoding="utf-8"))
            )
        return result

    return probe


def _build_runtime(
    project: Path,
    compiled: CompiledWorkflow,
    contracts: Any,
    invoker: Any,
    *,
    extra_ops: dict[str, Any] | None = None,
) -> GraphRuntime:
    change_dir = project / "qa" / "changes" / CHANGE_ID
    store = TreeStore(change_dir)
    checkpoints = CheckpointStore(change_dir)
    workspaces = WorkspaceBackend(change_dir)
    clock = FakeClock()
    holder: dict[str, GraphRuntime] = {}

    def run_child(task, graph_id, workspace, context):  # type: ignore[no-untyped-def]
        return holder["rt"].run_child(task, graph_id, workspace, context)

    node_runner = build_default_node_runner(
        invoker,
        store,
        contracts,
        compiled=compiled,
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

    state_defs: dict = {}
    for graph in compiled.schema.graphs.values():
        state_defs.update(dict(graph.state))
    scheduler = Scheduler(
        checkpoints=checkpoints,
        object_store=store,
        clock=clock,
        workspace_backend=workspaces,
        node_runner=node_runner,
        max_parallel_tasks=compiled.schema.policies.scheduler.max_parallel_tasks,
        contracts=contracts,
        state_defs=state_defs,
    )
    schemas = {compiled.digest: compiled}
    runtime = GraphRuntime(
        checkpoint_store=checkpoints,
        object_store=store,
        workspace_backend=workspaces,
        contracts=contracts,
        node_runner=node_runner,
        scheduler=scheduler,
        schema_resolver=lambda digest: schemas[digest],
        clock=clock,
    )
    holder["rt"] = runtime
    return runtime


def _context(project: Path, *, max_attempts: int = 1) -> RuntimeContext:
    return RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=project / "qa" / "changes" / CHANGE_ID,
        change_id=CHANGE_ID,
        params={"max_coverage_repair_attempts": max_attempts},
    )


def _gate_ctx(project_root: Path, change_dir: Path) -> GateEvaluationContext:
    return GateEvaluationContext(
        project_root=project_root,
        repo_root=project_root,
        change_dir=change_dir,
        change_id=CHANGE_ID,
        params={
            "auto_archive": True,
            "test_types": ["api"],
            "max_healing_attempts": 3,
        },
        state_values={},
        node_results={},
    )


def _seed_archive_companions(change_dir: Path, batch_id: str) -> None:
    """Minimal companions so archive-gate can judge collection_gaps alone."""
    _write_json(
        change_dir / "inspect" / "failure-analysis.json",
        {
            "schema_version": "1.0",
            "change_id": CHANGE_ID,
            "source_manifest": "execution/execution-manifest.yaml",
            "inspection_status": "completed",
            "batch_id": batch_id,
            "source_batch_id": batch_id,
            "final_status": "PASS",
            "inspect_mode": "primary",
            "compat_fallback_reason": None,
            "classification_performed": False,
            "status": "no_failures",
            "failures": [],
            "hard_fails": [],
            "needs_review": [],
            "known_product_issues": [],
        },
    )
    _write_json(
        change_dir / "healing" / "status.json",
        {"status": "not_needed", "attempts_used": 0, "all_fixers_no_op": False},
    )
    _write_json(change_dir / "review" / "case-review.json", {"decision": "pass"})
    _write_json(change_dir / "review" / "api-plan-review.json", {"decision": "pass"})
    _write_json(change_dir / "review" / "plan-review.json", {"decision": "pass"})
    _write_json(
        change_dir / "inspect" / "trace-sufficiency.json",
        {
            "schema_version": "1",
            "change_id": CHANGE_ID,
            "authoritative_batch_id": batch_id,
            "policy_digest": "0" * 64,
            "as_of": "2026-08-05T02:00:00+00:00",
            "integrity": "complete",
            "integrity_blocks_routing": False,
            "sufficient": True,
            "has_open_problems": False,
            "error_code": None,
            "insufficient_cases": [],
            "gap_codes": [],
        },
    )


def _closure_script(request: AgentRequest, attempt_n: int) -> AgentResult:
    del attempt_n
    root = Path(request.workspace_root)
    change = _change_root(root, request.change_id)
    baseline = _read_baseline(change)
    target = root / PROPERTY_TEST
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"def test_{CONSTRAINT_KEY}_holds():\n"
        f"    # property: {CONSTRAINT_KEY}\n"
        f"    assert True\n",
        encoding="utf-8",
    )
    _write_apply_summary(
        change,
        baseline=baseline,
        applied=True,
        files_modified=(PROPERTY_TEST,),
    )
    return AgentResult(ok=True)


def _declared_noop_script(request: AgentRequest, attempt_n: int) -> AgentResult:
    del attempt_n
    change = _change_root(Path(request.workspace_root), request.change_id)
    baseline = _read_baseline(change)
    _write_apply_summary(change, baseline=baseline, applied=False, files_modified=())
    return AgentResult(ok=True)


def _run_closure(
    project: Path,
    change_dir: Path,
    *,
    script: Callable[[AgentRequest, int], AgentResult],
    post_repair_covered: int,
    post_repair_uncovered: tuple[str, ...] | None = None,
    briefs: list[CoverageRepairBrief] | None = None,
) -> tuple[Any, list[str], list[CoverageRepairBrief]]:
    compiled, contracts, entry = _compile_closure_harness()
    invoker = ScriptedRepairInvoker(script)
    captured = briefs if briefs is not None else []
    extra = _stub_post_repair_ops(
        constraint_covered=post_repair_covered,
        uncovered=post_repair_uncovered,
    )
    extra["operation:probe-coverage-repair-need"] = _tracking_probe(captured)
    runtime = _build_runtime(project, compiled, contracts, invoker, extra_ops=extra)
    result = runtime.run(compiled, entry, _context(project, max_attempts=1))
    return result, _activated_nodes(change_dir), captured


# ---------------------------------------------------------------------------
# Step 1: closure
# ---------------------------------------------------------------------------


def test_closure_repairs_constraint_gap_and_metrics_pass(tmp_path: Path) -> None:
    project, change_dir = _seed_shortfall_project(
        tmp_path, gaps=[_constraint_gap(layer="execution")]
    )

    result, activated, briefs = _run_closure(
        project,
        change_dir,
        script=_closure_script,
        post_repair_covered=4,
    )

    assert result.status.status == "completed", result.reason
    assert briefs, "expected at least the entry probe brief"
    entry_brief = briefs[0]
    assert entry_brief.eligible is True
    assert len(entry_brief.repair_items) == 1
    assert entry_brief.repair_items[0].kind == "constraint_without_property"
    assert entry_brief.repair_items[0].locator.constraint_key == CONSTRAINT_KEY

    safety = CoverageRepairSafetyCheck.model_validate_json(
        (change_dir / COVERAGE_REPAIR_SAFETY_REL).read_text(encoding="utf-8")
    )
    assert safety.passed is True
    assert safety.needs_review is False

    metrics_path = change_dir / INSPECT_METRICS_REL
    assert metrics_path.is_file()
    source = json.loads((change_dir / METRICS_SOURCE_BATCH_REL).read_text(encoding="utf-8"))
    assert source["batch_id"] == BATCH_AFTER_RERUN
    doc = MetricsDocument.model_validate_json(metrics_path.read_text(encoding="utf-8"))
    declared = doc.metrics["constraint_coverage"].declared
    assert declared is not None
    assert CONSTRAINT_KEY not in declared.uncovered
    assert declared.covered == 4

    schema = load_workflow_v2(Path.cwd())
    metrics_gate = check_gate_in_view(
        schema.gates, "metrics-sufficiency-gate", _gate_ctx(project, change_dir)
    )
    assert metrics_gate.verdict.value == "pass"

    status = CoverageRepairStatus.model_validate_json(
        (change_dir / COVERAGE_REPAIR_STATUS_REL).read_text(encoding="utf-8")
    )
    assert status.status == "repaired"
    assert status.attempts_used == 1
    assert "complete-repaired" in activated
    assert "materialize-pr-metrics" in activated
    assert "metrics-sufficiency" in activated


# ---------------------------------------------------------------------------
# Step 2: fail-closed backstop
# ---------------------------------------------------------------------------


def test_fail_closed_noop_exhausts_and_metrics_need_human(tmp_path: Path) -> None:
    project, change_dir = _seed_shortfall_project(
        tmp_path, gaps=[_constraint_gap(layer="execution")]
    )

    result, activated, _briefs = _run_closure(
        project,
        change_dir,
        script=_declared_noop_script,
        post_repair_covered=1,
        post_repair_uncovered=(CONSTRAINT_KEY, "k2", "k3"),
    )

    assert result.status.status == "completed", result.reason
    assert "complete-exhausted" in activated
    status = CoverageRepairStatus.model_validate_json(
        (change_dir / COVERAGE_REPAIR_STATUS_REL).read_text(encoding="utf-8")
    )
    assert status.status == "exhausted"
    assert status.attempts_used == 1

    doc = MetricsDocument.model_validate_json(
        (change_dir / INSPECT_METRICS_REL).read_text(encoding="utf-8")
    )
    declared = doc.metrics["constraint_coverage"].declared
    assert declared is not None
    assert CONSTRAINT_KEY in declared.uncovered
    assert (declared.value or 0.0) < 0.5

    schema = load_workflow_v2(Path.cwd())
    metrics_gate = check_gate_in_view(
        schema.gates, "metrics-sufficiency-gate", _gate_ctx(project, change_dir)
    )
    assert metrics_gate.verdict.value == "needs_human_review"


# ---------------------------------------------------------------------------
# Step 3: nothing-to-repair (design v3-1 at integration scope)
# ---------------------------------------------------------------------------


def test_nothing_to_repair_skips_allocate_and_metrics_pass(tmp_path: Path) -> None:
    project, change_dir = _seed_passing_project(tmp_path)

    result, activated, briefs = _run_closure(
        project,
        change_dir,
        script=_closure_script,
        post_repair_covered=4,
    )

    assert result.status.status == "completed", result.reason
    assert briefs and briefs[0].eligible is False
    assert "complete-not-eligible" in activated
    assert "allocate" not in activated
    assert "repair" not in activated
    assert "materialize-pr-metrics" in activated
    assert "metrics-sufficiency" in activated

    status = CoverageRepairStatus.model_validate_json(
        (change_dir / COVERAGE_REPAIR_STATUS_REL).read_text(encoding="utf-8")
    )
    assert status.status == "not_eligible"
    assert status.attempts_used == 0

    schema = load_workflow_v2(Path.cwd())
    metrics_gate = check_gate_in_view(
        schema.gates, "metrics-sufficiency-gate", _gate_ctx(project, change_dir)
    )
    assert metrics_gate.verdict.value == "pass"


# ---------------------------------------------------------------------------
# Step 4: collection-gap bypass
# ---------------------------------------------------------------------------


def test_collection_gap_bypasses_repair_and_blocks_archive(tmp_path: Path) -> None:
    project, change_dir = _seed_collection_gap_project(tmp_path)

    result, activated, briefs = _run_closure(
        project,
        change_dir,
        script=_closure_script,
        post_repair_covered=4,
    )

    assert result.status.status == "completed", result.reason
    assert briefs and briefs[0].probe_verdict == "reject"
    assert briefs[0].eligible is False
    assert "allocate" not in activated
    assert "repair" not in activated
    assert "complete-not-eligible" in activated
    assert "materialize-pr-metrics" in activated
    assert "metrics-sufficiency" in activated
    # Packaged reject → report (not STOP); thin parent mirrors that edge.
    assert "report" in activated

    schema = load_workflow_v2(Path.cwd())
    metrics_gate = check_gate_in_view(
        schema.gates, "metrics-sufficiency-gate", _gate_ctx(project, change_dir)
    )
    assert metrics_gate.verdict.value == "reject"

    _seed_archive_companions(change_dir, BATCH_NEW)
    archive = check_gate_in_view(schema.gates, "archive-gate", _gate_ctx(project, change_dir))
    assert archive.verdict.value == "stop"
    assert archive.details is not None
    assert archive.details["cause"] == "archive.metrics_collection_gap"


# ---------------------------------------------------------------------------
# Step 5: declaration-layer refusal
# ---------------------------------------------------------------------------


def test_declaration_layer_gap_is_deferred_never_repaired(tmp_path: Path) -> None:
    """Declaration-only gaps: deferred in brief, allocate skipped, status freezes them."""
    project, change_dir = _seed_shortfall_project(
        tmp_path,
        gaps=[_declaration_gap()],
    )

    result, activated, briefs = _run_closure(
        project,
        change_dir,
        script=_closure_script,
        post_repair_covered=1,
        post_repair_uncovered=(CONSTRAINT_KEY, "k2", "k3"),
    )

    assert result.status.status == "completed", result.reason
    assert briefs
    entry_brief = briefs[0]
    assert entry_brief.eligible is False
    assert entry_brief.repair_items == ()
    assert any(
        item.reason == "declaration_layer" and item.locator.case_id == "TC_DECL_001"
        for item in entry_brief.deferred_to_intake
    )

    assert "allocate" not in activated
    assert "repair" not in activated
    assert "complete-not-eligible" in activated

    status = CoverageRepairStatus.model_validate_json(
        (change_dir / COVERAGE_REPAIR_STATUS_REL).read_text(encoding="utf-8")
    )
    assert status.status == "not_eligible"
    assert status.attempts_used == 0
    assert any(
        item.reason == "declaration_layer" and item.locator.case_id == "TC_DECL_001"
        for item in status.deferred_to_intake
    )
    assert status.status != "repaired"
