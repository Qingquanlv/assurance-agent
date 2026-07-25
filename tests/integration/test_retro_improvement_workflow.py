"""End-to-end Retro → Improvement closed-loop workflow assertions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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
from assurance_agent.workflow.issues.history import IssueHistoryIntegrityError
from tests.helpers_aa import write_aa_config

T0 = datetime(2026, 7, 25, 0, 0, 0, tzinfo=timezone.utc)


class NeverCalledInvoker:
    def invoke(self, request: AgentRequest) -> AgentResult:
        raise AssertionError(f"AgentInvoker must not be called; target={request.target}")


class RecordingInvoker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def invoke(self, request: AgentRequest) -> AgentResult:
        self.calls.append(request.target)
        raise AssertionError("agent should not run for this scenario")


class CandidateAgent:
    def __init__(
        self,
        retro_id: str,
        *,
        context: RetroContext,
        candidates: list[dict] | None = None,
        invalid: bool = False,
    ) -> None:
        self._retro_id = retro_id
        self._context = context
        self._candidates = candidates
        self._invalid = invalid
        self.calls = 0

    def invoke(self, request: AgentRequest) -> AgentResult:
        self.calls += 1
        retro_dir = request.workspace_root / "qa" / "retro" / self._retro_id
        # Sibling Retro dirs must not be present in the agent workspace.
        sibling = request.workspace_root / "qa" / "retro" / "retro-other"
        assert not sibling.exists(), "sibling Retro run must not be materialized"
        retro_dir.mkdir(parents=True, exist_ok=True)
        if self._invalid:
            (retro_dir / "proposal-candidates.json").write_text("{not-json\n", encoding="utf-8")
        else:
            document = {
                "schema_version": "2",
                "retro_id": self._retro_id,
                "context_sha256": context_sha256(self._context),
                "candidates": self._candidates
                or [
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
        (retro_dir / "retro-summary.md").write_text("# summary\n", encoding="utf-8")
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
    (project / "qa" / "changes" / "RETRO-RUN").mkdir(parents=True)
    write_aa_config(project)
    # Sibling Retro run that must never enter the agent workspace.
    other = project / "qa" / "retro" / "retro-other"
    other.mkdir(parents=True)
    (other / "context.json").write_text('{"retro_id":"retro-other"}\n', encoding="utf-8")
    return project


def _compile() -> tuple[CompiledWorkflow, object]:
    text = resources.read_text("schemas", "workflow-schema.yaml")
    contracts = load_execution_contracts(Path("."))
    return compile_workflow(parse_workflow_v2(text), contracts), contracts


def _runtime(
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


def _ctx(project: Path, retro_id: str) -> RuntimeContext:
    return RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=project / "qa" / "changes" / "RETRO-RUN",
        change_id="RETRO-RUN",
        params={
            "retro_id": retro_id,
            "retro_dry_run": False,
            "retro_last": 10,
            "retro_min_evidence": 1,
        },
    )


def _v2_context(retro_id: str, *, incomplete: bool = False) -> RetroContext:
    return RetroContext(
        retro_id=retro_id,
        generated_at="2026-07-25T00:00:00Z",
        window=RetroWindow(
            selection=RetroSelectionSnapshot(mode="change_ids", requested_change_ids=("CH-1",)),
            change_ids=("CH-1",),
        ),
        source_manifest=RetroSourceManifest(
            issue_slice_sha256="sha256:slice",
            issue_sources=(
                RetroSourceDescriptor(
                    kind="change_issue_ledger",
                    change_id="CH-1",
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


def _stub_collect(
    ctx: RetroContext,
) -> object:
    def _fn(
        task: ExecutableTask,
        workspace: TaskWorkspace,
        context: RuntimeContext,
    ) -> TaskResult:
        retro_id = str(context.params.get("retro_id", ""))
        retro_dir = workspace.project_root / "qa" / "retro" / retro_id
        retro_dir.mkdir(parents=True, exist_ok=True)
        (retro_dir / "context.json").write_text(
            json.dumps(ctx.model_dump(mode="json"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return TaskResult(
            status="succeeded",
            value={"retro_id": retro_id, "signal_count": ctx.signal_count},
        )

    return _fn


def test_zero_signal_never_invokes_agent(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _compile()
    invoker = RecordingInvoker()
    runtime = _runtime(project, compiled, contracts, invoker)
    result = runtime.run(compiled, "retro", _ctx(project, "retro-zero"))
    assert result.exit_code == 0
    assert invoker.calls == []
    assert not (project / "qa/retro/retro-zero/proposal-candidates.json").exists()


def test_corrupt_issue_ledger_fails_before_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integrity errors surface via real retro_collect → invalid_input, before aa-retro."""

    def _boom(*_a: object, **_k: object) -> object:
        raise IssueHistoryIntegrityError("corrupt Issue Ledger")

    monkeypatch.setattr(
        "assurance_agent.workflow.graph.handlers.retro_ops.run_retro_collect",
        _boom,
    )
    project = _make_project(tmp_path)
    compiled, contracts = _compile()
    invoker = RecordingInvoker()
    runtime = _runtime(project, compiled, contracts, invoker)
    result = runtime.run(compiled, "retro", _ctx(project, "retro-corrupt"))
    assert result.status.status == "failed"
    assert invoker.calls == []


