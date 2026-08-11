"""Task 13/14: recovery terminals, healing, and B0→B1 current-authority projections."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import pytest
import yaml

from assurance_agent.artifacts.models.issue_events import ChangeIssueEvent
from assurance_agent.evidence.current_projection import load_current_reconciled_projection
from assurance_agent.evidence.layer_summary import TraceLayerSummaryError, summarize_projection_by_layer
from assurance_agent.evidence.trace import fold_trace
from assurance_agent.workflow.core.events import read_events_strict
from assurance_agent.workflow.driver.runtime_factory import (
    ResolvedExecutionBundle,
    one_definition_resolver,
    validate_ingest_model_map,
)
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult, ErrorKind
from assurance_agent.workflow.graph.checkpoint import CheckpointStore, project_invocation
from assurance_agent.workflow.graph.compiler import compile_packaged_workflow, compile_workflow
from assurance_agent.workflow.graph.contracts import ExecutionContractCatalog, load_execution_contracts
from assurance_agent.workflow.graph.definition_pinning import (
    load_pinned_execution_definition,
    request_for_compiled,
)
from assurance_agent.workflow.graph.handlers.agent import AgentHandler
from assurance_agent.workflow.graph.handlers.gate import GateHandler
from assurance_agent.workflow.graph.handlers.interrupt import InterruptHandler
from assurance_agent.workflow.graph.handlers.join import JoinHandler
from assurance_agent.workflow.driver.operations_catalog import default_operations
from assurance_agent.workflow.graph.handlers.operation import (
    OperationFn,
    OperationHandler,
)
from assurance_agent.workflow.graph.handlers.subgraph import SubgraphHandler
from assurance_agent.workflow.graph.ingest_catalog import (
    IngestArtifactCatalog,
    validate_catalog_runtime,
)
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.runtime import (
    GraphDefinitionChanged,
    GraphRuntime,
    assert_live_semantic_compatibility,
)
from assurance_agent.workflow.graph.scheduler import Scheduler
from assurance_agent.workflow.graph.schema_v2 import EdgeDef, EntrypointDef, load_workflow_v2
from assurance_agent.workflow.graph.task_runner import HandlerNodeRunner
from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceBackend
from assurance_agent.workflow.improvements.ledger import atomic_write_json
from tests.helpers_aa import write_aa_config
from tests.unit.evidence import test_issue_replay_authority as auth


def _write_api_case(change_dir: Path, case_id: str = "TC_DEPT_API_001") -> None:
    path = change_dir / "cases" / "dept" / "case.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""
schema_version: "1.0"
added:
  - case_id: {case_id}
    module: system.dept
    type: API
    title: create
    status: active
    priority: P0
    severity: blocker
    automation:
      required: true
modified: []
removed: []
""",
        encoding="utf-8",
    )


def _write_selected_api_result(change_dir: Path, batch_id: str) -> None:
    path = change_dir / "execution" / "runs" / batch_id / "api-result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "change_id": auth.CHANGE_ID,
                "batch_id": batch_id,
                "target": "api",
                "cases": [],
                "unmapped_tests": [],
            }
        ),
        encoding="utf-8",
    )


def materialize_reconciled_v2(project_root: Path) -> Path:
    change_dir = project_root / "qa" / "changes" / auth.CHANGE_ID
    _write_api_case(change_dir)
    projection = fold_trace(project_root, auth.CHANGE_ID, phase="reconciled")
    path = change_dir / "inspect" / "trace-projection.json"
    atomic_write_json(path, projection.model_dump(mode="json"))
    return path


class _InjectedCrash(BaseException):
    """Test-only seam crash before ordinary write-set commit.

    Subclasses ``BaseException`` so nested ``HandlerNodeRunner`` (which catches
    ``Exception``) cannot convert the seam into a failed subgraph task and tear
    down the child workspace before resume.
    """


T0 = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)
CHANGE_ID = auth.CHANGE_ID
B0_BATCH = auth.BATCH_ID
B1_BATCH = "B1"
# Post-healing metrics/trace gates are feature-branch additions main recovery
# fixtures never exercised (main was inspect→healing→report). Stub the whole
# post-healing chain so these terminals still end at report with exit 0.
STUB_SUBGRAPHS = frozenset(
    {
        "api-branch",
        "e2e-branch",
        "fuzz-branch",
        "performance-branch",
        "healing",
        "coverage-repair",
    }
)
_STUB_PASS_GATES = frozenset({"metrics-sufficiency", "trace-sufficiency"})

Outcome = Literal["success", "analyzer_recovery", "sync_recovery", "reconcile_failed"]

TRACE_TERMINAL_CASES = (
    ("assurance", "inspect-with-issues", "analyzer_recovery"),
    ("assurance", "inspect-with-issues", "sync_recovery"),
    ("issue-analyze-workflow", None, "success"),
    ("issue-analyze-workflow", None, "analyzer_recovery"),
    ("issue-analyze-workflow", None, "sync_recovery"),
    ("issue-reconcile-workflow", None, "success"),
    ("issue-reconcile-workflow", None, "sync_recovery"),
)


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


class ScriptedAnalyzer:
    """Deterministic analyzer: fail with ``error_kind`` until attempt N, then succeed."""

    succeed_on_attempt: int | None
    error_kind: ErrorKind
    batch_id: str
    calls: int

    def __init__(
        self,
        *,
        succeed_on_attempt: int | None,
        error_kind: ErrorKind = "timeout",
        batch_id: str = B1_BATCH,
    ) -> None:
        self.succeed_on_attempt = succeed_on_attempt
        self.error_kind = error_kind
        self.batch_id = batch_id
        self.calls = 0

    def invoke(self, request: AgentRequest) -> AgentResult:
        self.calls += 1
        if self.succeed_on_attempt is None or self.calls < self.succeed_on_attempt:
            return AgentResult(ok=False, error_kind=self.error_kind, error=f"scripted {self.error_kind}")
        workspace = Path(request.workspace_root)
        change_root = workspace / "qa" / "changes" / request.change_id
        inspect_dir = change_root / "inspect"
        inspect_dir.mkdir(parents=True, exist_ok=True)
        manifest = json.loads((inspect_dir / "issue-evidence-manifest.json").read_text(encoding="utf-8"))
        evidence_digest = manifest["digest"]
        candidates = {
            "schema_version": "1.0",
            "change_id": request.change_id,
            "batch_id": self.batch_id,
            "evidence_bundle_digest": evidence_digest,
            "candidates": [],
        }
        (inspect_dir / "issue-candidates.json").write_text(
            json.dumps(candidates, sort_keys=True) + "\n", encoding="utf-8"
        )
        from assurance_agent.evidence.issue_identity import candidate_document_digest

        cand_digest = candidate_document_digest(candidates)
        (inspect_dir / "issue-analysis-status.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "change_id": request.change_id,
                    "batch_id": self.batch_id,
                    "status": "completed",
                    "evidence_bundle_digest": evidence_digest,
                    "candidate_count": 0,
                    "candidate_digest": cand_digest,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return AgentResult(ok=True)


class SkillRouter:
    def __init__(self, analyzer: ScriptedAnalyzer) -> None:
        self.analyzer = analyzer

    def invoke(self, request: AgentRequest) -> AgentResult:
        if "issue-analyzer" in request.target:
            return self.analyzer.invoke(request)
        return AgentResult(ok=True)


def _compile_packaged(
    *,
    assurance_entrypoint: bool = False,
    inspect_retry: str | None = None,
) -> tuple[CompiledWorkflow, Any]:
    schema = load_workflow_v2(Path.cwd())
    if assurance_entrypoint:
        graphs = dict(schema.graphs)
        if inspect_retry is not None:
            assurance = graphs["assurance"]
            nodes = dict(assurance.nodes)
            inspect_node = nodes["inspect-with-issues"]
            nodes["inspect-with-issues"] = inspect_node.model_copy(update={"retry": inspect_retry})
            graphs["assurance"] = assurance.model_copy(update={"nodes": nodes})
        schema = schema.model_copy(
            update={
                "entrypoints": {
                    **dict(schema.entrypoints),
                    "assurance-direct": EntrypointDef(graph="assurance"),
                },
                "graphs": graphs if inspect_retry is not None else schema.graphs,
            }
        )
    contracts = load_execution_contracts(Path.cwd())
    return compile_workflow(schema, contracts), contracts


def _context(project: Path) -> RuntimeContext:
    change = project / "qa" / "changes" / CHANGE_ID
    return RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=change,
        change_id=CHANGE_ID,
        params={
            "run_mode": "codegen-only",
            "test_types": ["api"],
            "run_tests": True,
            "max_healing_attempts": 0,
        },
    )


