"""Integration tests for the independent Improvement review workflow.

Validates:
  - packaged entrypoint / graph / contracts / CLI choice
  - interrupt → resume apply increments Improvement version
  - referenced Problem projection and Issue ledger bytes stay identical
  - apply writes only qa/improvements/** (never qa/issues/**)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from assurance_agent import resources
from assurance_agent.artifacts.models.improvements import ImprovementState
from assurance_agent.artifacts.models.issues import Problem, ProblemProjection
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from assurance_agent.workflow.graph.checkpoint import CheckpointStore
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import load_execution_contracts, parse_execution_contracts
from assurance_agent.workflow.graph.handlers.operation import default_operations
from assurance_agent.workflow.graph.models import CompiledWorkflow, ResumeCommand, RuntimeContext
from assurance_agent.workflow.graph.runtime import GraphRuntime
from assurance_agent.workflow.graph.scheduler import Scheduler
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2, parse_workflow_v2
from assurance_agent.workflow.graph.task_runner import build_default_node_runner
from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceBackend
from assurance_agent.workflow.improvements.events import IMPROVEMENT_EVENT_ADAPTER
from assurance_agent.workflow.improvements.ledger import ProjectImprovementStore
from assurance_agent.workflow.improvements.review import REVIEW_ACTIONS
from assurance_agent.workflow.issues.projection import dump_projection
from tests.helpers_aa import write_aa_config

T0 = datetime(2026, 7, 26, 0, 0, 0, tzinfo=timezone.utc)
IMP_ID = "IMP-ABCDEF0123456789FFFF"
PROB_ID = "PROB-abc1"


class NeverCalledInvoker:
    def invoke(self, request: AgentRequest) -> AgentResult:
        raise AssertionError(f"AgentInvoker must not be called; target={request.target}")


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


def _problem_doc() -> ProblemProjection:
    return ProblemProjection(
        schema_version="1.0",
        generated_at="2026-07-26T00:00:00Z",
        problems=[
            Problem.model_validate(
                {
                    "problem_id": PROB_ID,
                    "fingerprint": {"version": "1", "digest": "b" * 64},
                    "title": "HTTP 500 on empty department name",
                    "assessment": {
                        "classification": "product_bug",
                        "severity": "high",
                        "authority": "llm_provisional",
                    },
                    "status": "triaged",
                    "first_seen": {"change_id": "CH-1", "occurrence_id": "OCC-1"},
                    "last_seen": {"change_id": "CH-1", "occurrence_id": "OCC-1"},
                    "occurrences": ["OCC-1"],
                    "version": 2,
                }
            )
        ],
    )


def _seed_issue_ledger(project: Path) -> tuple[bytes, bytes]:
    issues = project / "qa" / "issues"
    issues.mkdir(parents=True, exist_ok=True)
    problems_bytes = dump_projection(_problem_doc())
    (issues / "problems.json").write_bytes(problems_bytes)
    events = (
        '{"schema_version":"1.0","seq":1,"event_id":"EVT-seed","idempotency_key":"IDEM-seed",'
        '"ts":"2026-07-26T00:00:00Z","type":"problem_detected","problem_id":"PROB-abc1",'
        '"expected_problem_version":0}\n'
    )
    events_bytes = events.encode("utf-8")
    (issues / "events.jsonl").write_bytes(events_bytes)
    (issues / "review-queue.json").write_text(
        '{"schema_version":"1.0","problem_ids":["PROB-abc1"]}\n', encoding="utf-8"
    )
    return problems_bytes, events_bytes


def _seed_improvement(project: Path) -> None:
    store = ProjectImprovementStore(project)
    event = IMPROVEMENT_EVENT_ADAPTER.validate_python(
        {
            "schema_version": "1.0",
            "seq": 1,
            "event_id": "IMPEVT-PROP",
            "idempotency_key": "IDEM-PROP",
            "ts": "2026-07-26T00:00:00Z",
            "improvement_id": IMP_ID,
            "expected_improvement_version": 0,
            "type": "improvement_proposed",
            "fingerprint": "a" * 64,
            "fingerprint_version": "1",
            "kind": "workflow_improvement",
            "delivery": "change_draft",
            "source_refs": {"problem_ids": [PROB_ID]},
            "target": "assurance_agent/workflow/inspect",
            "rationale": "Repeated truncation",
            "proposed_change": "Preserve pytest E lines",
            "verification": {
                "suites": ["workflow-full"],
                "success_criteria": "No truncation",
            },
            "risk": "low",
            "confidence": "high",
            "retro_id": "RETRO-1",
            "candidate_id": "IMP-CAND-1",
            "context_sha256": "c" * 64,
            "candidate_batch_digest": "d" * 64,
        }
    )
    store.append_and_rebuild([event])


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / "qa" / "changes" / "CH-REVIEW").mkdir(parents=True)
    write_aa_config(project)
    _seed_issue_ledger(project)
    _seed_improvement(project)
    return project


def _compile() -> tuple[CompiledWorkflow, object]:
    text = resources.read_text("schemas", "workflow-schema.yaml")
    contracts = load_execution_contracts(Path("."))
    return compile_workflow(parse_workflow_v2(text), contracts), contracts


def _runtime(
    project: Path,
    compiled: CompiledWorkflow,
    contracts: object,
) -> GraphRuntime:
    change_dir = project / "qa" / "changes" / "CH-REVIEW"
    store = TreeStore(change_dir)
    checkpoints = CheckpointStore(change_dir)
    workspaces = WorkspaceBackend(change_dir)
    clock = FakeClock()
    holder: dict[str, GraphRuntime] = {}

    def run_child(task, graph_id, workspace, context):  # type: ignore[no-untyped-def]
        return holder["rt"].run_child(task, graph_id, workspace, context)

    node_runner = build_default_node_runner(
        NeverCalledInvoker(),
        store,
        contracts,  # type: ignore[arg-type]
        compiled=compiled,
        run_child=run_child,
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


def _ctx(project: Path, *, review_id: str = "IREV-1") -> RuntimeContext:
    return RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=project / "qa" / "changes" / "CH-REVIEW",
        change_id="CH-REVIEW",
        params={
            "improvement_id": IMP_ID,
            "improvement_review_id": review_id,
        },
    )


# ---------------------------------------------------------------------------
# Entrypoint / graph / CLI registration
# ---------------------------------------------------------------------------


class TestImprovementReviewEntrypoint:
    def test_packaged_workflow_has_improvement_review(self) -> None:
        schema = load_workflow_v2(Path("."))
        assert "improvement-review" in schema.entrypoints
        ep = schema.entrypoints["improvement-review"]
        assert ep.restart == "repeatable"
        assert ep.allow is not None
        assert "improvement_id" in ep.allow
        assert "improvement_review_id" in ep.allow

    def test_graph_has_required_nodes_and_actions(self) -> None:
        schema = load_workflow_v2(Path("."))
        graph = schema.graphs.get("improvement-review-workflow")
        assert graph is not None
        node_names = set(graph.nodes.keys())
        for expected in ("load-improvement", "human-interrupt", "apply-review"):
            assert expected in node_names
        interrupt = graph.nodes["human-interrupt"].interrupt
        assert interrupt is not None
        declared = set(interrupt.actions)
        for action in REVIEW_ACTIONS | {"stop"}:
            assert action in declared

    def test_compiled_restart_is_repeatable(self) -> None:
        compiled, _ = _compile()
        assert compiled.entrypoints["improvement-review"].restart == "repeatable"

    def test_cli_entrypoint_choice_includes_improvement_review(self) -> None:
        from assurance_agent.commands.workflow_cmd import _ENTRYPOINT_CHOICE

        assert "improvement-review" in set(_ENTRYPOINT_CHOICE.choices)


class TestOperationRegistration:
    def test_review_operations_registered(self) -> None:
        ops = default_operations()
        assert "operation:load-improvement-review-context" in ops
        assert "operation:apply-improvement-review" in ops

    def test_contracts_declare_improvement_ledger_lock(self) -> None:
        catalog = parse_execution_contracts(resources.read_text("schemas", "execution-contracts.yaml"))
        apply = catalog.contracts["operation:apply-improvement-review"]
        assert "project:qa/improvements/**" in apply.synchronized
        assert "project:improvement-registry" in apply.exclusive
        assert "project:qa/issues/**" not in apply.writes
        load = catalog.contracts["operation:load-improvement-review-context"]
        assert "change:improvement-review/**" in load.writes


# ---------------------------------------------------------------------------
# Interrupt / resume + Issue isolation
# ---------------------------------------------------------------------------


def test_interrupt_resume_increments_version_without_touching_issues(
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path)
    issues_dir = project / "qa" / "issues"
    before_problems = (issues_dir / "problems.json").read_bytes()
    before_events = (issues_dir / "events.jsonl").read_bytes()
    before_queue = (issues_dir / "review-queue.json").read_bytes()

    improvements_before = json.loads(
        (project / "qa" / "improvements" / "improvements.json").read_text(encoding="utf-8")
    )
    assert improvements_before["improvements"][IMP_ID]["version"] == 1
    assert improvements_before["improvements"][IMP_ID]["state"] == "proposed"

    compiled, contracts = _compile()
    runtime = _runtime(project, compiled, contracts)
    result = runtime.run(compiled, "improvement-review", _ctx(project))
    assert result.status.status == "interrupted"
    assert result.status.pending_interrupts

    pending = result.status.pending_interrupts[0]
    done = runtime.resume(
        result.invocation_id,
        ResumeCommand(
            interrupt_id=pending.interrupt_id,
            action="approve",
            reason="looks good",
            who="alice",
        ),
    )
    assert done.exit_code == 0, done.reason
    assert done.status.status == "completed"

    improvements_after = json.loads(
        (project / "qa" / "improvements" / "improvements.json").read_text(encoding="utf-8")
    )
    item = improvements_after["improvements"][IMP_ID]
    assert item["version"] == 2
    assert item["state"] == ImprovementState.APPROVED.value

    assert (issues_dir / "problems.json").read_bytes() == before_problems
    assert (issues_dir / "events.jsonl").read_bytes() == before_events
    assert (issues_dir / "review-queue.json").read_bytes() == before_queue

    # Context written under change:improvement-review/**
    context_path = project / "qa" / "changes" / "CH-REVIEW" / "improvement-review" / "IREV-1" / "context.json"
    assert context_path.is_file()
    ctx_doc = json.loads(context_path.read_text(encoding="utf-8"))
    assert ctx_doc["improvement_id"] == IMP_ID
    assert "assessment" not in ctx_doc
    assert "severity" not in ctx_doc