def test_incomplete_issue_context_allows_process_improvements_only(tmp_path: Path) -> None:
    """Amendment 2A: incomplete Issue integrity blocks only domain_knowledge."""
    project = _make_project(tmp_path)
    retro_id = "retro-incomplete"
    compiled, contracts = _compile()
    ctx = _v2_context(retro_id, incomplete=True)
    assert ctx.allows_domain_knowledge is False
    process = {
        "candidate_id": "IMP-CAND-P",
        "kind": "workflow_improvement",
        "delivery": "change_draft",
        "source_refs": {"problem_ids": ["PROB-1"]},
        "target": "assurance_agent/workflow/inspect",
        "rationale": "Process improvement still allowed",
        "proposed_change": "Tighten inspect truncation handling",
        "verification": {
            "suites": ["workflow-full"],
            "success_criteria": "No truncation Observation",
        },
        "risk": "low",
        "confidence": "high",
    }
    invoker = CandidateAgent(retro_id, context=ctx, candidates=[process])
    runtime = _runtime(
        project,
        compiled,
        contracts,
        invoker,
        extra_ops={"operation:retro-collect": _stub_collect(ctx)},
    )
    result = runtime.run(compiled, "retro", _ctx(project, retro_id))
    assert result.exit_code == 0, result.reason
    assert invoker.calls == 1
    status = json.loads(
        (project / "qa/retro" / retro_id / "accept-status.json").read_text(encoding="utf-8")
    )
    assert status["result"] == "accepted"
    assert len(status["improvement_ids"]) == 1


def test_invalid_candidate_batch_writes_no_project_improvements(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    retro_id = "retro-invalid"
    compiled, contracts = _compile()
    ctx = _v2_context(retro_id)
    invoker = CandidateAgent(retro_id, context=ctx, invalid=True)
    runtime = _runtime(
        project,
        compiled,
        contracts,
        invoker,
        extra_ops={"operation:retro-collect": _stub_collect(ctx)},
    )
    result = runtime.run(compiled, "retro", _ctx(project, retro_id))
    assert result.status.status == "failed"
    assert not (project / "qa/improvements/events.jsonl").exists()
    # Failed receipt may still exist in the current run.
    receipt = project / "qa/retro" / retro_id / "accept-status.json"
    if receipt.is_file():
        assert json.loads(receipt.read_text(encoding="utf-8"))["result"] == "failed"


def test_successful_run_produces_five_artifacts_and_one_improvement(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    retro_id = "retro-success"
    compiled, contracts = _compile()
    ctx = _v2_context(retro_id)
    invoker = CandidateAgent(retro_id, context=ctx)
    runtime = _runtime(
        project,
        compiled,
        contracts,
        invoker,
        extra_ops={"operation:retro-collect": _stub_collect(ctx)},
    )
    result = runtime.run(compiled, "retro", _ctx(project, retro_id))
    assert result.exit_code == 0, result.reason
    retro_dir = project / "qa" / "retro" / retro_id
    for name in (
        "context.json",
        "proposal-candidates.json",
        "retro-summary.md",
        "accept-status.json",
        "review-queue.md",
    ):
        assert (retro_dir / name).is_file(), name
    status = json.loads((retro_dir / "accept-status.json").read_text(encoding="utf-8"))
    assert status["result"] == "accepted"
    assert len(status["improvement_ids"]) == 1
    assert (project / "qa/improvements/events.jsonl").is_file()