def _seed_b0_completed(project: Path) -> Path:
    write_aa_config(project)
    auth._write_completed_with_occurrence(project)
    change = project / "qa" / "changes" / CHANGE_ID
    _write_api_case(change)
    _write_selected_api_result(change, B0_BATCH)
    materialize_reconciled_v2(project)
    projection = json.loads((change / "inspect" / "trace-projection.json").read_text(encoding="utf-8"))
    assert projection["authoritative_batch_id"] == B0_BATCH
    assert projection["schema_version"] == "2"
    return change


def _advance_inputs_to_b1(change: Path, *, seed_completed_analysis: bool) -> None:
    """Replace B0 issue/execution authority with B1 seeds the graph will consume."""
    for path in (
        change / auth.MANIFEST_SOURCE,
        change / auth.CANDIDATES_SOURCE,
        change / auth.OBSERVATIONS_SOURCE,
        change / auth.LEDGER_SOURCE,
        change / auth.SNAPSHOT_SOURCE,
        change / auth.RECONCILE_SOURCE,
        change / auth.FAILURE_SOURCE,
        change / auth.EXECUTION_ANCHOR,
    ):
        path.unlink(missing_ok=True)

    failure = auth._failure_payload()
    failure["batch_id"] = B1_BATCH
    failure["source_batch_id"] = B1_BATCH
    auth._write_json(change / auth.FAILURE_SOURCE, failure)

    original = auth.BATCH_ID
    auth.BATCH_ID = B1_BATCH
    try:
        digest, candidates, observation = auth._seed_manifest_tree(change)
        if seed_completed_analysis:
            from assurance_agent.evidence.issue_identity import candidate_document_digest

            empty = candidates.model_copy(update={"candidates": []})
            auth._write_json(change / auth.CANDIDATES_SOURCE, empty.model_dump(mode="json"))
            c_digest = candidate_document_digest(empty.model_dump(mode="json"))
            events: list[ChangeIssueEvent] = [
                auth._obs_recorded(observation, seq=1),
                auth._analysis_completed_event(
                    evidence_digest=digest,
                    candidate_digest=c_digest,
                    candidate_count=0,
                    seq=2,
                ),
            ]
            auth._write_ledger(change, events)
            auth._write_snapshot_from_events(change, events)
            auth._write_json(
                change / "inspect" / "issue-analysis-status.json",
                {
                    "schema_version": "1.0",
                    "change_id": CHANGE_ID,
                    "batch_id": B1_BATCH,
                    "status": "completed",
                    "evidence_bundle_digest": digest,
                    "candidate_count": 0,
                    "candidate_digest": c_digest,
                },
            )
        else:
            events: list[ChangeIssueEvent] = [auth._obs_recorded(observation, seq=1)]
            auth._write_ledger(change, events)
            auth._write_snapshot_from_events(change, events)
    finally:
        auth.BATCH_ID = original
    _write_selected_api_result(change, B1_BATCH)
    # Drop B0 project-authority files so task-workspace folds and live folds agree.
    # The preseeded B0 projection still carries B0 problem links for replacement asserts.
    project_root = change.parents[2]
    (project_root / auth.PROJECT_LEDGER_SOURCE).unlink(missing_ok=True)
    (project_root / auth.PROJECT_PROBLEMS_SOURCE).unlink(missing_ok=True)


