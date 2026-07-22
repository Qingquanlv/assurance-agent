"""Task 12：nested subgraph namespaces + audited resumable interrupts."""

from __future__ import annotations

import json
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from assurance_agent.workflow.core.events import read_events_strict
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from assurance_agent.workflow.graph.checkpoint import CheckpointStore
from assurance_agent.workflow.graph.compiler import compile_workflow, canonical_digest
from assurance_agent.workflow.graph.contracts import parse_execution_contracts
from assurance_agent.workflow.graph.handlers.operation import (
    OperationFn,
    OperationHandler,
    default_operations,
)
from assurance_agent.workflow.graph.leases import SystemClock
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    ResumeCommand,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.runtime import GraphRuntime, GraphRuntimeError
from assurance_agent.workflow.graph.scheduler import Scheduler
from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2
from assurance_agent.workflow.graph.task_runner import build_default_node_runner
from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceBackend
from tests.helpers_aa import write_aa_config

T0 = datetime(2026, 7, 20, 1, 0, 0, tzinfo=timezone.utc)

_CONTRACTS = """\
schema_version: "1"
contracts:
  operation:no-op:
    handler: operation
    side_effect_free: true
  operation:write-marker:
    handler: operation
    side_effect_free: false
    writes: ["repo:tests/api/**"]
    authorization_writes: ["repo:tests/api/**"]
    retryable_errors: [timeout, transport, internal]
  operation:write-review:
    handler: operation
    side_effect_free: false
    writes: ["change:review/**"]
    authorization_writes: ["change:review/**"]
    retryable_errors: []
  operation:write-safety:
    handler: operation
    side_effect_free: false
    writes: ["change:healing/**"]
    authorization_writes: ["change:healing/**"]
    retryable_errors: []
  operation:write-child-a:
    handler: operation
    side_effect_free: false
    writes: ["change:branch-a/**"]
    authorization_writes: ["change:branch-a/**"]
    retryable_errors: []
  operation:write-child-b:
    handler: operation
    side_effect_free: false
    writes: ["change:branch-b/**"]
    authorization_writes: ["change:branch-b/**"]
    retryable_errors: []
  operation:rerun:
    handler: operation
    side_effect_free: true
  operation:proposal:
    handler: operation
    side_effect_free: true
  builtin:gate:
    handler: builtin
    side_effect_free: true
  builtin:interrupt:
    handler: builtin
    side_effect_free: true
"""

_GATE_FOOTER = """
gates:
  case-review-gate:
    reads: [review/case-review.json]
    invalid_json: stop
    missing_field_is: stop
    needs_human_review_when: "case_review.decision == 'needs_human_review'"
    reject_when: "case_review.decision == 'reject'"
    pass_when: "case_review.decision == 'pass'"
  fixer-safety-gate:
    reads: [healing/fixer-safety-check.json]
    invalid_json: stop
    missing_field_is: stop
    needs_human_review_when: "fixer_safety_check.decision == 'needs_human_review'"
    pass_when: "fixer_safety_check.decision == 'pass'"
"""


class NeverCalledInvoker:
    def invoke(self, request: AgentRequest) -> AgentResult:
        raise AssertionError(f"AgentInvoker must not be called: {request}")


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self._now = start
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    (project / "tests" / "api").mkdir(parents=True)
    write_aa_config(project)
    return project


def _context(project: Path, *, params: dict[str, object] | None = None) -> RuntimeContext:
    change = project / "qa" / "changes" / "CH-1"
    return RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=change,
        change_id="CH-1",
        params=params or {"run_mode": "full"},
    )


