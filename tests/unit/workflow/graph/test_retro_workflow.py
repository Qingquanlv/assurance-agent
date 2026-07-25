"""Graph-level retro workflow tests: dry-run, zero-signal, and accept-rewrite.

These tests drive the canonical ``retro`` entrypoint through a minimal
``GraphRuntime`` to verify the three distinct routing branches of
``retro-workflow`` (collect-only / noop / full propose+accept).

Design note on test_retro_workflow_accept_rewrites_legacy_proposals
--------------------------------------------------------------------
Candidate evidence is read from the *host* project root (``retro_collect`` passes
``context.project_root`` for reads and the workspace only for writes), so seeding
it here would mean planting archived changes plus a projectable ledger on the host
and letting the real aggregator derive signals from them.

To exercise the propose+accept branch in isolation we instead replace
``operation:retro-collect`` with a stub that writes a minimal
``context.json`` with ``signal_count=1`` and returns the matching value.
Everything else (propose via FakeRetroAgent + accept via the real handler)
runs through the unmodified product code path.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from assurance_agent import resources
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

# ---------------------------------------------------------------------------
# Invokers
# ---------------------------------------------------------------------------


class NeverCalledInvoker:
    """Asserts that no agent node is ever invoked."""

    def invoke(self, request: AgentRequest) -> AgentResult:
        raise AssertionError(f"AgentInvoker must not be called; target={request.target}")


class FakeRetroAgent:
    """Writes legacy proposals (no finding_kind/payload) to the task workspace.

    Cites the evidence id the stub context carries: accept refuses proposals whose
    ``evidence_ids`` are absent from the collected context.
    """

    def __init__(self, retro_id: str) -> None:
        self._retro_id = retro_id

    def invoke(self, request: AgentRequest) -> AgentResult:
        retro_dir = request.workspace_root / "qa" / "retro" / self._retro_id
        retro_dir.mkdir(parents=True, exist_ok=True)
        # Legacy shape: apply_kind + proposed_change, no finding_kind/payload.
        proposals = [
            {
                "id": "p1",
                "apply_kind": "memory_append",
                "proposed_change": "Update the prompt rule to handle edge cases.",
                "evidence_ids": ["CH-SEED-1#seq1"],
                "problem": "Prompt rule misses edge cases",
            }
        ]
        (retro_dir / "proposals.json").write_text(json.dumps(proposals), encoding="utf-8")
        # propose also declares retro-summary.md as a required output
        (retro_dir / "retro-summary.md").write_text(
            "# Retro summary\n\nSynthetic summary for test.\n", encoding="utf-8"
        )
        return AgentResult(ok=True)


# ---------------------------------------------------------------------------
# Fixtures
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
        # Overlay custom operation handlers on the default runner (same pattern
        # as test_finalize_and_child_stop).
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


def _fake_retro_collect_with_signal(
    task: ExecutableTask,
    workspace: TaskWorkspace,
    context: RuntimeContext,
) -> TaskResult:
    """Stub retro-collect that returns signal_count=1 without scanning for candidates.

    Writes a minimal context.json directly and returns the matching value dict so
    the edge expression ``node('collect').value.signal_count > 0`` evaluates to
    True, keeping this test focused on the propose+accept branch.
    """
    retro_id = context.params.get("retro_id", "")
    retro_dir = workspace.project_root / "qa" / "retro" / str(retro_id)
    retro_dir.mkdir(parents=True, exist_ok=True)
    ctx = {
        "retro_id": retro_id,
        "generated_at": "2026-07-25T00:00:00Z",
        "window": {"change_count": 1, "change_ids": ["CH-SEED-1"], "change_sources": []},
        "signals": {
            "failure_distribution": [],
            "gate_pushback": [
                {
                    "gate": "test-gate",
                    "verdict": "needs_fix",
                    "count": 1,
                    "top_reasons": [],
                    "evidence_ids": ["CH-SEED-1#seq1"],
                    "changes": ["CH-SEED-1"],
                }
            ],
            "healing_efficiency": {"attempts": 0, "applied": 0, "success_rate": 0.0, "evidence_ids": []},
            "human_decisions": [],
            "reclassifications": [],
            "skill_execution": [],
            "eval_trend": [],
        },
        "signal_count": 1,
    }
    (retro_dir / "context.json").write_text(json.dumps(ctx), encoding="utf-8")
    return TaskResult(
        status="succeeded",
        value={"retro_id": retro_id, "signal_count": 1},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_retro_workflow_dry_run_stops_after_collect(tmp_path: Path) -> None:
    """retro_dry_run=true: collect runs, no agent, graph completes; no proposals."""
    project = _make_project(tmp_path)
    retro_id = "retro-dry-001"
    compiled, contracts = _compile_canonical()
    runtime = _build_runtime(project, compiled, contracts, NeverCalledInvoker())
    context = _retro_context(project, retro_id, dry_run=True)

    result = runtime.run(compiled, "retro", context)

    assert result.exit_code == 0, result.reason
    assert result.status.status == "completed"
    # collect wrote context.json
    context_file = project / "qa" / "retro" / retro_id / "context.json"
    assert context_file.exists(), "context.json must be written by collect"
    # propose was skipped (dry_run edge)
    proposals_file = project / "qa" / "retro" / retro_id / "proposals.json"
    assert not proposals_file.exists(), "proposals.json must NOT exist on dry run"


def test_retro_workflow_zero_signals_ends_without_propose(tmp_path: Path) -> None:
    """Empty archive → signal_count=0 → collect-to-END; no agent, no proposals."""
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
    proposals_file = project / "qa" / "retro" / retro_id / "proposals.json"
    assert not proposals_file.exists(), "proposals.json must NOT exist when signal_count==0"


def test_retro_workflow_accept_rewrites_legacy_proposals(tmp_path: Path) -> None:
    """Stub collect with signal_count=1 → propose+accept; legacy proposals get finding_kind+payload."""
    project = _make_project(tmp_path)
    retro_id = "retro-accept-001"
    compiled, contracts = _compile_canonical()
    invoker = FakeRetroAgent(retro_id)
    # Override operation:retro-collect with a stub that returns signal_count=1.
    # See module docstring for why real archived evidence cannot be used here.
    extra_ops = {"operation:retro-collect": _fake_retro_collect_with_signal}
    runtime = _build_runtime(project, compiled, contracts, invoker, extra_ops=extra_ops)
    context = _retro_context(project, retro_id, dry_run=False)

    result = runtime.run(compiled, "retro", context)

    assert result.exit_code == 0, result.reason
    assert result.status.status == "completed"
    retro_dir = project / "qa" / "retro" / retro_id
    proposals_file = retro_dir / "proposals.json"
    assert proposals_file.exists(), "proposals.json must exist after accept"
    raw = json.loads(proposals_file.read_text(encoding="utf-8"))
    entries = raw.get("proposals", raw)
    assert isinstance(entries, list) and len(entries) >= 1
    assert entries[0].get("finding_kind") is not None, "accept must rewrite finding_kind"
    assert entries[0].get("payload") is not None, "accept must rewrite payload"
    review_queue = retro_dir / "review-queue.md"
    assert review_queue.exists(), "review-queue.md must be created by accept"
