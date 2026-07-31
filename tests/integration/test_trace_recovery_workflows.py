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
from assurance_agent.workflow.driver.runtime_factory import one_definition_resolver
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult, ErrorKind
from assurance_agent.workflow.graph.checkpoint import CheckpointStore, project_invocation
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.graph.handlers.agent import AgentHandler
from assurance_agent.workflow.graph.handlers.gate import GateHandler
from assurance_agent.workflow.graph.handlers.interrupt import InterruptHandler
from assurance_agent.workflow.graph.handlers.join import JoinHandler
from assurance_agent.workflow.graph.handlers.operation import (
    OperationFn,
    OperationHandler,
    default_operations,
)
from assurance_agent.workflow.graph.handlers.subgraph import SubgraphHandler
from assurance_agent.workflow.graph.ingest_catalog import validate_catalog_runtime
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.runtime import GraphRuntime
from assurance_agent.workflow.graph.scheduler import Scheduler
from assurance_agent.workflow.graph.schema_v2 import EntrypointDef, load_workflow_v2
from assurance_agent.workflow.graph.task_runner import HandlerNodeRunner
from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceBackend
from assurance_agent.workflow.improvements.ledger import atomic_write_json
from tests.helpers_aa import write_aa_config
from tests.unit.evidence import test_issue_replay_authority as auth
from tests.unit.evidence.test_fold_trace_reconciled import (
    _write_api_case,
    _write_selected_api_result,
    materialize_reconciled_v2,
)

T0 = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)
CHANGE_ID = auth.CHANGE_ID
B0_BATCH = auth.BATCH_ID
B1_BATCH = "B1"
STUB_SUBGRAPHS = frozenset({"api-branch", "e2e-branch", "fuzz-branch", "performance-branch", "healing"})

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


def _compile_packaged(*, assurance_entrypoint: bool = False) -> tuple[CompiledWorkflow, Any]:
    schema = load_workflow_v2(Path.cwd())
    if assurance_entrypoint:
        schema = schema.model_copy(
            update={
                "entrypoints": {
                    **dict(schema.entrypoints),
                    "assurance-direct": EntrypointDef(graph="assurance"),
                }
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

    return {
        "operation:run-tests": stub_run_tests,
        "operation:inspect": stub_inspect,
        "operation:collect-observations": stub_collect,
        "operation:generate-report": stub_report,
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
        "builtin:gate": GateHandler(compiled),
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
        del task, context
        calls["n"] += 1
        inspect_dir = workspace.change_dir / "inspect"
        inspect_dir.mkdir(parents=True, exist_ok=True)
        (inspect_dir / "trace-projection.json").write_text(
            json.dumps({"schema_version": "2", "phase": "reconciled", "integrity": "incomplete"}) + "\n",
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
    "prior_outcome",
    ["success", "analyzer_recovery", "sync_recovery"],
)
def test_healing_rerun_at_b1_after_prior_terminal(tmp_path: Path, prior_outcome: str) -> None:
    project, change, _owner, first = _run_terminal_path(
        tmp_path,
        parent="issue-analyze-workflow",
        child=None,
        outcome=prior_outcome,  # type: ignore[arg-type]
    )
    first_digest = first.model_dump(mode="json")

    # Second healing rerun with unchanged authoritative source bytes → byte-identical.
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
    result = runtime.run(compiled, "issue-analyze", _context(project))
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
