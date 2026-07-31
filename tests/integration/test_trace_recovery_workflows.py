"""Task 13: exhausted-retry issue paths still deliver END after materialization."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from assurance_agent.workflow.core.events import read_events_strict
from assurance_agent.workflow.driver.runtime_factory import one_definition_resolver
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from assurance_agent.workflow.graph.checkpoint import CheckpointStore
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.graph.handlers.agent import AgentHandler
from assurance_agent.workflow.graph.handlers.gate import GateHandler
from assurance_agent.workflow.graph.handlers.interrupt import InterruptHandler
from assurance_agent.workflow.graph.handlers.join import JoinHandler
from assurance_agent.workflow.graph.handlers.operation import OperationFn, OperationHandler, default_operations
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
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2
from assurance_agent.workflow.graph.task_runner import HandlerNodeRunner
from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceBackend
from tests.helpers_aa import write_aa_config

T0 = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)
CHANGE_ID = "CH-TRACE-13"


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


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    change = project / "qa" / "changes" / CHANGE_ID
    change.mkdir(parents=True)
    (project / "qa" / "issues").mkdir(parents=True)
    write_aa_config(project)
    _seed_inspect_inputs(change)
    return project


def _seed_inspect_inputs(change: Path) -> None:
    inspect_dir = change / "inspect"
    inspect_dir.mkdir(parents=True, exist_ok=True)
    issues_dir = change / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    (inspect_dir / "observations.json").write_text(
        json.dumps({"schema_version": "1.0", "change_id": CHANGE_ID, "observations": [], "abnormal_count": 1})
        + "\n",
        encoding="utf-8",
    )
    (inspect_dir / "issue-evidence-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "change_id": CHANGE_ID,
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
        json.dumps({"schema_version": "1.0", "change_id": CHANGE_ID, "occurrences": []}) + "\n",
        encoding="utf-8",
    )


def _context(project: Path) -> RuntimeContext:
    change = project / "qa" / "changes" / CHANGE_ID
    return RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=change,
        change_id=CHANGE_ID,
        params={"run_mode": "full"},
    )


def _compile_packaged() -> tuple[CompiledWorkflow, Any]:
    schema = load_workflow_v2(Path.cwd())
    contracts = load_execution_contracts(Path.cwd())
    return compile_workflow(schema, contracts), contracts


class _ScriptedAnalyzer:
    """Succeeds only on the Nth invoke (1-based)."""

    def __init__(self, *, succeed_on_attempt: int) -> None:
        self.succeed_on_attempt = succeed_on_attempt
        self.calls = 0

    def invoke(self, request: AgentRequest) -> AgentResult:
        self.calls += 1
        if self.calls < self.succeed_on_attempt:
            return AgentResult(ok=False, error_kind="timeout", error="scripted analyzer timeout")
        workspace = Path(request.workspace_root)
        change_root = workspace / "qa" / "changes" / request.change_id
        inspect_dir = change_root / "inspect"
        inspect_dir.mkdir(parents=True, exist_ok=True)
        candidates = {
            "schema_version": "1.0",
            "change_id": request.change_id,
            "batch_id": "batch-1",
            "evidence_bundle_digest": "sha256:" + "a" * 64,
            "candidates": [],
        }
        (inspect_dir / "issue-candidates.json").write_text(
            json.dumps(candidates, sort_keys=True) + "\n", encoding="utf-8"
        )
        (inspect_dir / "issue-analysis-status.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "change_id": request.change_id,
                    "batch_id": "batch-1",
                    "status": "completed",
                    "evidence_bundle_digest": candidates["evidence_bundle_digest"],
                    "candidate_count": 0,
                    "candidate_digest": "sha256:" + "b" * 64,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return AgentResult(ok=True)


def _counting_reconcile_conflict(*, fail_attempts: int) -> tuple[OperationFn, dict[str, int]]:
    calls = {"n": 0}

    def _fn(task: ExecutableTask, workspace: Any, context: RuntimeContext) -> TaskResult:
        del task
        calls["n"] += 1
        if calls["n"] <= fail_attempts:
            return TaskResult(status="failed", error_kind="conflict", error="scripted project sync conflict")
        # Should not succeed in the exhausted-retry scenarios under test.
        inspect_dir = workspace.change_dir / "inspect"
        inspect_dir.mkdir(parents=True, exist_ok=True)
        (inspect_dir / "issue-reconcile-status.json").write_text(
            json.dumps({"status": "completed"}) + "\n", encoding="utf-8"
        )
        return TaskResult(status="succeeded", value={"occurrence_count": 0})

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


def _build_runtime(
    project: Path,
    compiled: CompiledWorkflow,
    contracts: Any,
    *,
    analyzer: _ScriptedAnalyzer,
    ops: dict[str, OperationFn],
) -> GraphRuntime:
    change = project / "qa" / "changes" / CHANGE_ID
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


def _assert_completed_without_budget_exhaustion(result: Any, change: Path) -> None:
    assert result.exit_code == 0, result.reason
    assert result.status.status == "completed", result.reason
    events = read_events_strict(change)
    assert not any(
        e.get("type") == "invocation_failed" and "max_supersteps" in str(e.get("reason", "")) for e in events
    )
    assert "max_supersteps exhausted" not in str(result.reason or "")


def test_issue_analyze_exhausted_reconcile_reaches_materializer_and_end(tmp_path: Path) -> None:
    """Analyzer succeeds on attempt 3; reconcile exhausts 3 attempts; pending + materializer + END."""
    project = _make_project(tmp_path)
    compiled, contracts = _compile_packaged()
    assert compiled.schema.graphs["issue-analyze-workflow"].max_supersteps == 9

    analyzer = _ScriptedAnalyzer(succeed_on_attempt=3)
    reconcile, reconcile_calls = _counting_reconcile_conflict(fail_attempts=3)
    pending, pending_calls = _counting_pending()
    materialize, materialize_calls = _counting_materializer()
    runtime = _build_runtime(
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
    change = project / "qa" / "changes" / CHANGE_ID
    result = runtime.run(compiled, "issue-analyze", _context(project))

    _assert_completed_without_budget_exhaustion(result, change)
    assert analyzer.calls == 3
    assert reconcile_calls["n"] == 3
    assert pending_calls["n"] == 1
    assert materialize_calls["n"] == 1
    assert (change / "inspect" / "trace-projection.json").is_file()


def test_issue_reconcile_exhausted_retries_reaches_materializer_and_end(tmp_path: Path) -> None:
    """Reconcile exhausts 3 attempts; pending recovery + materializer + END under max_supersteps=6."""
    project = _make_project(tmp_path)
    compiled, contracts = _compile_packaged()
    assert compiled.schema.graphs["issue-reconcile-workflow"].max_supersteps == 6

    # Seed candidates required by pending recovery reads under real contracts when not overridden.
    (project / "qa" / "changes" / CHANGE_ID / "inspect" / "issue-candidates.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "change_id": CHANGE_ID,
                "batch_id": "batch-1",
                "evidence_bundle_digest": "sha256:" + "a" * 64,
                "candidates": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    analyzer = _ScriptedAnalyzer(succeed_on_attempt=1)
    reconcile, reconcile_calls = _counting_reconcile_conflict(fail_attempts=3)
    pending, pending_calls = _counting_pending()
    materialize, materialize_calls = _counting_materializer()
    runtime = _build_runtime(
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
    change = project / "qa" / "changes" / CHANGE_ID
    result = runtime.run(compiled, "issue-reconcile", _context(project))

    _assert_completed_without_budget_exhaustion(result, change)
    assert reconcile_calls["n"] == 3
    assert pending_calls["n"] == 1
    assert materialize_calls["n"] == 1
    assert (change / "inspect" / "trace-projection.json").is_file()
