"""GraphRuntime end-to-end：最小 run/status 与 crash/recovery 边界。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from assurance_agent.workflow.core.events import read_events_strict
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from assurance_agent.workflow.graph.checkpoint import CheckpointStore
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import load_execution_contracts, parse_execution_contracts
from assurance_agent.workflow.graph.handlers.operation import (
    OperationFn,
    OperationHandler,
    default_operations,
)
from assurance_agent.workflow.graph.leases import SystemClock
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.runtime import GraphRuntime
from assurance_agent.workflow.graph.scheduler import Scheduler
from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2
from assurance_agent.workflow.graph.task_runner import HandlerNodeRunner, build_default_node_runner
from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceBackend
from tests.helpers_aa import write_aa_config

T0 = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)

_WRITE_CONTRACTS = """\
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
"""

_WRITE_WORKFLOW = """\
schema_version: "2"
name: write-min
params:
  run_mode: {type: enum, values: [full], default: full}
entrypoints:
  full: {graph: main, allow: "params.run_mode == 'full'"}
policies:
  retry:
    never: {max_attempts: 1, retry_on: []}
    flaky:
      max_attempts: 3
      retry_on: [timeout, transport, internal]
      backoff: {initial_seconds: 0.01, multiplier: 1.0, max_seconds: 0.01, jitter: false}
  timeout:
    local: {run_seconds: 60, heartbeat_seconds: 0.05}
  scheduler: {max_parallel_tasks: 2}
graphs:
  main:
    max_supersteps: 5
    nodes:
      first:
        uses: operation:write-marker
        outputs: ["repo:tests/api/marker.py"]
        retry: flaky
        timeout: local
    edges:
      - {from: START, to: first}
      - {from: first, to: END}
