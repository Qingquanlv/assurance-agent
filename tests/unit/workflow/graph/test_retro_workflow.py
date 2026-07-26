"""Graph-level retro workflow tests: dry-run, zero-signal, and propose+reconcile.

These tests drive the canonical ``retro`` entrypoint through a minimal
``GraphRuntime`` to verify the three distinct routing branches of
``retro-workflow`` (collect-only / noop / full propose+reconcile).

Design note on test_retro_workflow_reconcile_accepts_candidates
--------------------------------------------------------------------
Candidate evidence is read from the *host* project root (``retro_collect`` passes
``context.project_root`` for reads and the workspace only for writes), so seeding
it here would mean planting archived changes plus a projectable ledger on the host
and letting the real aggregator derive signals from them.

To exercise the propose+reconcile branch in isolation we instead replace
``operation:retro-collect`` with a stub that writes a minimal schema-v2
``context.json`` with ``signal_count=1`` and returns the matching value.
Everything else (propose via FakeRetroAgent + reconcile via the real handler)
runs through the unmodified product code path.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from assurance_agent import resources
from assurance_agent.retro.candidates import context_sha256
from assurance_agent.retro.types import (
    EvalRetroSignals,
    IssueRetroSignals,
    RetroContext,
    RetroIntegrity,
    RetroSelectionSnapshot,
    RetroSignalSet,
    RetroSourceDescriptor,
    RetroSourceManifest,
    RetroWindow,
    WorkflowRetroSignals,
)
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from assurance_agent.workflow.graph.checkpoint import CheckpointStore
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.graph.handlers.operation import (
    OperationHandler,
    default_operations,
)
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.runtime import GraphRuntime
from assurance_agent.workflow.graph.scheduler import Scheduler
from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2
from assurance_agent.workflow.graph.task_runner import (
    HandlerNodeRunner,
    build_default_node_runner,
)
from assurance_agent.workflow.graph.workspace import TaskWorkspace, TreeStore, WorkspaceBackend
from tests.helpers_aa import write_aa_config

T0 = datetime(2026, 7, 25, 0, 0, 0, tzinfo=timezone.utc)


class NeverCalledInvoker:
    """Asserts that no agent node is ever invoked."""

    def invoke(self, request: AgentRequest) -> AgentResult:
        raise AssertionError(f"AgentInvoker must not be called; target={request.target}")


class FakeRetroAgent:
    """Writes schema-v2 proposal-candidates.json into the task workspace."""

    def __init__(self, retro_id: str, *, context: RetroContext) -> None:
        self._retro_id = retro_id
        self._context = context

    def invoke(self, request: AgentRequest) -> AgentResult:
        retro_dir = request.workspace_root / "qa" / "retro" / self._retro_id
        retro_dir.mkdir(parents=True, exist_ok=True)
        document = {
            "schema_version": "2",
            "retro_id": self._retro_id,
            "context_sha256": context_sha256(self._context),
            "candidates": [
                {
                    "candidate_id": "IMP-CAND-1",
                    "kind": "workflow_improvement",
                    "delivery": "change_draft",
                    "source_refs": {"problem_ids": ["PROB-1"]},
                    "target": "assurance_agent/workflow/inspect",
                    "rationale": "Repeated truncation across changes",
                    "proposed_change": "Preserve pytest E lines when classifying failures",
                    "verification": {
                        "suites": ["workflow-full"],
                        "success_criteria": "No truncation Observation",
                    },
                    "risk": "low",
                    "confidence": "high",
                }
            ],
        }
        (retro_dir / "proposal-candidates.json").write_text(
            json.dumps(document, sort_keys=True) + "\n", encoding="utf-8"
        )
        (retro_dir / "retro-summary.md").write_text(
            "# Retro summary\n\nSynthetic summary for test.\n", encoding="utf-8"
        )
        return AgentResult(ok=True)


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
    retro_run = project / "qa" / "changes" / "RETRO-RUN"
    retro_run.mkdir(parents=True)
    write_aa_config(project)
    return project


def _compile_canonical() -> tuple[CompiledWorkflow, object]:
    text = resources.read_text("schemas", "workflow-schema.yaml")
    contracts = load_execution_contracts(Path("."))
    compiled = compile_workflow(parse_workflow_v2(text), contracts)
    return compiled, contracts


def _build_runtime(
    project: Path,
    compiled: CompiledWorkflow,
    contracts: object,
    invoker: object,
    *,
    extra_ops: dict | None = None,
) -> GraphRuntime:
    change_dir = project / "qa" / "changes" / "RETRO-RUN"
    store = TreeStore(change_dir)
    checkpoints = CheckpointStore(change_dir)
    workspaces = WorkspaceBackend(change_dir)
    clock = FakeClock()
    holder: dict[str, GraphRuntime] = {}

    def run_child(task, graph_id, workspace, context):  # type: ignore[no-untyped-def]
        return holder["rt"].run_child(task, graph_id, workspace, context)

    node_runner = build_default_node_runner(
        invoker,  # type: ignore[arg-type]
        store,
        contracts,  # type: ignore[arg-type]
        compiled=compiled,
        run_child=run_child,
    )
    if extra_ops:
        op_handler = OperationHandler({**default_operations(), **extra_ops})
        assert isinstance(node_runner, HandlerNodeRunner)
        node_runner._handlers.update(  # noqa: SLF001
            {target: op_handler for target in extra_ops}
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
        contracts=contracts,  # type: ignore[arg-type]
        state_defs=state_defs,
    )
    schemas = {compiled.digest: compiled}
    runtime = GraphRuntime(
        checkpoint_store=checkpoints,
        object_store=store,
        workspace_backend=workspaces,
        contracts=contracts,  # type: ignore[arg-type]
        node_runner=node_runner,
        scheduler=scheduler,
        schema_resolver=lambda digest: schemas[digest],
        clock=clock,
    )
    holder["rt"] = runtime
    return runtime


def _retro_context(
    project: Path,
    retro_id: str,
    *,
    dry_run: bool = False,
) -> RuntimeContext:
    return RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=project / "qa" / "changes" / "RETRO-RUN",
        change_id="RETRO-RUN",
        params={
            "retro_id": retro_id,
            "retro_dry_run": dry_run,
            "retro_last": 10,
            "retro_min_evidence": 1,
        },
    )


def _v2_context(retro_id: str, *, incomplete: bool = False) -> RetroContext:
    return RetroContext(
        retro_id=retro_id,
        generated_at="2026-07-25T00:00:00Z",
        window=RetroWindow(
            selection=RetroSelectionSnapshot(mode="change_ids", requested_change_ids=("CH-SEED-1",)),
            change_ids=("CH-SEED-1",),
        ),
        source_manifest=RetroSourceManifest(
            issue_slice_sha256="sha256:slice",
            issue_sources=(
                RetroSourceDescriptor(
                    kind="change_issue_ledger",
                    change_id="CH-SEED-1",
                    sha256="sha256:issue",
                    evidence_ids=("PROB-1",),
                ),
            ),
            workflow_sources=(),
            eval_sources=(),
        ),
        integrity=RetroIntegrity(
            status="incomplete" if incomplete else "complete",
            reasons=("analysis_failed",) if incomplete else (),
        ),
        signals=RetroSignalSet(
            issue=IssueRetroSignals(),
            workflow=WorkflowRetroSignals(),
            eval=EvalRetroSignals(),
        ),
        signal_count=1,
    )


def _fake_retro_collect_with_signal(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Stub retro-collect that returns signal_count=1 without scanning for candidates."""
    retro_id = str(context.params.get("retro_id", ""))
    retro_dir = workspace.project_root / "qa" / "retro" / retro_id
    retro_dir.mkdir(parents=True, exist_ok=True)
    ctx = _v2_context(retro_id)
    (retro_dir / "context.json").write_text(
        json.dumps(ctx.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return TaskResult(
        status="succeeded",
        value={"retro_id": retro_id, "signal_count": 1},
    )


def test_retro_workflow_dry_run_stops_after_collect(tmp_path: Path) -> None:
    """retro_dry_run=true: collect runs, no agent, graph completes; no candidates."""
    project = _make_project(tmp_path)
    retro_id = "retro-dry-001"
    compiled, contracts = _compile_canonical()
    runtime = _build_runtime(project, compiled, contracts, NeverCalledInvoker())
    context = _retro_context(project, retro_id, dry_run=True)

    result = runtime.run(compiled, "retro", context)

    assert result.exit_code == 0, result.reason
    assert result.status.status == "completed"
    context_file = project / "qa" / "retro" / retro_id / "context.json"
    assert context_file.exists(), "context.json must be written by collect"
    candidates = project / "qa" / "retro" / retro_id / "proposal-candidates.json"
    assert not candidates.exists(), "proposal-candidates.json must NOT exist on dry run"


def test_retro_workflow_zero_signals_ends_without_propose(tmp_path: Path) -> None:
    """Empty archive → signal_count=0 → collect-to-END; no agent, no candidates."""
    project = _make_project(tmp_path)
    retro_id = "retro-noop-001"
    compiled, contracts = _compile_canonical()
    runtime = _build_runtime(project, compiled, contracts, NeverCalledInvoker())
    context = _retro_context(project, retro_id, dry_run=False)

    result = runtime.run(compiled, "retro", context)

    assert result.exit_code == 0, result.reason
    assert result.status.status == "completed"
    context_file = project / "qa" / "retro" / retro_id / "context.json"
    assert context_file.exists(), "context.json must be written by collect"
    ctx_data = json.loads(context_file.read_text(encoding="utf-8"))
    assert ctx_data.get("signal_count") == 0
    candidates = project / "qa" / "retro" / retro_id / "proposal-candidates.json"
    assert not candidates.exists(), "proposal-candidates.json must NOT exist when signal_count==0"


def test_retro_workflow_reconcile_accepts_candidates(tmp_path: Path) -> None:
    """Stub collect with signal_count=1 → propose+reconcile; Improvement Ledger updated."""
    project = _make_project(tmp_path)
    retro_id = "retro-accept-001"
    compiled, contracts = _compile_canonical()
    ctx = _v2_context(retro_id)
    invoker = FakeRetroAgent(retro_id, context=ctx)
    extra_ops = {"operation:retro-collect": _fake_retro_collect_with_signal}
    runtime = _build_runtime(project, compiled, contracts, invoker, extra_ops=extra_ops)
    context = _retro_context(project, retro_id, dry_run=False)

    result = runtime.run(compiled, "retro", context)

    assert result.exit_code == 0, result.reason
    assert result.status.status == "completed"
    retro_dir = project / "qa" / "retro" / retro_id
    assert (retro_dir / "proposal-candidates.json").is_file()
    assert (retro_dir / "accept-status.json").is_file()
    assert (retro_dir / "review-queue.md").is_file()
    assert (retro_dir / "retro-summary.md").is_file()
    status = json.loads((retro_dir / "accept-status.json").read_text(encoding="utf-8"))
    assert status["result"] == "accepted"
    assert (project / "qa" / "improvements" / "events.jsonl").is_file()