def _compile(body: str) -> tuple[CompiledWorkflow, object]:
    text = (
        'schema_version: "2"\nname: t\n'
        "params:\n  run_mode: {type: enum, values: [full], default: full}\n"
        "entrypoints:\n  full: {graph: main, allow: \"params.run_mode == 'full'\"}\n"
        "policies:\n"
        "  retry:\n    never: {max_attempts: 1, retry_on: []}\n"
        "  timeout:\n    local: {run_seconds: 60, heartbeat_seconds: 0.05}\n"
        "  scheduler: {max_parallel_tasks: 4}\n"
        "graphs:\n" + textwrap.indent(textwrap.dedent(body), "  ") + _GATE_FOOTER
    )
    contracts = parse_execution_contracts(_CONTRACTS)
    return compile_workflow(parse_workflow_v2(text), contracts), contracts


def _ops() -> dict[str, OperationFn]:
    ops = default_operations()

    def write_marker(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        path = workspace.project_root / "tests" / "api" / "marker.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("marker\n", encoding="utf-8")
        return TaskResult(status="succeeded")

    def write_review(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        path = workspace.change_dir / "review" / "case-review.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"decision": "needs_human_review"}),
            encoding="utf-8",
        )
        return TaskResult(status="succeeded")

    def write_child_a(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        path = workspace.change_dir / "branch-a" / "out.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("a\n", encoding="utf-8")
        return TaskResult(status="succeeded")

    def write_child_b(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        path = workspace.change_dir / "branch-b" / "out.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("b\n", encoding="utf-8")
        return TaskResult(status="succeeded")

    def write_safety(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        path = workspace.change_dir / "healing" / "fixer-safety-check.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"decision": "needs_human_review"}),
            encoding="utf-8",
        )
        return TaskResult(status="succeeded")

    ops["operation:write-marker"] = write_marker
    ops["operation:write-review"] = write_review
    ops["operation:write-child-a"] = write_child_a
    ops["operation:write-child-b"] = write_child_b
    ops["operation:write-safety"] = write_safety
    ops["operation:rerun"] = default_operations()["operation:no-op"]
    ops["operation:proposal"] = default_operations()["operation:no-op"]
    return ops


def _build_runtime(
    project: Path,
    compiled: CompiledWorkflow,
    contracts,
    *,
    clock=None,
    ops: dict[str, OperationFn] | None = None,
) -> GraphRuntime:
    change = project / "qa" / "changes" / "CH-1"
    store = TreeStore(change)
    checkpoints = CheckpointStore(change)
    workspaces = WorkspaceBackend(change)
    clock = clock or SystemClock()
    holder: dict[str, GraphRuntime] = {}

    def run_child(task, graph_id, workspace, context):  # type: ignore[no-untyped-def]
        return holder["rt"].run_child(task, graph_id, workspace, context)

    base = build_default_node_runner(
        NeverCalledInvoker(), store, contracts, compiled=compiled, run_child=run_child
    )
    if ops is not None:
        op_handler = OperationHandler(ops)

        # Wrap: keep builtins/subgraph from base, override operations.
        class Combined:
            def execute(self, task, workspace, context):  # type: ignore[no-untyped-def]
                if task.target.startswith("operation:"):
                    return op_handler.execute(task, workspace, context)
                return base.execute(task, workspace, context)

        node_runner = Combined()
    else:
        node_runner = base

    graph_id = compiled.entrypoints["full"].graph_id
    state_defs = dict(compiled.schema.graphs[graph_id].state)
    # Child graphs may also have state; union for scheduler reducers.
    for gid, graph in compiled.schema.graphs.items():
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


# ---------------------------------------------------------------------------
# Step 2：nested completion + distinct namespaces
# ---------------------------------------------------------------------------


_PARALLEL_SUBGRAPHS = """
main:
  max_supersteps: 8
  nodes:
    left: {uses: graph:child, retry: never, timeout: local}
    right: {uses: graph:child, retry: never, timeout: local}
  edges:
    - {from: START, to: left}
    - {from: START, to: right}
    - {from: left, to: END}
    - {from: right, to: END}
child:
  max_supersteps: 5
  nodes:
    work:
      uses: operation:write-child-a
      outputs: [change:branch-a/out.txt]
      retry: never
      timeout: local
  edges:
    - {from: START, to: work}
    - {from: work, to: END}
"""


def test_parallel_subgraphs_get_distinct_namespaces(tmp_path: Path) -> None:
    # right child also writes branch-b so footprints don't collide on same path.
    body = _PARALLEL_SUBGRAPHS.replace(
        "operation:write-child-a",
        "operation:write-child-a",
        1,
    )
    # Specialize right's child graph.
    body = """
main:
  max_supersteps: 8
  nodes:
    left:
      uses: graph:child_a
      outputs: [change:branch-a/out.txt]
      retry: never
      timeout: local
    right:
      uses: graph:child_b
      outputs: [change:branch-b/out.txt]
      retry: never
      timeout: local
  edges:
    - {from: START, to: left}
    - {from: START, to: right}
    - {from: left, to: END}
    - {from: right, to: END}
child_a:
  max_supersteps: 5
  nodes:
    work:
      uses: operation:write-child-a
      outputs: [change:branch-a/out.txt]
      retry: never
      timeout: local
  edges:
    - {from: START, to: work}
    - {from: work, to: END}
child_b:
  max_supersteps: 5
  nodes:
    work:
      uses: operation:write-child-b
      outputs: [change:branch-b/out.txt]
      retry: never
      timeout: local
  edges:
    - {from: START, to: work}
    - {from: work, to: END}
"""
    project = _make_project(tmp_path)
    compiled, contracts = _compile(body)
    runtime = _build_runtime(project, compiled, contracts, ops=_ops())
    result = runtime.run(compiled, "full", _context(project))
    assert result.exit_code == 0

    change = _context(project).change_dir
    started = [e for e in read_events_strict(change) if e.get("type") == "graph_invocation_started"]
    root = next(e for e in started if e.get("parent_invocation_id") is None)
    children = [e for e in started if e.get("parent_invocation_id") == root["invocation_id"]]
    assert len(children) == 2
    namespaces = {e["checkpoint_ns"] for e in children}
    assert len(namespaces) == 2
    for child in children:
        ns = child["checkpoint_ns"]
        assert isinstance(ns, str)
        assert ns.startswith(f"{root['invocation_id']}/")
        assert child["parent_task_id"]
        # Deterministic child id from parent task + graph.
        expected = canonical_digest(
            {
                "parent_task_id": str(child["parent_task_id"]),
                "graph_id": str(child["graph_id"]),
            }
        )
        assert child["invocation_id"] == expected

    # Fresh runtime projects both children.
    fresh = _build_runtime(project, compiled, contracts, ops=_ops())
    for child in children:
        proj = fresh._checkpoints.project(str(child["invocation_id"]))  # noqa: SLF001
        assert proj.terminal == "completed"
        assert proj.parent_invocation_id == root["invocation_id"]

    assert (project / "qa" / "changes" / "CH-1" / "branch-a" / "out.txt").read_text() == "a\n"
    assert (project / "qa" / "changes" / "CH-1" / "branch-b" / "out.txt").read_text() == "b\n"


# ---------------------------------------------------------------------------
# Step 4：interrupt publication + parallel nested interrupts
# ---------------------------------------------------------------------------


_REVIEW_INTERRUPT = """
main:
  max_supersteps: 8
  nodes:
    review_branch:
      uses: graph:review_child
      retry: never
      timeout: local
  edges:
    - {from: START, to: review_branch}
    - {from: review_branch, to: END}
review_child:
  max_supersteps: 5
  nodes:
    seed:
      uses: operation:write-review
      outputs: [change:review/case-review.json]
      retry: never
      timeout: local
    human:
      uses: builtin:interrupt
      interrupt:
        reason: needs a human
        checkpoint: case-review-gate
        bind: audited_gate_read
        actions: [fix_and_proceed, accept_risk, stop]
      retry: never
      timeout: local
  edges:
    - {from: START, to: seed}
    - {from: seed, to: human}
  routes:
    - from: human
      select: "resume.action"
      cases:
        fix_and_proceed: END
        accept_risk: END
        stop: STOP
      default: STOP
"""


def test_interrupt_publishes_view_and_pending_sibling_write_set(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _compile(_REVIEW_INTERRUPT)
    runtime = _build_runtime(project, compiled, contracts, ops=_ops())
    result = runtime.run(compiled, "full", _context(project))
    assert result.exit_code == 30
    assert result.status.status == "interrupted"
    assert len(result.status.pending_interrupts) == 1
    interrupt = result.status.pending_interrupts[0]
    assert interrupt.actions == ("fix_and_proceed", "accept_risk", "stop")
    assert "review/case-review.json" in interrupt.audited_reads_sha256
    assert interrupt.artifact_view
    # Nested child wrote review but parent did not flatten/commit a write-set.
    assert result.status.pending_write_sets == ()
    assert interrupt.checkpoint_ns != result.invocation_id

    change = _context(project).change_dir
    events = read_events_strict(change)
    interrupted = [e for e in events if e.get("type") == "graph_interrupted"]
    assert len(interrupted) >= 1
    root_interrupted = [e for e in interrupted if e.get("invocation_id") == result.invocation_id]
    assert len(root_interrupted) == 1
    assert root_interrupted[0]["audited_reads_sha256"] == interrupt.audited_reads_sha256
    assert root_interrupted[0]["artifact_view"] == interrupt.artifact_view

    view_file = change / interrupt.artifact_view / "review" / "case-review.json"
    assert view_file.is_file()
    assert json.loads(view_file.read_text(encoding="utf-8"))["decision"] == "needs_human_review"
    assert not (view_file.stat().st_mode & 0o222)
    # Child wrote review into its private lineage; parent canonical workspace has no commit.
    assert not (change / "review" / "case-review.json").exists()


_PARALLEL_INTERRUPT_SUBGRAPHS = """
main:
  max_supersteps: 8
  nodes:
    left:
      uses: graph:branch_left
      retry: never
      timeout: local
    right:
      uses: graph:branch_right
      retry: never
      timeout: local
  edges:
    - {from: START, to: left}
    - {from: START, to: right}
    - {from: left, to: END}
    - {from: right, to: END}
branch_left:
  max_supersteps: 5
  nodes:
    human:
      uses: builtin:interrupt
      interrupt:
        reason: needs a human
        checkpoint: case-review-gate
        bind: audited_gate_read
        actions: [fix_and_proceed, accept_risk, stop]
      retry: never
      timeout: local
  edges:
    - {from: START, to: human}
  routes:
    - from: human
      select: "resume.action"
      cases:
        fix_and_proceed: END
        accept_risk: END
        stop: STOP
      default: STOP
branch_right:
  max_supersteps: 5
  nodes:
    human:
      uses: builtin:interrupt
      interrupt:
        reason: needs a human
        checkpoint: case-review-gate
        bind: audited_gate_read
        actions: [fix_and_proceed, accept_risk, stop]
      retry: never
      timeout: local
  edges:
    - {from: START, to: human}
  routes:
    - from: human
      select: "resume.action"
      cases:
        fix_and_proceed: END
        accept_risk: END
        stop: STOP
      default: STOP
"""


def test_parallel_subgraph_interrupts_both_pending(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    change = project / "qa" / "changes" / "CH-1"
    review = change / "review"
    review.mkdir(parents=True)
    (review / "case-review.json").write_text(json.dumps({"decision": "needs_human_review"}), encoding="utf-8")
    compiled, contracts = _compile(_PARALLEL_INTERRUPT_SUBGRAPHS)
    runtime = _build_runtime(project, compiled, contracts, ops=_ops())
    result = runtime.run(compiled, "full", _context(project))
    assert result.exit_code == 30
    ids = {i.interrupt_id for i in result.status.pending_interrupts}
    assert len(ids) == 2
    namespaces = {i.checkpoint_ns for i in result.status.pending_interrupts}
    assert len(namespaces) == 2

    first = result.status.pending_interrupts[0]
    resumed = runtime.resume(
        result.invocation_id,
        ResumeCommand(
            interrupt_id=first.interrupt_id,
            action="accept_risk",
            reason="ack one branch",
            who="tester",
        ),
    )
    assert resumed.exit_code == 30
    pending = {i.interrupt_id for i in resumed.status.pending_interrupts}
    assert first.interrupt_id not in pending
    assert len(pending) == 1


# ---------------------------------------------------------------------------
# Step 6：audited resume routing
# ---------------------------------------------------------------------------


_HEALING_SAFETY = """
main:
  max_supersteps: 10
  nodes:
    seed:
      uses: operation:write-safety
      outputs: [change:healing/fixer-safety-check.json]
      retry: never
      timeout: local
    safety:
      uses: builtin:gate
      with: {gate: fixer-safety-gate}
      retry: never
      timeout: local
    safety-interrupt:
      uses: builtin:interrupt
      interrupt:
        reason: fixer safety
        checkpoint: healing.safety
        bind: audited_gate_read
        actions: [fix_and_proceed, accept_risk, stop]
      retry: never
      timeout: local
    rerun:
      uses: operation:rerun
      retry: never
      timeout: local
    proposal:
      uses: operation:proposal
      retry: never
      timeout: local
  edges:
    - {from: START, to: seed}
    - {from: seed, to: safety}
    - {from: rerun, to: END}
    - {from: proposal, to: END}
  routes:
    - from: safety
      select: "node('safety').gate.verdict"
      cases:
        pass: rerun
        needs_human_review: safety-interrupt
      default: STOP
    - from: safety-interrupt
      select: "resume.action"
      cases:
        accept_risk: rerun
        fix_and_proceed: proposal
        stop: STOP
      default: STOP
"""


def test_accept_risk_at_healing_safety_routes_to_rerun(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _compile(_HEALING_SAFETY)
    ops = _ops()
    calls = {"rerun": 0, "proposal": 0}
    base_rerun = ops["operation:rerun"]
    base_proposal = ops["operation:proposal"]

    def rerun(task, workspace, context):  # type: ignore[no-untyped-def]
        calls["rerun"] += 1
        return base_rerun(task, workspace, context)

    def proposal(task, workspace, context):  # type: ignore[no-untyped-def]
        calls["proposal"] += 1
        return base_proposal(task, workspace, context)

    ops["operation:rerun"] = rerun
    ops["operation:proposal"] = proposal
    runtime = _build_runtime(project, compiled, contracts, ops=ops)
    result = runtime.run(compiled, "full", _context(project))
    assert result.exit_code == 30
    interrupt = result.status.pending_interrupts[0]
    assert interrupt.checkpoint == "healing.safety"

    done = runtime.resume(
        result.invocation_id,
        ResumeCommand(
            interrupt_id=interrupt.interrupt_id,
            action="accept_risk",
            reason="accept residual risk",
            who="reviewer",
        ),
    )
    assert done.exit_code == 0
    assert calls["rerun"] == 1
    assert calls["proposal"] == 0
    events = read_events_strict(_context(project).change_dir)
    assert any(e.get("type") == "graph_resumed" and e.get("action") == "accept_risk" for e in events)


def test_fix_and_proceed_routes_to_proposal(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _compile(_HEALING_SAFETY)
    ops = _ops()
    calls = {"rerun": 0, "proposal": 0}
    base_rerun = ops["operation:rerun"]
    base_proposal = ops["operation:proposal"]

    def rerun(task, workspace, context):  # type: ignore[no-untyped-def]
        calls["rerun"] += 1
        return base_rerun(task, workspace, context)

    def proposal(task, workspace, context):  # type: ignore[no-untyped-def]
        calls["proposal"] += 1
        return base_proposal(task, workspace, context)

    ops["operation:rerun"] = rerun
    ops["operation:proposal"] = proposal
    runtime = _build_runtime(project, compiled, contracts, ops=ops)
    result = runtime.run(compiled, "full", _context(project))
    interrupt = result.status.pending_interrupts[0]
    done = runtime.resume(
        result.invocation_id,
        ResumeCommand(
            interrupt_id=interrupt.interrupt_id,
            action="fix_and_proceed",
            reason="send to fixer",
            who="reviewer",
        ),
    )
    assert done.exit_code == 0
    assert calls["proposal"] == 1
    assert calls["rerun"] == 0


def test_stop_resume_exits_20(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _compile(_HEALING_SAFETY)
    runtime = _build_runtime(project, compiled, contracts, ops=_ops())
    result = runtime.run(compiled, "full", _context(project))
    interrupt = result.status.pending_interrupts[0]
    stopped = runtime.resume(
        result.invocation_id,
        ResumeCommand(
            interrupt_id=interrupt.interrupt_id,
            action="stop",
            reason="halt",
            who="reviewer",
        ),
    )
    assert stopped.exit_code == 20
    assert stopped.status.status == "stopped"


def test_stale_hash_rejects_without_graph_resumed(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _compile(_HEALING_SAFETY)
    runtime = _build_runtime(project, compiled, contracts, ops=_ops())
    result = runtime.run(compiled, "full", _context(project))
    interrupt = result.status.pending_interrupts[0]
    assert interrupt.artifact_view
    view = _context(project).change_dir / interrupt.artifact_view / "healing" / "fixer-safety-check.json"
    # Temporarily make writable to tamper, then restore.
    view.chmod(0o644)
    view.write_text(json.dumps({"decision": "tampered"}), encoding="utf-8")
    view.chmod(0o444)

    with pytest.raises(GraphRuntimeError, match="audited read drift"):
        runtime.resume(
            result.invocation_id,
            ResumeCommand(
                interrupt_id=interrupt.interrupt_id,
                action="accept_risk",
                reason="stale",
                who="reviewer",
            ),
        )
    events = read_events_strict(_context(project).change_dir)
    assert not any(e.get("type") == "graph_resumed" for e in events)


def test_resume_skips_successful_sibling_and_completed_child(tmp_path: Path) -> None:
    body = """
main:
  max_supersteps: 10
  nodes:
    ok:
      uses: graph:ok_child
      outputs: [change:branch-a/out.txt]
      retry: never
      timeout: local
    bad:
      uses: graph:bad_child
      retry: never
      timeout: local
  edges:
    - {from: START, to: ok}
    - {from: START, to: bad}
    - {from: ok, to: END}
    - {from: bad, to: END}
ok_child:
  max_supersteps: 5
  nodes:
    work:
      uses: operation:write-child-a
      outputs: [change:branch-a/out.txt]
      retry: never
      timeout: local
  edges:
    - {from: START, to: work}
    - {from: work, to: END}
bad_child:
  max_supersteps: 5
  nodes:
    seed:
      uses: operation:write-review
      outputs: [change:review/case-review.json]
      retry: never
      timeout: local
    human:
      uses: builtin:interrupt
      interrupt:
        reason: needs a human
        checkpoint: case-review-gate
        bind: audited_gate_read
        actions: [fix_and_proceed, accept_risk, stop]
      retry: never
      timeout: local
  edges:
    - {from: START, to: seed}
    - {from: seed, to: human}
  routes:
    - from: human
      select: "resume.action"
      cases:
        fix_and_proceed: END
        accept_risk: END
        stop: STOP
      default: STOP
"""
    project = _make_project(tmp_path)
    compiled, contracts = _compile(body)
    ops = _ops()
    calls = {"write-child-a": 0, "write-review": 0}
    base_a = ops["operation:write-child-a"]
    base_review = ops["operation:write-review"]

    def count_a(task, workspace, context):  # type: ignore[no-untyped-def]
        calls["write-child-a"] += 1
        return base_a(task, workspace, context)

    def count_review(task, workspace, context):  # type: ignore[no-untyped-def]
        calls["write-review"] += 1
        return base_review(task, workspace, context)

    ops["operation:write-child-a"] = count_a
    ops["operation:write-review"] = count_review
    runtime = _build_runtime(project, compiled, contracts, ops=ops)
    result = runtime.run(compiled, "full", _context(project))
    assert result.exit_code == 30
    assert calls["write-child-a"] == 1
    assert calls["write-review"] == 1
    interrupt = result.status.pending_interrupts[0]

    done = runtime.resume(
        result.invocation_id,
        ResumeCommand(
            interrupt_id=interrupt.interrupt_id,
            action="accept_risk",
            reason="continue bad branch",
            who="reviewer",
        ),
    )
    assert done.exit_code == 0
    # Successful sibling child work and completed seed must not re-run.
    assert calls["write-child-a"] == 1
    assert calls["write-review"] == 1


# ---------------------------------------------------------------------------
# Three-level nest: root → mid → leaf interrupt (intake → case-review-cycle)
# ---------------------------------------------------------------------------

_THREE_LEVEL_INTERRUPT = """
main:
  max_supersteps: 12
  nodes:
    mid:
      uses: graph:mid
      retry: never
      timeout: local
  edges:
    - {from: START, to: mid}
    - {from: mid, to: END}
mid:
  max_supersteps: 10
  nodes:
    leaf:
      uses: graph:leaf
      retry: never
      timeout: local
  edges:
    - {from: START, to: leaf}
    - {from: leaf, to: END}
leaf:
  max_supersteps: 6
  nodes:
    seed:
      uses: operation:write-review
      outputs: [change:review/case-review.json]
      retry: never
      timeout: local
    human:
      uses: builtin:interrupt
      interrupt:
        reason: needs a human
        checkpoint: case-review-gate
        bind: audited_gate_read
        actions: [fix_and_proceed, accept_risk, stop]
      retry: never
      timeout: local
  edges:
    - {from: START, to: seed}
    - {from: seed, to: human}
  routes:
    - from: human
      select: "resume.action"
      cases:
        fix_and_proceed: END
        accept_risk: END
        stop: STOP
      default: STOP
"""


def test_accept_risk_through_three_level_nest_completes(tmp_path: Path) -> None:
    """accept_risk must resolve interrupts on every ancestor invocation along ns.

    Regression: only root+leaf got graph_resumed, so the mid invocation still
    saw a pending interrupt and immediately re-bubbled the same gate.
    """
    project = _make_project(tmp_path)
    compiled, contracts = _compile(_THREE_LEVEL_INTERRUPT)
    runtime = _build_runtime(project, compiled, contracts, ops=_ops())
    result = runtime.run(compiled, "full", _context(project))
    assert result.exit_code == 30
    assert result.status.status == "interrupted"
    interrupt = result.status.pending_interrupts[0]
    # ns: root/mid/<mid-inv>/leaf/<leaf-inv> — three invocation ids
    assert interrupt.checkpoint_ns.count("/") >= 4

    done = runtime.resume(
        result.invocation_id,
        ResumeCommand(
            interrupt_id=interrupt.interrupt_id,
            action="accept_risk",
            reason="accept through mid layer",
            who="reviewer",
        ),
    )
    assert done.exit_code == 0, done.reason
    assert done.status.status == "completed"
    assert done.status.pending_interrupts == ()

    events = read_events_strict(_context(project).change_dir)
    resumed = [e for e in events if e.get("type") == "graph_resumed"]
    resumed_invs = {e.get("invocation_id") for e in resumed}
    # Every invocation id along the interrupt ns must receive graph_resumed.
    ns_parts = interrupt.checkpoint_ns.split("/")
    expected_invs = {ns_parts[i] for i in range(0, len(ns_parts), 2)}
    assert expected_invs <= resumed_invs