gates: {}
"""


class NeverCalledInvoker:
    def invoke(self, request: AgentRequest) -> AgentResult:
        raise AssertionError(f"AgentInvoker must not be called for operation targets: {request}")


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


class _InjectedCrash(RuntimeError):
    pass


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


def _minimal_compiled() -> tuple[CompiledWorkflow, object]:
    text = Path("tests/fixtures/workflow-v2-minimal.yaml").read_text(encoding="utf-8")
    contracts = load_execution_contracts(Path("."))
    compiled = compile_workflow(parse_workflow_v2(text), contracts)
    return compiled, contracts


def _write_compiled() -> tuple[CompiledWorkflow, object]:
    contracts = parse_execution_contracts(_WRITE_CONTRACTS)
    compiled = compile_workflow(parse_workflow_v2(_WRITE_WORKFLOW), contracts)
    return compiled, contracts


def _write_ops() -> dict[str, OperationFn]:
    ops = default_operations()

    def write_marker(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        path = workspace.project_root / "tests" / "api" / "marker.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("marker\n", encoding="utf-8")
        return TaskResult(status="succeeded")

    ops["operation:write-marker"] = write_marker
    return ops


def _op_runner(ops: dict[str, OperationFn]) -> HandlerNodeRunner:
    handler = OperationHandler(ops)
    return HandlerNodeRunner({target: handler for target in ops})


def _build_runtime(
    project: Path,
    compiled: CompiledWorkflow,
    contracts,
    *,
    clock=None,
    node_runner=None,
) -> GraphRuntime:
    change = project / "qa" / "changes" / "CH-1"
    store = TreeStore(change)
    checkpoints = CheckpointStore(change)
    workspaces = WorkspaceBackend(change)
    clock = clock or SystemClock()
    if node_runner is None:
        node_runner = build_default_node_runner(
            NeverCalledInvoker(), store, contracts, compiled=compiled
        )
    graph_id = compiled.entrypoints["full"].graph_id
    state_defs = dict(compiled.schema.graphs[graph_id].state)
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
    return GraphRuntime(
        checkpoint_store=checkpoints,
        object_store=store,
        workspace_backend=workspaces,
        contracts=contracts,
        node_runner=node_runner,
        scheduler=scheduler,
        schema_resolver=lambda digest: schemas[digest],
        clock=clock,
    )


def test_minimal_graph_run_and_fresh_status(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _minimal_compiled()
    runtime = _build_runtime(project, compiled, contracts)
    context = _context(project)

    result = runtime.run(compiled, "full", context)
    assert result.exit_code == 0
    assert result.status.status == "completed"
    events = read_events_strict(context.change_dir)
    assert [event["type"] for event in events].count("task_attempt_started") == 1
    assert [event["type"] for event in events].count("task_attempt_succeeded") == 1
    assert events[-1]["type"] == "graph_completed"

    fresh_runtime = _build_runtime(project, compiled, contracts)
    assert fresh_runtime.status(result.invocation_id).model_dump() == result.status.model_dump()


def test_crash_after_attempt_started_abandons_and_retries(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _write_compiled()
    clock = FakeClock()
    ops = _write_ops()
    runtime = _build_runtime(project, compiled, contracts, clock=clock, node_runner=_op_runner(ops))

    def crash_run(prepared, plan, projection, context, leases):  # type: ignore[no-untyped-def]
        raise _InjectedCrash("after task_attempt_started")

    runtime._scheduler._run_attempt = crash_run  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(_InjectedCrash):
        runtime.run(compiled, "full", _context(project))

    change = _context(project).change_dir
    events = read_events_strict(change)
    assert any(e.get("type") == "task_attempt_started" for e in events)
    assert not any(e.get("type") == "task_attempt_succeeded" for e in events)
    invocation_id = next(
        str(e["invocation_id"])
        for e in events
        if e.get("type") == "graph_invocation_started"
    )

    clock.advance(3600)
    fresh = _build_runtime(project, compiled, contracts, clock=clock, node_runner=_op_runner(ops))
    result = fresh.resume(invocation_id)
    assert result.exit_code == 0
    events = read_events_strict(change)
    assert any(e.get("type") == "task_attempt_abandoned" for e in events)
    assert [e.get("type") for e in events].count("task_attempt_started") == 2
    assert [e.get("type") for e in events].count("task_attempt_succeeded") == 1


def test_crash_after_success_retries_update_only(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _write_compiled()
    ops = _write_ops()
    calls = {"n": 0}
    base = OperationHandler(ops)

    class CountingHandler(OperationHandler):
        def execute(self, task, workspace, context):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            return base.execute(task, workspace, context)

    counting = CountingHandler(ops)
    runner = HandlerNodeRunner({target: counting for target in ops})
    runtime = _build_runtime(project, compiled, contracts, node_runner=runner)

    def crash_commit(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise _InjectedCrash("after task_attempt_succeeded")

    runtime._scheduler._commit_wave = crash_commit  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(_InjectedCrash):
        runtime.run(compiled, "full", _context(project))

    change = _context(project).change_dir
    events = read_events_strict(change)
    assert any(e.get("type") == "task_attempt_succeeded" for e in events)
    assert not any(e.get("type") == "superstep_committed" for e in events)
    assert calls["n"] == 1
    invocation_id = next(
        str(e["invocation_id"])
        for e in events
        if e.get("type") == "graph_invocation_started"
    )

    fresh_calls = {"n": 0}

    class FreshCounting(OperationHandler):
        def execute(self, task, workspace, context):  # type: ignore[no-untyped-def]
            fresh_calls["n"] += 1
            return base.execute(task, workspace, context)

    fresh_handler = FreshCounting(ops)
    fresh = _build_runtime(
        project,
        compiled,
        contracts,
        node_runner=HandlerNodeRunner({target: fresh_handler for target in ops}),
    )
    result = fresh.resume(invocation_id)
    assert result.exit_code == 0
    assert fresh_calls["n"] == 0  # Update-only：succeeded sibling 绕过 handler
    events = read_events_strict(change)
    assert any(e.get("type") == "superstep_committed" for e in events)
    assert [e.get("type") for e in events].count("task_attempt_succeeded") == 1
    assert (project / "tests" / "api" / "marker.py").read_text(encoding="utf-8") == "marker\n"


def test_crash_during_materialization_repairs_without_reexec(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _write_compiled()
    ops = _write_ops()
    calls = {"n": 0}
    base = OperationHandler(ops)

    class CountingHandler(OperationHandler):
        def execute(self, task, workspace, context):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            return base.execute(task, workspace, context)

    counting = CountingHandler(ops)
    runtime = _build_runtime(
        project,
        compiled,
        contracts,
        node_runner=HandlerNodeRunner({target: counting for target in ops}),
    )
    store = runtime._objects  # noqa: SLF001
    original_apply = store.apply_tree
    applies = {"n": 0}

    def fail_first_apply(*args, **kwargs):  # type: ignore[no-untyped-def]
        applies["n"] += 1
        if applies["n"] == 1:
            raise _InjectedCrash("during materialization")
        return original_apply(*args, **kwargs)

    store.apply_tree = fail_first_apply  # type: ignore[method-assign]
    with pytest.raises(_InjectedCrash):
        runtime.run(compiled, "full", _context(project))

    change = _context(project).change_dir
    events = read_events_strict(change)
    assert any(e.get("type") == "superstep_committed" for e in events)
    assert calls["n"] == 1
    invocation_id = next(
        str(e["invocation_id"])
        for e in events
        if e.get("type") == "graph_invocation_started"
    )

    fresh_calls = {"n": 0}

    class FreshCounting(OperationHandler):
        def execute(self, task, workspace, context):  # type: ignore[no-untyped-def]
            fresh_calls["n"] += 1
            return base.execute(task, workspace, context)

    fresh_handler = FreshCounting(ops)
    fresh = _build_runtime(
        project,
        compiled,
        contracts,
        node_runner=HandlerNodeRunner({target: fresh_handler for target in ops}),
    )
    result = fresh.resume(invocation_id)
    assert result.exit_code == 0
    assert fresh_calls["n"] == 0
    assert [e.get("type") for e in read_events_strict(change)].count("task_attempt_succeeded") == 1
    assert (project / "tests" / "api" / "marker.py").read_text(encoding="utf-8") == "marker\n"


def test_crash_after_write_set_freeze_before_success_retries_attempt(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _write_compiled()
    ops = _write_ops()
    clock = FakeClock()
    runtime = _build_runtime(
        project, compiled, contracts, clock=clock, node_runner=_op_runner(ops)
    )
    store = runtime._objects  # noqa: SLF001
    original_freeze = store.freeze_write_set

    def crash_freeze(*args, **kwargs):  # type: ignore[no-untyped-def]
        original_freeze(*args, **kwargs)
        raise _InjectedCrash("after write-set freeze")

    store.freeze_write_set = crash_freeze  # type: ignore[method-assign]
    with pytest.raises(_InjectedCrash):
        runtime.run(compiled, "full", _context(project))

    change = _context(project).change_dir
    events = read_events_strict(change)
    assert any(e.get("type") == "task_attempt_started" for e in events)
    assert not any(e.get("type") == "task_attempt_succeeded" for e in events)
    invocation_id = next(
        str(e["invocation_id"])
        for e in events
        if e.get("type") == "graph_invocation_started"
    )

    clock.advance(3600)
    store.freeze_write_set = original_freeze  # type: ignore[method-assign]
    fresh = _build_runtime(project, compiled, contracts, clock=clock, node_runner=_op_runner(ops))
    result = fresh.resume(invocation_id)
    assert result.exit_code == 0
    events = read_events_strict(change)
    assert any(e.get("type") == "task_attempt_abandoned" for e in events)
    assert [e.get("type") for e in events].count("task_attempt_succeeded") == 1