def _assurance_stub_ops() -> dict[str, OperationFn]:
    def stub_run_tests(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
        del task, context
        path = workspace.change_dir / "execution" / "execution-manifest.yaml"
        assert path.is_file(), "B1 execution anchor must be pre-seeded"
        man = yaml.safe_load(path.read_text(encoding="utf-8"))
        return TaskResult(
            status="succeeded",
            value={"batch_id": man["batch_id"], "final_status": man.get("final_status", "PASS")},
        )

    def stub_inspect(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
        del task, context
        insp = workspace.change_dir / "inspect"
        insp.mkdir(parents=True, exist_ok=True)
        if not (insp / "failure-analysis.json").is_file():
            failure = auth._failure_payload()
            failure["batch_id"] = B1_BATCH
            failure["source_batch_id"] = B1_BATCH
            auth._write_json(insp / "failure-analysis.json", failure)
        (insp / "quality-gate-result.json").write_text(
            json.dumps({"final_status": "PASS"}) + "\n", encoding="utf-8"
        )
        return TaskResult(status="succeeded", value={"final_status": "PASS"})

    def stub_collect(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
        del task, workspace, context
        return TaskResult(status="succeeded", value={"abnormal_count": 1, "batch_id": B1_BATCH})

    def stub_report(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
        del task, context
        report = workspace.change_dir / "report"
        report.mkdir(parents=True, exist_ok=True)
        for name in ("quality-report.json", "quality-report.md", "executive-summary.md"):
            (report / name).write_text("ok\n", encoding="utf-8")
        return TaskResult(status="succeeded")

    def stub_materialize_pr_metrics(
        task: ExecutableTask, workspace: Any, context: RuntimeContext
    ) -> TaskResult:
        del task, context
        from assurance_agent.artifacts.models.metrics import MetricsDocument

        inspect = workspace.change_dir / "inspect"
        inspect.mkdir(parents=True, exist_ok=True)
        doc = MetricsDocument.model_validate(
            {
                "schema_version": "2",
                "change_id": CHANGE_ID,
                "cadence": "pr",
                "computed_at": "2026-07-31T12:00:00Z",
                "risk_tier": "low",
                "risk_tier_lower_bound": "low",
                "risk_tier_declared": None,
                "risk_declaration_lowered": False,
                "risk_lowered_declarations": [],
                "metrics": {
                    "constraint_coverage": {
                        "layer": "api",
                        "status": "not_evaluated",
                        "value": None,
                        "declared": None,
                        "evidence": "",
                    }
                },
                "collection_gaps": [],
                "shortboards": [],
                "floor_ratio": None,
                "policy_digest": "a" * 64,
            }
        )
        (inspect / "metrics.json").write_text(doc.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return TaskResult(status="succeeded", value={"stubbed": "materialize-pr-metrics"})

    return {
        "operation:run-tests": stub_run_tests,
        "operation:inspect": stub_inspect,
        "operation:collect-observations": stub_collect,
        "operation:generate-report": stub_report,
        "operation:materialize-pr-metrics": stub_materialize_pr_metrics,
    }


def _counting_reconcile(
    *, fail_attempts: int, error_kind: ErrorKind = "conflict"
) -> tuple[OperationFn, dict[str, int]]:
    calls = {"n": 0}

    def _fn(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
        del task
        calls["n"] += 1
        if calls["n"] <= fail_attempts:
            return TaskResult(status="failed", error_kind=error_kind, error=f"scripted {error_kind}")
        # Successful reconcile: completed empty authority for B1.
        inspect_dir = workspace.change_dir / "inspect"
        candidates = json.loads((inspect_dir / "issue-candidates.json").read_text(encoding="utf-8"))
        from assurance_agent.evidence.issue_identity import candidate_document_digest

        c_digest = candidate_document_digest(candidates)
        auth._write_reconcile_status(
            workspace.change_dir,
            schema_version="2.0",
            status="completed",
            evidence_bundle_digest=candidates["evidence_bundle_digest"],
            candidate_digest=c_digest,
            occurrence_count=0,
        )
        # Keep ledger/snapshot consistent with completed empty analysis.
        return TaskResult(status="succeeded", value={"occurrence_count": 0})

    return _fn, calls


def _semantic_reconcile_failed() -> OperationFn:
    def _fn(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
        del task, context
        # Mirror write_recovery_state("reconcile_failed"): failed V2 status masks snapshot.
        (workspace.change_dir / auth.SNAPSHOT_SOURCE).unlink(missing_ok=True)
        candidates = json.loads(
            (workspace.change_dir / "inspect" / "issue-candidates.json").read_text(encoding="utf-8")
        )
        from assurance_agent.evidence.issue_identity import candidate_document_digest

        c_digest = candidate_document_digest(candidates)
        auth._write_json(
            workspace.change_dir / auth.RECONCILE_SOURCE,
            {
                "schema_version": "2.0",
                "change_id": CHANGE_ID,
                "batch_id": candidates["batch_id"],
                "status": "failed",
                "evidence_bundle_digest": candidates["evidence_bundle_digest"],
                "candidate_digest": c_digest,
                "error": "semantic",
            },
        )
        # Task succeeds (semantic failure is recorded, not a retryable transport error).
        return TaskResult(status="succeeded", value={"status": "failed"})

    return _fn


def _gate_handler(compiled: CompiledWorkflow, *, stub_post_healing: bool) -> Any:
    inner = GateHandler(compiled)
    if not stub_post_healing:
        return inner

    class _PassPostHealingGates:
        def execute(self, task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
            if task.node_id in _STUB_PASS_GATES:
                return TaskResult(
                    status="succeeded",
                    value="pass",
                    gate_report={
                        "gate_id": task.node_id,
                        "verdict": "pass",
                        "matched_rule": "stub",
                        "reason": "recovery fixture stubs post-healing gates",
                        "reads_sha256": {},
                        "value": "pass",
                    },
                )
            return inner.execute(task, workspace, context)

    return _PassPostHealingGates()


def _build_runtime(
    project: Path,
    compiled: CompiledWorkflow,
    contracts: Any,
    *,
    analyzer: ScriptedAnalyzer,
    ops: dict[str, OperationFn],
    stub_assurance_subgraphs: bool = False,
) -> GraphRuntime:
    change = project / "qa" / "changes" / CHANGE_ID
    store = TreeStore(change)
    checkpoints = CheckpointStore(change)
    workspaces = WorkspaceBackend(change)
    clock = FakeClock()
    holder: dict[str, GraphRuntime] = {}

    def run_child(task: Any, graph_id: str, workspace: Any, context: Any) -> TaskResult:
        if stub_assurance_subgraphs and graph_id in STUB_SUBGRAPHS:
            return TaskResult(status="succeeded", value={"stubbed": graph_id})
        return holder["rt"].run_child(task, graph_id, workspace, context)

    merged = default_operations()
    merged.update(ops)
    op_handler = OperationHandler(merged)
    agent = AgentHandler(SkillRouter(analyzer), store, contracts=contracts, compiled=compiled)
    handlers: dict[str, Any] = {
        "builtin:join": JoinHandler(),
        "builtin:gate": _gate_handler(compiled, stub_post_healing=stub_assurance_subgraphs),
        "builtin:interrupt": InterruptHandler(compiled),
        **{target: op_handler for target in merged},
    }
    node_runner = HandlerNodeRunner(
        handlers,
        namespace_handlers={
            "skill": agent,
            "graph": SubgraphHandler(run_child),
        },
        compiled=compiled,
        object_store=store,
    )
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
    runtime = GraphRuntime(
        checkpoint_store=checkpoints,
        object_store=store,
        workspace_backend=workspaces,
        definition_resolver=one_definition_resolver(
            compiled=compiled,
            contracts=contracts,
            ingest_catalog=validate_catalog_runtime(),
            node_runner=node_runner,
            scheduler=scheduler,
        ),
        clock=clock,
    )
    holder["rt"] = runtime
    return runtime


def _owner_invocation_id(change: Path, *, child_graph: str | None) -> str:
    events = read_events_strict(change)
    if child_graph is None:
        return next(
            str(e["invocation_id"])
            for e in events
            if e.get("type") == "graph_invocation_started" and e.get("parent_invocation_id") is None
        )
    return next(
        str(e["invocation_id"])
        for e in events
        if e.get("type") == "graph_invocation_started" and e.get("graph_id") == child_graph
    )


def _materializer_success_count(change: Path, invocation_id: str) -> int:
    projection = project_invocation(change, invocation_id)
    return sum(
        1
        for task in projection.tasks.values()
        if task.node_id == "materialize-trace-projection" and task.status == "succeeded"
    )


def _assert_no_b0_problem_links(projection: Any) -> None:
    assert all(not row.open_problem_ids for row in projection.rows)


def _run_terminal_path(
    tmp_path: Path,
    *,
    parent: str,
    child: str | None,
    outcome: Outcome,
    analyzer_error_kind: ErrorKind = "timeout",
    reconcile_error_kind: ErrorKind = "conflict",
) -> tuple[Path, Path, str, Any]:
    project = tmp_path / "proj"
    change = _seed_b0_completed(project)
    # issue-reconcile starts at reconcile; semantic-failure needs a completed analysis prefix.
    seed_completed = parent == "issue-reconcile-workflow" or outcome == "reconcile_failed"
    _advance_inputs_to_b1(change, seed_completed_analysis=seed_completed)

    assurance = parent == "assurance"
    compiled, contracts = _compile_packaged(assurance_entrypoint=assurance)

    ops: dict[str, OperationFn] = {}
    if outcome == "analyzer_recovery":
        analyzer = ScriptedAnalyzer(succeed_on_attempt=None, error_kind=analyzer_error_kind)
    elif outcome == "sync_recovery":
        analyzer = ScriptedAnalyzer(succeed_on_attempt=1)
        reconcile, _ = _counting_reconcile(fail_attempts=3, error_kind=reconcile_error_kind)
        ops["operation:reconcile-issues"] = reconcile
    elif outcome == "reconcile_failed":
        analyzer = ScriptedAnalyzer(succeed_on_attempt=1)
        ops["operation:reconcile-issues"] = _semantic_reconcile_failed()
    else:
        # Success uses the real reconcile-issues op so ledger analysis_completed is authored.
        analyzer = ScriptedAnalyzer(succeed_on_attempt=1)

    if assurance:
        ops.update(_assurance_stub_ops())

    runtime = _build_runtime(
        project,
        compiled,
        contracts,
        analyzer=analyzer,
        ops=ops,
        stub_assurance_subgraphs=assurance,
    )
    entry = (
        "assurance-direct"
        if assurance
        else ("issue-analyze" if parent == "issue-analyze-workflow" else "issue-reconcile")
    )
    result = runtime.run(compiled, entry, _context(project))
    assert result.exit_code == 0, result.reason
    assert result.status.status == "completed", result.reason

    owner_id = _owner_invocation_id(change, child_graph=child)
    assert _materializer_success_count(change, owner_id) == 1

    projection = load_current_reconciled_projection(project, CHANGE_ID)
    assert projection.authoritative_batch_id == B1_BATCH
    assert projection.schema_version == "2"
    _assert_no_b0_problem_links(projection)
    return project, change, owner_id, projection


# ---------------------------------------------------------------------------
# Task 13 — exhausted-retry paths still reach materializer + END
# ---------------------------------------------------------------------------


def _make_project_legacy(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    change = project / "qa" / "changes" / "CH-TRACE-13"
    change.mkdir(parents=True)
    (project / "qa" / "issues").mkdir(parents=True)
    write_aa_config(project)
    inspect_dir = change / "inspect"
    inspect_dir.mkdir(parents=True, exist_ok=True)
    issues_dir = change / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    (inspect_dir / "observations.json").write_text(
        json.dumps(
            {"schema_version": "1.0", "change_id": "CH-TRACE-13", "observations": [], "abnormal_count": 1}
        )
        + "\n",
        encoding="utf-8",
    )
    (inspect_dir / "issue-evidence-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "change_id": "CH-TRACE-13",
                "batch_id": "batch-1",
                "digest": "sha256:" + "a" * 64,
                "entries": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (issues_dir / "events.jsonl").write_text("", encoding="utf-8")
    (issues_dir / "snapshot.json").write_text(
        json.dumps({"schema_version": "1.0", "change_id": "CH-TRACE-13", "occurrences": []}) + "\n",
        encoding="utf-8",
    )
    return project


def _counting_materializer() -> tuple[OperationFn, dict[str, int]]:
    calls = {"n": 0}

    def _fn(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
        del task
        calls["n"] += 1
        inspect_dir = workspace.change_dir / "inspect"
        inspect_dir.mkdir(parents=True, exist_ok=True)
        change_id = context.change_id
        (inspect_dir / "trace-projection.json").write_text(
            json.dumps(
                {
                    "schema_version": "2",
                    "change_id": change_id,
                    "phase": "reconciled",
                    "authoritative_batch_id": "batch-1",
                    "sources": [],
                    "rows": [],
                    "unmapped_tests": [],
                    "gaps": [
                        {
                            "code": "issue_reconciliation_unavailable",
                            "source": "inspect/issue-evidence-manifest.json",
                            "detail": "reason=missing",
                        }
                    ],
                    "integrity": "incomplete",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (inspect_dir / "trace-sufficiency.json").write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "change_id": change_id,
                    "authoritative_batch_id": "batch-1",
                    "policy_digest": None,
                    "as_of": None,
                    "integrity": "incomplete",
                    "integrity_blocks_routing": True,
                    "sufficient": False,
                    "has_open_problems": False,
                    "error_code": "evidence_projection_missing",
                    "insufficient_cases": [],
                    "gap_codes": ["issue_reconciliation_unavailable"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return TaskResult(status="succeeded", value={"phase": "reconciled", "integrity": "incomplete"})

    return _fn, calls


def _counting_pending() -> tuple[OperationFn, dict[str, int]]:
    calls = {"n": 0}

    def _fn(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
        del task, context
        calls["n"] += 1
        inspect_dir = workspace.change_dir / "inspect"
        inspect_dir.mkdir(parents=True, exist_ok=True)
        issues_dir = workspace.change_dir / "issues"
        issues_dir.mkdir(parents=True, exist_ok=True)
        (inspect_dir / "issue-reconcile-status.json").write_text(
            json.dumps({"status": "project_sync_pending"}) + "\n", encoding="utf-8"
        )
        (issues_dir / "events.jsonl").write_text("", encoding="utf-8")
        (issues_dir / "snapshot.json").write_text(json.dumps({"sync_pending": True}) + "\n", encoding="utf-8")
        return TaskResult(status="succeeded", value={"sync_pending": True})

    return _fn, calls


def _build_runtime_legacy(
    project: Path,
    compiled: CompiledWorkflow,
    contracts: Any,
    *,
    analyzer: ScriptedAnalyzer,
    ops: dict[str, OperationFn],
) -> GraphRuntime:
    change = project / "qa" / "changes" / "CH-TRACE-13"
    store = TreeStore(change)
    checkpoints = CheckpointStore(change)
    workspaces = WorkspaceBackend(change)
    clock = FakeClock()
    holder: dict[str, GraphRuntime] = {}

    def run_child(task: Any, graph_id: str, workspace: Any, context: Any) -> TaskResult:
        return holder["rt"].run_child(task, graph_id, workspace, context)

    merged = default_operations()
    merged.update(ops)
    op_handler = OperationHandler(merged)
    agent = AgentHandler(analyzer, store, contracts=contracts, compiled=compiled)
    handlers: dict[str, Any] = {
        "builtin:join": JoinHandler(),
        "builtin:gate": GateHandler(compiled),
        "builtin:interrupt": InterruptHandler(compiled),
        **{target: op_handler for target in merged},
    }
    node_runner = HandlerNodeRunner(
        handlers,
        namespace_handlers={"skill": agent, "graph": SubgraphHandler(run_child)},
        compiled=compiled,
        object_store=store,
    )
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
    runtime = GraphRuntime(
        checkpoint_store=checkpoints,
        object_store=store,
        workspace_backend=workspaces,
        definition_resolver=one_definition_resolver(
            compiled=compiled,
            contracts=contracts,
            ingest_catalog=validate_catalog_runtime(),
            node_runner=node_runner,
            scheduler=scheduler,
        ),
        clock=clock,
    )
    holder["rt"] = runtime
    return runtime


def test_issue_analyze_exhausted_reconcile_reaches_materializer_and_end(tmp_path: Path) -> None:
    project = _make_project_legacy(tmp_path)
    compiled, contracts = _compile_packaged()
    assert compiled.schema.graphs["issue-analyze-workflow"].max_supersteps == 9
    analyzer = ScriptedAnalyzer(succeed_on_attempt=3, batch_id="batch-1")
    reconcile, reconcile_calls = _counting_reconcile(fail_attempts=3)
    pending, pending_calls = _counting_pending()
    materialize, materialize_calls = _counting_materializer()
    runtime = _build_runtime_legacy(
        project,
        compiled,
        contracts,
        analyzer=analyzer,
        ops={
            "operation:reconcile-issues": reconcile,
            "operation:record-project-sync-pending": pending,
            "operation:materialize-trace-projection": materialize,
        },
    )
    change = project / "qa" / "changes" / "CH-TRACE-13"
    ctx = RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=change,
        change_id="CH-TRACE-13",
        params={"run_mode": "full"},
    )
    result = runtime.run(compiled, "issue-analyze", ctx)
    assert result.exit_code == 0, result.reason
    assert analyzer.calls == 3
    assert reconcile_calls["n"] == 3
    assert pending_calls["n"] == 1
    assert materialize_calls["n"] == 1


def test_issue_reconcile_exhausted_retries_reaches_materializer_and_end(tmp_path: Path) -> None:
    project = _make_project_legacy(tmp_path)
    compiled, contracts = _compile_packaged()
    assert compiled.schema.graphs["issue-reconcile-workflow"].max_supersteps == 6
    (project / "qa" / "changes" / "CH-TRACE-13" / "inspect" / "issue-candidates.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "change_id": "CH-TRACE-13",
                "batch_id": "batch-1",
                "evidence_bundle_digest": "sha256:" + "a" * 64,
                "candidates": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    analyzer = ScriptedAnalyzer(succeed_on_attempt=1, batch_id="batch-1")
    reconcile, reconcile_calls = _counting_reconcile(fail_attempts=3)
    pending, pending_calls = _counting_pending()
    materialize, materialize_calls = _counting_materializer()
    runtime = _build_runtime_legacy(
        project,
        compiled,
        contracts,
        analyzer=analyzer,
        ops={
            "operation:reconcile-issues": reconcile,
            "operation:record-project-sync-pending": pending,
            "operation:materialize-trace-projection": materialize,
        },
    )
    change = project / "qa" / "changes" / "CH-TRACE-13"
    ctx = RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=change,
        change_id="CH-TRACE-13",
        params={"run_mode": "full"},
    )
    result = runtime.run(compiled, "issue-reconcile", ctx)
    assert result.exit_code == 0, result.reason
    assert reconcile_calls["n"] == 3
    assert pending_calls["n"] == 1
    assert materialize_calls["n"] == 1


# ---------------------------------------------------------------------------
# Task 14 Step 1 — B0→B1 terminal paths through current authority loader
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("parent", "child", "outcome"), TRACE_TERMINAL_CASES)
def test_trace_terminal_paths_publish_current_b1(
    tmp_path: Path,
    parent: str,
    child: str | None,
    outcome: str,
) -> None:
    _project, _change, _owner, projection = _run_terminal_path(
        tmp_path,
        parent=parent,
        child=child,
        outcome=outcome,  # type: ignore[arg-type]
    )
    if outcome == "success":
        assert projection.integrity in {"complete", "incomplete"}
        assert "issue_analysis_failed" not in {g.code for g in projection.gaps}
        assert "project_sync_pending" not in {g.code for g in projection.gaps}
    elif outcome == "analyzer_recovery":
        assert projection.integrity == "incomplete"
        assert {g.code for g in projection.gaps} == {"issue_analysis_failed"}
    else:
        assert projection.integrity == "incomplete"
        assert {g.code for g in projection.gaps} == {"project_sync_pending"}


# ---------------------------------------------------------------------------
# Task 14 Step 2 — main-graph happy + semantic failure + error-kind matrix
# ---------------------------------------------------------------------------


def test_assurance_happy_path_publishes_current_b1(tmp_path: Path) -> None:
    _project, _change, owner, projection = _run_terminal_path(
        tmp_path, parent="assurance", child="inspect-with-issues", outcome="success"
    )
    assert owner
    assert "issue_analysis_failed" not in {g.code for g in projection.gaps}
    assert "project_sync_pending" not in {g.code for g in projection.gaps}
    assert "issue_reconcile_failed" not in {g.code for g in projection.gaps}


def test_assurance_semantic_reconcile_failure_incomplete(tmp_path: Path) -> None:
    _project, change, owner, projection = _run_terminal_path(
        tmp_path, parent="assurance", child="inspect-with-issues", outcome="reconcile_failed"
    )
    assert projection.integrity == "incomplete"
    assert {g.code for g in projection.gaps} == {"issue_reconcile_failed"}
    assert _materializer_success_count(change, owner) == 1


@pytest.mark.parametrize("error_kind", ["timeout", "transport", "rate_limit", "invalid_output"])
def test_assurance_analyzer_recovery_error_kinds(tmp_path: Path, error_kind: ErrorKind) -> None:
    _project, change, owner, projection = _run_terminal_path(
        tmp_path,
        parent="assurance",
        child="inspect-with-issues",
        outcome="analyzer_recovery",
        analyzer_error_kind=error_kind,
    )
    assert projection.integrity == "incomplete"
    assert {g.code for g in projection.gaps} == {"issue_analysis_failed"}
    assert _materializer_success_count(change, owner) == 1


@pytest.mark.parametrize("error_kind", ["conflict", "transport"])
def test_assurance_sync_recovery_error_kinds(tmp_path: Path, error_kind: ErrorKind) -> None:
    _project, change, owner, projection = _run_terminal_path(
        tmp_path,
        parent="assurance",
        child="inspect-with-issues",
        outcome="sync_recovery",
        reconcile_error_kind=error_kind,
    )
    assert projection.integrity == "incomplete"
    assert {g.code for g in projection.gaps} == {"project_sync_pending"}
    assert _materializer_success_count(change, owner) == 1


# ---------------------------------------------------------------------------
# Task 14 Step 3 — materializer failure + healing / repeatability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "parent",
    ["assurance", "issue-analyze-workflow", "issue-reconcile-workflow"],
)
def test_materializer_fold_failure_publishes_nothing(
    tmp_path: Path, parent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assurance_agent.evidence import trace as trace_mod

    project = tmp_path / "proj"
    change = _seed_b0_completed(project)
    b0_bytes = (change / "inspect" / "trace-projection.json").read_bytes()
    if parent == "issue-reconcile-workflow":
        _advance_inputs_to_b1(change, seed_completed_analysis=True)
    else:
        _advance_inputs_to_b1(change, seed_completed_analysis=False)

    assurance = parent == "assurance"
    compiled, contracts = _compile_packaged(assurance_entrypoint=assurance)
    analyzer = ScriptedAnalyzer(succeed_on_attempt=1)
    ops: dict[str, OperationFn] = {}
    if assurance:
        ops.update(_assurance_stub_ops())

    def boom(*_a: object, **_k: object) -> object:
        raise TraceLayerSummaryError("injected materializer failure")

    monkeypatch.setattr(trace_mod, "summarize_projection_by_layer", boom)
    runtime = _build_runtime(
        project,
        compiled,
        contracts,
        analyzer=analyzer,
        ops=ops,
        stub_assurance_subgraphs=assurance,
    )
    entry = (
        "assurance-direct"
        if assurance
        else ("issue-analyze" if parent == "issue-analyze-workflow" else "issue-reconcile")
    )
    result = runtime.run(compiled, entry, _context(project))
    assert result.exit_code != 0
    assert result.status.status != "completed"
    # No newer projection published: either unchanged B0 bytes or missing.
    out = change / "inspect" / "trace-projection.json"
    if out.is_file():
        assert out.read_bytes() == b0_bytes
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["authoritative_batch_id"] == B0_BATCH


@pytest.mark.parametrize(
    ("parent", "prior_outcome"),
    [
        ("issue-analyze-workflow", "success"),
        ("issue-analyze-workflow", "analyzer_recovery"),
        ("issue-analyze-workflow", "sync_recovery"),
        ("issue-reconcile-workflow", "success"),
        ("issue-reconcile-workflow", "sync_recovery"),
    ],
)
def test_healing_rerun_at_b1_after_prior_terminal(tmp_path: Path, parent: str, prior_outcome: str) -> None:
    project, change, _owner, first = _run_terminal_path(
        tmp_path,
        parent=parent,  # type: ignore[arg-type]
        child=None,
        outcome=prior_outcome,  # type: ignore[arg-type]
    )
    first_digest = first.model_dump(mode="json")

    # Healing rerun with unchanged authoritative source bytes → byte-identical.
    compiled, contracts = _compile_packaged()
    analyzer = ScriptedAnalyzer(
        succeed_on_attempt=None if prior_outcome == "analyzer_recovery" else 1,
    )
    ops: dict[str, OperationFn] = {}
    if prior_outcome == "sync_recovery":
        reconcile, _ = _counting_reconcile(fail_attempts=3)
        ops["operation:reconcile-issues"] = reconcile
    runtime = _build_runtime(
        project,
        compiled,
        contracts,
        analyzer=analyzer,
        ops=ops,
    )
    entry = "issue-analyze" if parent == "issue-analyze-workflow" else "issue-reconcile"
    result = runtime.run(compiled, entry, _context(project))
    assert result.exit_code == 0, result.reason
    second = load_current_reconciled_projection(project, CHANGE_ID)
    assert second.authoritative_batch_id == B1_BATCH
    assert second.model_dump(mode="json") == first_digest
    live = fold_trace(project, CHANGE_ID, phase="reconciled")
    assert summarize_projection_by_layer(live).model_dump(mode="json") == summarize_projection_by_layer(
        second
    ).model_dump(mode="json")
    from assurance_agent.evidence.digests import projection_digest

    assert projection_digest(second) == projection_digest(live)
    # Canonical on-disk bytes match a fresh atomic write of the live fold.
    expected_path = change / "inspect" / "_expected-trace-projection.json"
    atomic_write_json(expected_path, live.model_dump(mode="json"))
    assert (change / "inspect" / "trace-projection.json").read_bytes() == expected_path.read_bytes()
    expected_path.unlink()


def test_prior_analysis_failed_cleared_by_analyze_success(tmp_path: Path) -> None:
    project, _change, _owner, prior = _run_terminal_path(
        tmp_path, parent="issue-analyze-workflow", child=None, outcome="analyzer_recovery"
    )
    assert {g.code for g in prior.gaps} == {"issue_analysis_failed"}

    compiled, contracts = _compile_packaged()
    analyzer = ScriptedAnalyzer(succeed_on_attempt=1)
    runtime = _build_runtime(project, compiled, contracts, analyzer=analyzer, ops={})
    result = runtime.run(compiled, "issue-analyze", _context(project))
    assert result.exit_code == 0, result.reason
    healed = load_current_reconciled_projection(project, CHANGE_ID)
    assert healed.authoritative_batch_id == B1_BATCH
    assert "issue_analysis_failed" not in {g.code for g in healed.gaps}


def test_prior_analysis_failed_remains_after_repeated_failure(tmp_path: Path) -> None:
    project, _change, _owner, prior = _run_terminal_path(
        tmp_path, parent="issue-analyze-workflow", child=None, outcome="analyzer_recovery"
    )
    compiled, contracts = _compile_packaged()
    analyzer = ScriptedAnalyzer(succeed_on_attempt=None)
    runtime = _build_runtime(project, compiled, contracts, analyzer=analyzer, ops={})
    result = runtime.run(compiled, "issue-analyze", _context(project))
    assert result.exit_code == 0, result.reason
    again = load_current_reconciled_projection(project, CHANGE_ID)
    assert again.integrity == "incomplete"
    assert {g.code for g in again.gaps} == {"issue_analysis_failed"}
    assert again.authoritative_batch_id == prior.authoritative_batch_id == B1_BATCH


def test_prior_sync_pending_cleared_by_reconcile_success(tmp_path: Path) -> None:
    project, _change, _owner, prior = _run_terminal_path(
        tmp_path, parent="issue-reconcile-workflow", child=None, outcome="sync_recovery"
    )
    assert {g.code for g in prior.gaps} == {"project_sync_pending"}

    compiled, contracts = _compile_packaged()
    analyzer = ScriptedAnalyzer(succeed_on_attempt=1)
    runtime = _build_runtime(project, compiled, contracts, analyzer=analyzer, ops={})
    result = runtime.run(compiled, "issue-reconcile", _context(project))
    assert result.exit_code == 0, result.reason
    healed = load_current_reconciled_projection(project, CHANGE_ID)
    assert "project_sync_pending" not in {g.code for g in healed.gaps}


def test_prior_sync_pending_remains_after_repeated_conflict(tmp_path: Path) -> None:
    project, _change, _owner, prior = _run_terminal_path(
        tmp_path, parent="issue-reconcile-workflow", child=None, outcome="sync_recovery"
    )
    compiled, contracts = _compile_packaged()
    analyzer = ScriptedAnalyzer(succeed_on_attempt=1)
    reconcile, _ = _counting_reconcile(fail_attempts=3)
    runtime = _build_runtime(
        project,
        compiled,
        contracts,
        analyzer=analyzer,
        ops={"operation:reconcile-issues": reconcile},
    )
    result = runtime.run(compiled, "issue-reconcile", _context(project))
    assert result.exit_code == 0, result.reason
    again = load_current_reconciled_projection(project, CHANGE_ID)
    assert again.integrity == "incomplete"
    assert {g.code for g in again.gaps} == {"project_sync_pending"}
    assert again.authoritative_batch_id == prior.authoritative_batch_id == B1_BATCH


# ---------------------------------------------------------------------------
# Clarification 10 — identity drift triad, pre-Task13 corpus, nested seams
# ---------------------------------------------------------------------------


def _pre_task13_independent_issue_schema(schema: Any) -> Any:
    """Independent issue graphs as of pre-Task13: no materializer, recover→END."""
    graphs = dict(schema.graphs)
    for graph_id, max_supersteps, edges in (
        (
            "issue-analyze-workflow",
            8,
            [
                EdgeDef.model_validate({"from": "START", "to": "analyze-issues"}),
                EdgeDef.model_validate({"from": "analyze-issues", "to": "reconcile-issues"}),
                EdgeDef.model_validate({"from": "reconcile-issues", "to": "END"}),
            ],
        ),
        (
            "issue-reconcile-workflow",
            4,
            [
                EdgeDef.model_validate({"from": "START", "to": "reconcile-issues"}),
                EdgeDef.model_validate({"from": "reconcile-issues", "to": "END"}),
            ],
        ),
    ):
        graph = graphs[graph_id]
        nodes = {
            nid: (
                node.model_copy(update={"recover": node.recover.model_copy(update={"continue_to": "END"})})
                if node.recover is not None and node.recover.continue_to == "materialize-trace-projection"
                else node
            )
            for nid, node in graph.nodes.items()
            if nid != "materialize-trace-projection"
        }
        graphs[graph_id] = graph.model_copy(
            update={"nodes": nodes, "edges": edges, "max_supersteps": max_supersteps}
        )
    return schema.model_copy(update={"graphs": graphs})


def _compile_pre_task13_issue_analyze() -> tuple[CompiledWorkflow, Any, IngestArtifactCatalog]:
    schema = _pre_task13_independent_issue_schema(load_workflow_v2(Path.cwd()))
    contracts = load_execution_contracts(Path.cwd())
    catalog = validate_catalog_runtime()
    compiled = compile_workflow(schema, contracts)
    assert "materialize-trace-projection" not in compiled.schema.graphs["issue-analyze-workflow"].nodes
    return compiled, contracts, catalog


def _compile_live_packaged() -> tuple[CompiledWorkflow, Any, IngestArtifactCatalog]:
    schema = load_workflow_v2(Path.cwd())
    contracts = load_execution_contracts(Path.cwd())
    catalog = validate_catalog_runtime()
    compiled = compile_packaged_workflow(schema, contracts)
    return compiled, contracts, catalog


def _mutated_live_contracts(contracts: ExecutionContractCatalog) -> ExecutionContractCatalog:
    target = "operation:record-issue-analysis-failure"
    base = contracts.contracts[target]
    flipped = base.model_copy(update={"side_effect_free": not base.side_effect_free})
    return ExecutionContractCatalog(contracts={**contracts.contracts, target: flipped})


def _mutated_live_catalog(catalog: IngestArtifactCatalog) -> IngestArtifactCatalog:
    symbol, spec = next((name, art) for name, art in catalog.artifacts.items() if art.kind == "path_only")
    drifted = spec.model_copy(update={"path": spec.path + ".clarification10-drift"})
    return catalog.model_copy(update={"artifacts": {**catalog.artifacts, symbol: drifted}})


def _runner_and_scheduler(
    *,
    compiled: CompiledWorkflow,
    contracts: Any,
    store: TreeStore,
    checkpoints: CheckpointStore,
    workspaces: WorkspaceBackend,
    clock: FakeClock,
    analyzer: ScriptedAnalyzer,
    ops: dict[str, OperationFn],
    run_child: Any,
    stub_post_healing_gates: bool = False,
) -> tuple[HandlerNodeRunner, Scheduler]:
    merged = default_operations()
    merged.update(ops)
    op_handler = OperationHandler(merged)
    agent = AgentHandler(SkillRouter(analyzer), store, contracts=contracts, compiled=compiled)
    handlers: dict[str, Any] = {
        "builtin:join": JoinHandler(),
        "builtin:gate": _gate_handler(compiled, stub_post_healing=stub_post_healing_gates),
        "builtin:interrupt": InterruptHandler(compiled),
        **{target: op_handler for target in merged},
    }
    node_runner = HandlerNodeRunner(
        handlers,
        namespace_handlers={"skill": agent, "graph": SubgraphHandler(run_child)},
        compiled=compiled,
        object_store=store,
    )
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
    return node_runner, scheduler


def _build_pinning_aware_runtime(
    project: Path,
    *,
    live_compiled: CompiledWorkflow,
    live_contracts: Any,
    live_catalog: IngestArtifactCatalog,
    analyzer: ScriptedAnalyzer,
    ops: dict[str, OperationFn],
    stub_assurance_subgraphs: bool = False,
) -> tuple[GraphRuntime, list[Any]]:
    """Resolver mirrors ``build_graph_runtime``: live hit or ``load_pinned_execution_definition``."""
    change = project / "qa" / "changes" / CHANGE_ID
    store = TreeStore(change)
    checkpoints = CheckpointStore(change)
    workspaces = WorkspaceBackend(change)
    clock = FakeClock()
    holder: dict[str, GraphRuntime] = {}
    pinned_loads: list[Any] = []

    def run_child(task: Any, graph_id: str, workspace: Any, context: Any) -> TaskResult:
        if stub_assurance_subgraphs and graph_id in STUB_SUBGRAPHS:
            return TaskResult(status="succeeded", value={"stubbed": graph_id})
        return holder["rt"].run_child(task, graph_id, workspace, context)

    def services_for(compiled: CompiledWorkflow, contracts: Any) -> tuple[HandlerNodeRunner, Scheduler]:
        return _runner_and_scheduler(
            compiled=compiled,
            contracts=contracts,
            store=store,
            checkpoints=checkpoints,
            workspaces=workspaces,
            clock=clock,
            analyzer=analyzer,
            ops=ops,
            run_child=run_child,
            stub_post_healing_gates=stub_assurance_subgraphs,
        )

    live_models = validate_ingest_model_map(live_catalog)
    live_runner, live_scheduler = services_for(live_compiled, live_contracts)
    live_request = request_for_compiled(live_compiled, event_schema_version=5)
    cache: dict[Any, ResolvedExecutionBundle] = {
        live_request: ResolvedExecutionBundle(
            request=live_request,
            compiled=live_compiled,
            contracts=live_contracts,
            ingest_catalog=live_catalog,
            model_map=live_models,
            node_runner=live_runner,
            scheduler=live_scheduler,
        )
    }

    def resolve(request: Any) -> ResolvedExecutionBundle:
        assert_live_semantic_compatibility(request)
        hit = cache.get(request)
        if hit is not None:
            return hit
        same = (
            request.graph_digest == live_compiled.digest
            and request.ingest_catalog_digest == live_compiled.ingest_catalog_digest
            and dict(request.contract_digests) == live_compiled.contract_digests
        )
        if same:
            bundle = ResolvedExecutionBundle(
                request=request,
                compiled=live_compiled,
                contracts=live_contracts,
                ingest_catalog=live_catalog,
                model_map=live_models,
                node_runner=live_runner,
                scheduler=live_scheduler,
            )
            cache[request] = bundle
            return bundle
        pinned = load_pinned_execution_definition(change, request)
        pinned_loads.append(request)
        pinned_models = validate_ingest_model_map(pinned.ingest_catalog)
        pinned_runner, pinned_scheduler = services_for(pinned.compiled, pinned.contracts)
        bundle = ResolvedExecutionBundle(
            request=request,
            compiled=pinned.compiled,
            contracts=pinned.contracts,
            ingest_catalog=pinned.ingest_catalog,
            model_map=pinned_models,
            node_runner=pinned_runner,
            scheduler=pinned_scheduler,
        )
        cache[request] = bundle
        return bundle

    runtime = GraphRuntime(
        checkpoint_store=checkpoints,
        object_store=store,
        workspace_backend=workspaces,
        definition_resolver=resolve,
        clock=clock,
    )
    holder["rt"] = runtime
    return runtime, pinned_loads


def _start_issue_analyze_pending_commit(
    project: Path,
    compiled: CompiledWorkflow,
    contracts: Any,
    catalog: IngestArtifactCatalog,
    *,
    analyzer: ScriptedAnalyzer,
    ops: dict[str, OperationFn],
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """Run until first successful task has a frozen write-set pending ordinary commit."""
    change = project / "qa" / "changes" / CHANGE_ID
    store = TreeStore(change)
    checkpoints = CheckpointStore(change)
    workspaces = WorkspaceBackend(change)
    clock = FakeClock()
    holder: dict[str, GraphRuntime] = {}

    def run_child(task: Any, graph_id: str, workspace: Any, context: Any) -> TaskResult:
        return holder["rt"].run_child(task, graph_id, workspace, context)

    node_runner, scheduler = _runner_and_scheduler(
        compiled=compiled,
        contracts=contracts,
        store=store,
        checkpoints=checkpoints,
        workspaces=workspaces,
        clock=clock,
        analyzer=analyzer,
        ops=ops,
        run_child=run_child,
    )
    runtime = GraphRuntime(
        checkpoint_store=checkpoints,
        object_store=store,
        workspace_backend=workspaces,
        definition_resolver=one_definition_resolver(
            compiled=compiled,
            contracts=contracts,
            ingest_catalog=catalog,
            node_runner=node_runner,
            scheduler=scheduler,
        ),
        clock=clock,
    )
    holder["rt"] = runtime

    orig = Scheduler._commit_wave

    def crash_first_commit(self: Scheduler, **kwargs: Any) -> list[str]:  # noqa: ANN401
        projection = kwargs["projection"]
        live = project_invocation(change, projection.invocation_id)
        if any(t.status == "succeeded" and not t.outputs_committed for t in live.tasks.values()):
            raise _InjectedCrash("ordinary pending commit seam")
        return orig(self, **kwargs)

    monkeypatch.setattr(Scheduler, "_commit_wave", crash_first_commit)
    with pytest.raises(_InjectedCrash, match="ordinary pending commit seam"):
        runtime.run(compiled, "issue-analyze", _context(project))
    monkeypatch.setattr(Scheduler, "_commit_wave", orig)
    events = read_events_strict(change)
    assert any(e.get("type") == "task_attempt_succeeded" for e in events)
    assert not any(e.get("type") == "superstep_committed" for e in events)
    return next(
        str(e["invocation_id"])
        for e in events
        if e.get("type") == "graph_invocation_started" and e.get("parent_invocation_id") is None
    )


@pytest.mark.parametrize("drift_kind", ["graph", "contract", "catalog"])
def test_identity_drift_triad_resumes_via_load_pinned(
    tmp_path: Path, drift_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live packaged digests ≠ pinned; resume continues via exact pinned snapshots."""
    project = tmp_path / "proj"
    change = _seed_b0_completed(project)
    _advance_inputs_to_b1(change, seed_completed_analysis=False)

    pinned_compiled, pinned_contracts, pinned_catalog = _compile_pre_task13_issue_analyze()
    live_compiled, live_contracts, live_catalog = _compile_live_packaged()
    if drift_kind == "graph":
        assert live_compiled.digest != pinned_compiled.digest
        resume_live = (live_compiled, live_contracts, live_catalog)
        start = (pinned_compiled, pinned_contracts, pinned_catalog)
    elif drift_kind == "contract":
        # Contract-only: keep post-Task13 graph/catalog; drift one live contract digest.
        start = (live_compiled, live_contracts, live_catalog)
        mutated = _mutated_live_contracts(live_contracts)
        drifted = compile_workflow(live_compiled.schema, mutated)
        assert drifted.digest == live_compiled.digest
        assert drifted.ingest_catalog_digest == live_compiled.ingest_catalog_digest
        assert drifted.contract_digests != live_compiled.contract_digests
        resume_live = (drifted, mutated, live_catalog)
    else:
        start = (live_compiled, live_contracts, live_catalog)
        mutated_catalog = _mutated_live_catalog(live_catalog)
        from assurance_agent.workflow.graph.compiler import _compile_with_catalog

        drifted = _compile_with_catalog(
            live_compiled.schema,
            contracts=live_contracts,
            ingest_catalog=mutated_catalog,
            activation_errors=(),
        )
        assert drifted.digest == live_compiled.digest
        assert drifted.contract_digests == live_compiled.contract_digests
        assert drifted.ingest_catalog_digest != live_compiled.ingest_catalog_digest
        resume_live = (drifted, live_contracts, mutated_catalog)

    start_compiled, start_contracts, start_catalog = start
    analyzer = ScriptedAnalyzer(succeed_on_attempt=None)
    invocation_id = _start_issue_analyze_pending_commit(
        project,
        start_compiled,
        start_contracts,
        start_catalog,
        analyzer=analyzer,
        ops={},
        monkeypatch=monkeypatch,
    )
    projection = project_invocation(change, invocation_id)
    assert projection.graph_digest == start_compiled.digest
    assert projection.terminal is None

    live_c, live_ct, live_cat = resume_live
    # Prove live packaging digests differ from the invocation pin for the drifted axis.
    if drift_kind == "graph":
        assert live_c.digest != projection.graph_digest
    elif drift_kind == "contract":
        assert dict(live_c.contract_digests) != dict(projection.contract_digests)
        assert live_c.digest == projection.graph_digest
        assert live_c.ingest_catalog_digest == projection.ingest_catalog_digest
    else:
        assert live_c.ingest_catalog_digest != projection.ingest_catalog_digest
        assert live_c.digest == projection.graph_digest
        assert dict(live_c.contract_digests) == dict(projection.contract_digests)

    resume_analyzer = ScriptedAnalyzer(succeed_on_attempt=None)
    runtime, pinned_loads = _build_pinning_aware_runtime(
        project,
        live_compiled=live_c,
        live_contracts=live_ct,
        live_catalog=live_cat,
        analyzer=resume_analyzer,
        ops={},
    )
    result = runtime.resume(invocation_id)
    assert result.exit_code == 0, result.reason
    assert pinned_loads, "resume must load committed snapshots when live identities drifted"
    req = pinned_loads[0]
    assert req.graph_digest == projection.graph_digest
    assert req.ingest_catalog_digest == projection.ingest_catalog_digest
    assert dict(req.contract_digests) == dict(projection.contract_digests)

    resolved = runtime._definition_resolver(req)  # noqa: SLF001
    assert resolved.compiled.digest == projection.graph_digest
    assert resolved.compiled.ingest_catalog_digest == projection.ingest_catalog_digest
    assert dict(resolved.compiled.contract_digests) == dict(projection.contract_digests)

    final = project_invocation(change, invocation_id)
    assert final.terminal == "completed"
    if drift_kind == "graph":
        # Pre-Task13 pinned topology: resume must not inject a materializer node.
        assert (
            "materialize-trace-projection"
            not in resolved.compiled.schema.graphs["issue-analyze-workflow"].nodes
        )
        assert _materializer_success_count(change, invocation_id) == 0
    else:
        # Contract/catalog-only drift may retain the post-Task13 materializer node; do not
        # claim identity drift suppresses its later legitimate execution under current handlers.
        assert (
            "materialize-trace-projection" in resolved.compiled.schema.graphs["issue-analyze-workflow"].nodes
        )


def test_pre_task13_resume_does_not_upgrade_v1_or_inject_materializer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "proj"
    change = _seed_b0_completed(project)
    _advance_inputs_to_b1(change, seed_completed_analysis=False)
    # Leave a legacy V1 projection so resume cannot be said to upgrade/backfill V2.
    (change / "inspect" / "trace-projection.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "change_id": CHANGE_ID,
                "phase": "reconciled",
                "authoritative_batch_id": B0_BATCH,
                "sources": [],
                "rows": [],
                "unmapped_tests": [],
                "gaps": [],
                "integrity": "incomplete",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    v1_before = (change / "inspect" / "trace-projection.json").read_bytes()

    pinned_compiled, pinned_contracts, pinned_catalog = _compile_pre_task13_issue_analyze()
    live_compiled, live_contracts, live_catalog = _compile_live_packaged()
    assert live_compiled.digest != pinned_compiled.digest
    assert "materialize-trace-projection" in live_compiled.schema.graphs["issue-analyze-workflow"].nodes

    invocation_id = _start_issue_analyze_pending_commit(
        project,
        pinned_compiled,
        pinned_contracts,
        pinned_catalog,
        analyzer=ScriptedAnalyzer(succeed_on_attempt=None),
        ops={},
        monkeypatch=monkeypatch,
    )
    runtime, pinned_loads = _build_pinning_aware_runtime(
        project,
        live_compiled=live_compiled,
        live_contracts=live_contracts,
        live_catalog=live_catalog,
        analyzer=ScriptedAnalyzer(succeed_on_attempt=None),
        ops={},
    )
    result = runtime.resume(invocation_id)
    assert result.exit_code == 0, result.reason
    assert pinned_loads
    final = project_invocation(change, invocation_id)
    assert final.terminal == "completed"
    assert _materializer_success_count(change, invocation_id) == 0
    assert not any(t.node_id == "materialize-trace-projection" for t in final.tasks.values())
    assert (change / "inspect" / "trace-projection.json").read_bytes() == v1_before


def _assurance_child_pending_commit_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, str, str]:
    """Crash after child materializer success before ordinary commit; return ids."""
    from assurance_agent.workflow.graph.workspace import TaskWorkspace

    project = tmp_path / "proj"
    change = _seed_b0_completed(project)
    _advance_inputs_to_b1(change, seed_completed_analysis=False)
    # Allow one abandon+retry of the parent inspect subgraph after the seam crash.
    compiled, contracts = _compile_packaged(assurance_entrypoint=True, inspect_retry="cli-transient")
    analyzer = ScriptedAnalyzer(succeed_on_attempt=1)
    ops = _assurance_stub_ops()
    runtime = _build_runtime(
        project,
        compiled,
        contracts,
        analyzer=analyzer,
        ops=ops,
        stub_assurance_subgraphs=True,
    )
    orig = Scheduler._commit_wave
    hit = {"n": 0}

    def crash_child_materializer(self: Scheduler, **kwargs: Any) -> list[str]:  # noqa: ANN401
        projection = kwargs["projection"]
        live = project_invocation(change, projection.invocation_id)
        if any(
            t.node_id == "materialize-trace-projection"
            and t.status == "succeeded"
            and not t.outputs_committed
            for t in live.tasks.values()
        ):
            hit["n"] += 1
            raise _InjectedCrash("child materializer pending commit")
        return orig(self, **kwargs)

    # Child context.project_root is the parent inspect task workspace. Keep it so
    # direct child resume can apply the frozen write-set (process-kill semantics).
    monkeypatch.setattr(TaskWorkspace, "cleanup", lambda self: None)
    monkeypatch.setattr(Scheduler, "_commit_wave", crash_child_materializer)
    with pytest.raises(_InjectedCrash, match="child materializer pending commit"):
        runtime.run(compiled, "assurance-direct", _context(project))
    monkeypatch.setattr(Scheduler, "_commit_wave", orig)
    assert hit["n"] == 1
    parent_id = _owner_invocation_id(change, child_graph=None)
    child_id = _owner_invocation_id(change, child_graph="inspect-with-issues")
    child = project_invocation(change, child_id)
    assert child.terminal is None
    assert any(
        t.node_id == "materialize-trace-projection" and t.status == "succeeded" and not t.outputs_committed
        for t in child.tasks.values()
    )
    return project, change, parent_id, child_id


def _abandon_running_parent_tasks(change: Path, parent_id: str) -> None:
    """Settle crashed ``running`` parent tasks so resume can re-enter ``run_child``."""
    from assurance_agent.workflow.graph.leases import LeaseRegistry, abandon_running_attempt

    lease_path = change / ".graph-runtime" / "running-tasks.json"
    if lease_path.is_file():
        lease_path.write_text('{"schema_version":1,"leases":[]}\n', encoding="utf-8")
    parent = project_invocation(change, parent_id)
    for task_id, task in parent.tasks.items():
        if task.status != "running" or task.latest_attempt_id is None:
            continue
        try:
            LeaseRegistry(change).remove(task_id, task.latest_attempt_id)
        except Exception:  # noqa: BLE001
            pass
        abandon_running_attempt(
            change,
            invocation_id=parent_id,
            checkpoint_ns=parent.checkpoint_ns,
            task_id=task_id,
            attempt_id=task.latest_attempt_id,
            reason="clarification10 nested seam crash",
            abandoned_at=(T0 + timedelta(hours=2)).isoformat(),
        )


def test_nested_child_owned_recovery_seam_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, change, _parent_id, child_id = _assurance_child_pending_commit_seam(tmp_path, monkeypatch)
    compiled, contracts = _compile_packaged(assurance_entrypoint=True, inspect_retry="cli-transient")
    runtime = _build_runtime(
        project,
        compiled,
        contracts,
        analyzer=ScriptedAnalyzer(succeed_on_attempt=1),
        ops=_assurance_stub_ops(),
        stub_assurance_subgraphs=True,
    )
    result = runtime.resume(child_id)
    assert result.exit_code == 0, result.reason
    child = project_invocation(change, child_id)
    assert child.terminal == "completed"
    assert _materializer_success_count(change, child_id) == 1
    assert any(
        t.node_id == "materialize-trace-projection" and t.outputs_committed for t in child.tasks.values()
    )
    # Nested child applies into the parent task workspace; live current-authority
    # publication is owned by the later parent subgraph commit — not asserted here.


def test_nested_incompatible_parent_epoch_leaves_child_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import assurance_agent.workflow.orchestration.gate_semantics as gate_sem

    project, change, parent_id, child_id = _assurance_child_pending_commit_seam(tmp_path, monkeypatch)
    before = project_invocation(change, child_id)
    pending_before = {
        tid
        for tid, t in before.tasks.items()
        if t.node_id == "materialize-trace-projection" and t.status == "succeeded" and not t.outputs_committed
    }
    assert pending_before
    compiled, contracts = _compile_packaged(assurance_entrypoint=True, inspect_retry="cli-transient")
    runtime = _build_runtime(
        project,
        compiled,
        contracts,
        analyzer=ScriptedAnalyzer(succeed_on_attempt=1),
        ops=_assurance_stub_ops(),
        stub_assurance_subgraphs=True,
    )
    monkeypatch.setattr(gate_sem, "gate_semantics_digest", lambda: "sha256:" + "0" * 64)
    with pytest.raises(GraphDefinitionChanged, match="gate semantics"):
        runtime.resume(parent_id)
    after = project_invocation(change, child_id)
    assert after.terminal is None
    pending_after = {
        tid
        for tid, t in after.tasks.items()
        if t.node_id == "materialize-trace-projection" and t.status == "succeeded" and not t.outputs_committed
    }
    assert pending_after == pending_before


def test_nested_compatible_parent_run_child_keeps_child_owned_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compatible parent resume drives ``run_child``, which performs child-owned recovery."""
    from assurance_agent.workflow.graph.workspace import TaskWorkspace, WorkspaceBackend

    project, change, parent_id, child_id = _assurance_child_pending_commit_seam(tmp_path, monkeypatch)
    child_before = project_invocation(change, child_id)
    assert child_before.terminal is None
    pending_before = {
        tid
        for tid, t in child_before.tasks.items()
        if t.node_id == "materialize-trace-projection" and t.status == "succeeded" and not t.outputs_committed
    }
    assert pending_before
    events_before = read_events_strict(change)
    child_commits_before = sum(
        1
        for e in events_before
        if e.get("type") == "superstep_committed" and e.get("invocation_id") == child_id
    )
    assert not any(
        t.node_id == "materialize-trace-projection" and t.outputs_committed
        for t in child_before.tasks.values()
    )

    # Process-kill seam: task workspace survives (cleanup already disabled in the
    # seam helper). Parent retry would otherwise ``rmtree`` that root in
    # ``WorkspaceBackend.create`` and make the child's frozen write-set
    # inapplicable. Reuse the surviving root so production ``run_child`` can
    # drive child recovery against the same sandbox.
    orig_create = WorkspaceBackend.create

    def create_reusing_surviving_root(
        self: WorkspaceBackend,
        *,
        task_id: str,
        base_tree_id: str,
        store: Any,
        sidecar_root: Path | None = None,
        side_effect_free: bool = False,
        claims: Any = None,
        declared_reads_only: bool = False,
        skill_name: str | None = None,
        initialize_git: bool = True,
    ) -> TaskWorkspace:
        root = self._tasks_root / task_id  # noqa: SLF001
        resolved_sidecar = (sidecar_root or self.sidecar_root_for(task_id)).resolve()
        if root.exists():
            manifest = resolved_sidecar / "tree.json"
            if not manifest.is_file():
                legacy = root / ".graph-runtime" / "tree.json"
                if legacy.is_file():
                    manifest = legacy
            return TaskWorkspace.from_materialized_root(
                task_id,
                root,
                base_tree_id,
                materialized_tree_id=base_tree_id,
                tree_manifest_path=manifest,
                sidecar_root=resolved_sidecar if manifest.parent == resolved_sidecar else None,
            )
        return orig_create(
            self,
            task_id=task_id,
            base_tree_id=base_tree_id,
            store=store,
            sidecar_root=resolved_sidecar,
            side_effect_free=side_effect_free,
            claims=claims,
            declared_reads_only=declared_reads_only,
            skill_name=skill_name,
            initialize_git=initialize_git,
        )

    monkeypatch.setattr(WorkspaceBackend, "create", create_reusing_surviving_root)

    # Do NOT resume the child directly: parent must drive recovery via run_child.
    _abandon_running_parent_tasks(change, parent_id)
    compiled, contracts = _compile_packaged(assurance_entrypoint=True, inspect_retry="cli-transient")
    parent_runtime = _build_runtime(
        project,
        compiled,
        contracts,
        analyzer=ScriptedAnalyzer(succeed_on_attempt=1),
        ops=_assurance_stub_ops(),
        stub_assurance_subgraphs=True,
    )
    result = parent_runtime.resume(parent_id)
    assert result.exit_code == 0, result.reason

    parent = project_invocation(change, parent_id)
    child = project_invocation(change, child_id)
    assert parent.terminal == "completed"
    assert child.terminal == "completed"
    assert _materializer_success_count(change, child_id) == 1
    assert any(
        t.node_id == "materialize-trace-projection" and t.outputs_committed for t in child.tasks.values()
    )
    assert all(t.node_id != "materialize-trace-projection" for t in parent.tasks.values())

    events = read_events_strict(change)
    child_commits_after = sum(
        1 for e in events if e.get("type") == "superstep_committed" and e.get("invocation_id") == child_id
    )
    assert child_commits_after > child_commits_before
    # Child-owned recovery/commit events stay on the child invocation id; parent
    # never hosts a materializer task (so it cannot claim those commits).
    assert all(
        e.get("invocation_id") != parent_id or "materialize-trace-projection" not in json.dumps(e)
        for e in events
        if e.get("type") == "superstep_committed"
    )
    # Parent retry's run_child targets the same child invocation (same task_id → same child id).
    assert any(
        e.get("type") == "graph_invocation_started"
        and e.get("invocation_id") == child_id
        and e.get("parent_invocation_id") == parent_id
        for e in events
    )
