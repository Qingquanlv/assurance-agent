"""GraphRuntime end-to-end：最小 run/status 与 crash/recovery 边界。"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from assurance_agent.workflow.core.events import read_events_strict
from assurance_agent.workflow.driver.operations_catalog import default_operations
from assurance_agent.workflow.driver.runtime_factory import assemble_graph_runtime
from assurance_agent.workflow.graph.leases import LeaseRegistry, SystemClock, new_lease
from assurance_agent.workflow.graph.agent_api import AgentRequest, AgentResult
from assurance_agent.workflow.graph.compiler import compile_workflow
from assurance_agent.workflow.graph.contracts import (
    ExecutionContractCatalog,
    load_execution_contracts,
    parse_execution_contracts,
)
from assurance_agent.workflow.graph.handlers.operation import (
    OperationFn,
    OperationHandler,
)
from assurance_agent.workflow.graph.models import (
    CompiledWorkflow,
    ExecutableTask,
    RuntimeContext,
    TaskResult,
)
from assurance_agent.workflow.graph.planner import PlanError
from assurance_agent.workflow.graph import runtime as runtime_mod
from assurance_agent.workflow.graph.runtime import (
    GraphDefinitionChanged,
    GraphRuntime,
    GraphRuntimeError,
)
from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2
from assurance_agent.workflow.graph.task_runner import HandlerNodeRunner
from assurance_agent.workflow.graph import workspace as workspace_mod
from assurance_agent.workflow.graph.workspace import TreeStore
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

_SYNC_CONTRACTS = """\
schema_version: "1"
contracts:
  operation:update-issue:
    handler: operation
    side_effect_free: false
    reads: ["project:qa/issues/**"]
    writes: ["project:qa/issues/**", "change:results/**"]
    authorization_writes: ["project:qa/issues/**", "change:results/**"]
    synchronized: ["project:qa/issues/**"]
    exclusive: ["project:issue-registry"]
"""

_SYNC_WORKFLOW = """\
name: synchronized-update
params:
  run_mode: {type: enum, values: [full], default: full}
entrypoints:
  full: {graph: main, allow: "params.run_mode == 'full'"}
policies:
  retry:
    never: {max_attempts: 1, retry_on: []}
  timeout:
    local: {run_seconds: 60, heartbeat_seconds: 0.05}
  scheduler: {max_parallel_tasks: 1}
graphs:
  main:
    max_supersteps: 5
    nodes:
      update:
        uses: operation:update-issue
        outputs:
          - project:qa/issues/ISSUE-1.json
          - change:results/update.json
        retry: never
        timeout: local
    edges:
      - {from: START, to: update}
      - {from: update, to: END}
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


def _context(
    project: Path,
    *,
    params: dict[str, object] | None = None,
    change_id: str = "CH-1",
) -> RuntimeContext:
    change = project / "qa" / "changes" / change_id
    return RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=change,
        change_id=change_id,
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


def _sync_compiled() -> tuple[CompiledWorkflow, object]:
    contracts = parse_execution_contracts(_SYNC_CONTRACTS)
    compiled = compile_workflow(parse_workflow_v2(_SYNC_WORKFLOW), contracts)
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


def _sync_ops(*, calls: dict[str, int] | None = None) -> dict[str, OperationFn]:
    ops = default_operations()

    def update_issue(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        if calls is not None:
            calls["n"] += 1
        # Simulate an unrelated live edit after the invocation snapshot. A synchronized
        # publication must never repair this path from its stale whole-tree target.
        (context.project_root / "app/source.py").write_text("unrelated live version 2\n")
        issue = workspace.project_root / "qa/issues/ISSUE-1.json"
        issue.write_text('{"version":2}\n', encoding="utf-8")
        result = workspace.change_dir / "results/update.json"
        result.parent.mkdir(parents=True)
        result.write_text('{"updated":true}\n', encoding="utf-8")
        return TaskResult(status="succeeded")

    ops["operation:update-issue"] = update_issue
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
    change_id: str = "CH-1",
    object_store=None,
) -> tuple[GraphRuntime, Any]:
    from assurance_agent.workflow.graph.definition_pinning import request_for_compiled

    change_dir = project / "qa" / "changes" / change_id
    if node_runner is not None:
        runtime = assemble_graph_runtime(
            project_root=project,
            change_dir=change_dir,
            compiled=compiled,
            contracts=contracts,
            clock=clock,
            object_store=object_store,
            build_node_runner=lambda _store, _run_child: node_runner,
        )
    else:
        runtime = assemble_graph_runtime(
            project_root=project,
            change_dir=change_dir,
            compiled=compiled,
            contracts=contracts,
            adapter=NeverCalledInvoker(),
            clock=clock,
            object_store=object_store,
        )
    scheduler = runtime._definition_resolver(  # noqa: SLF001
        request_for_compiled(compiled, event_schema_version=6)
    ).scheduler
    return runtime, scheduler


def test_minimal_graph_run_and_fresh_status(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _minimal_compiled()
    runtime, scheduler = _build_runtime(project, compiled, contracts)
    context = _context(project)

    result = runtime.run(compiled, "full", context)
    assert result.exit_code == 0
    assert result.status.status == "completed"
    events = read_events_strict(context.change_dir)
    assert [event["type"] for event in events].count("task_attempt_started") == 1
    assert [event["type"] for event in events].count("task_attempt_succeeded") == 1
    assert events[-1]["type"] == "graph_completed"
    started = next(e for e in events if e.get("type") == "graph_invocation_started")
    assert started.get("event_schema_version") == 6
    assert started.get("gate_semantics_object_id")
    assert started.get("topology_safety_semantics_digest")
    assert started.get("commit_safety_semantics_digest")

    fresh_runtime, _scheduler = _build_runtime(project, compiled, contracts)
    assert fresh_runtime.status(result.invocation_id).model_dump() == result.status.model_dump()


def test_runtime_maps_structured_plan_error_without_reading_its_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _minimal_compiled()
    runtime, scheduler = _build_runtime(project, compiled, contracts)

    def reject_plan(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise PlanError("compiled pins drifted", error_kind="graph_definition_changed")

    monkeypatch.setattr(runtime_mod, "plan_superstep", reject_plan)

    with pytest.raises(GraphDefinitionChanged, match="compiled pins drifted"):
        runtime.run(compiled, "full", _context(project))


def test_runtime_does_not_infer_plan_error_kind_from_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _minimal_compiled()
    runtime, scheduler = _build_runtime(project, compiled, contracts)

    def reject_plan(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise PlanError("graph_definition_changed appeared in ordinary diagnostics")

    monkeypatch.setattr(runtime_mod, "plan_superstep", reject_plan)

    with pytest.raises(GraphRuntimeError) as caught:
        runtime.run(compiled, "full", _context(project))
    assert type(caught.value) is GraphRuntimeError


def test_crash_after_attempt_started_abandons_and_retries(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _write_compiled()
    clock = FakeClock()
    ops = _write_ops()
    runtime, scheduler = _build_runtime(
        project, compiled, contracts, clock=clock, node_runner=_op_runner(ops)
    )

    def crash_run(prepared, plan, projection, context, leases):  # type: ignore[no-untyped-def]
        raise _InjectedCrash("after task_attempt_started")

    scheduler._run_attempt = crash_run  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(_InjectedCrash):
        runtime.run(compiled, "full", _context(project))

    change = _context(project).change_dir
    events = read_events_strict(change)
    assert any(e.get("type") == "task_attempt_started" for e in events)
    assert not any(e.get("type") == "task_attempt_succeeded" for e in events)
    invocation_id = next(
        str(e["invocation_id"]) for e in events if e.get("type") == "graph_invocation_started"
    )

    clock.advance(3600)
    fresh, fresh_scheduler = _build_runtime(
        project, compiled, contracts, clock=clock, node_runner=_op_runner(ops)
    )
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
    runtime, scheduler = _build_runtime(project, compiled, contracts, node_runner=runner)

    def crash_commit(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise _InjectedCrash("after task_attempt_succeeded")

    scheduler._commit_wave = crash_commit  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(_InjectedCrash):
        runtime.run(compiled, "full", _context(project))

    change = _context(project).change_dir
    events = read_events_strict(change)
    assert any(e.get("type") == "task_attempt_succeeded" for e in events)
    assert not any(e.get("type") == "superstep_committed" for e in events)
    assert calls["n"] == 1
    invocation_id = next(
        str(e["invocation_id"]) for e in events if e.get("type") == "graph_invocation_started"
    )

    fresh_calls = {"n": 0}

    class FreshCounting(OperationHandler):
        def execute(self, task, workspace, context):  # type: ignore[no-untyped-def]
            fresh_calls["n"] += 1
            return base.execute(task, workspace, context)

    fresh_handler = FreshCounting(ops)
    fresh, fresh_scheduler = _build_runtime(
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
    runtime, scheduler = _build_runtime(
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
        str(e["invocation_id"]) for e in events if e.get("type") == "graph_invocation_started"
    )

    fresh_calls = {"n": 0}

    class FreshCounting(OperationHandler):
        def execute(self, task, workspace, context):  # type: ignore[no-untyped-def]
            fresh_calls["n"] += 1
            return base.execute(task, workspace, context)

    fresh_handler = FreshCounting(ops)
    fresh, fresh_scheduler = _build_runtime(
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


def _seed_synchronized_project(project: Path) -> None:
    issue = project / "qa/issues/ISSUE-1.json"
    issue.parent.mkdir(parents=True)
    issue.write_text('{"version":1}\n', encoding="utf-8")
    app = project / "app/source.py"
    app.parent.mkdir(parents=True)
    app.write_text("invocation version 1\n", encoding="utf-8")


def test_synchronized_commit_next_loop_replays_targeted_publication_only(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _seed_synchronized_project(project)
    compiled, contracts = _sync_compiled()
    runtime, scheduler = _build_runtime(
        project,
        compiled,
        contracts,
        node_runner=_op_runner(_sync_ops()),
    )

    result = runtime.run(compiled, "full", _context(project))

    assert result.exit_code == 0
    assert (project / "qa/issues/ISSUE-1.json").read_text() == '{"version":2}\n'
    assert (project / "qa/changes/CH-1/results/update.json").read_text() == '{"updated":true}\n'
    assert (project / "app/source.py").read_text() == "unrelated live version 2\n"


def test_synchronized_commit_before_apply_is_repaired_by_fresh_runtime(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _seed_synchronized_project(project)
    compiled, contracts = _sync_compiled()
    calls = {"n": 0}
    runtime, scheduler = _build_runtime(
        project,
        compiled,
        contracts,
        node_runner=_op_runner(_sync_ops(calls=calls)),
    )
    store = runtime._objects  # noqa: SLF001

    def crash_before_apply(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise _InjectedCrash("after synchronized commit before targeted apply")

    store.apply_write_sets_to_synchronized_paths = crash_before_apply  # type: ignore[method-assign]
    with pytest.raises(_InjectedCrash, match="before targeted apply"):
        runtime.run(compiled, "full", _context(project))

    events = read_events_strict(_context(project).change_dir)
    assert any(event.get("type") == "superstep_committed" for event in events)
    invocation_id = next(
        str(event["invocation_id"]) for event in events if event.get("type") == "graph_invocation_started"
    )
    assert calls["n"] == 1
    assert (project / "qa/issues/ISSUE-1.json").read_text() == '{"version":1}\n'

    fresh_calls = {"n": 0}
    fresh, fresh_scheduler = _build_runtime(
        project,
        compiled,
        contracts,
        node_runner=_op_runner(_sync_ops(calls=fresh_calls)),
    )
    result = fresh.resume(invocation_id)

    assert result.exit_code == 0
    assert fresh_calls["n"] == 0
    assert (project / "qa/issues/ISSUE-1.json").read_text() == '{"version":2}\n'
    assert (project / "qa/changes/CH-1/results/update.json").read_text() == '{"updated":true}\n'
    assert (project / "app/source.py").read_text() == "unrelated live version 2\n"


def test_synchronized_partial_apply_is_repaired_by_fresh_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _make_project(tmp_path)
    _seed_synchronized_project(project)
    compiled, contracts = _sync_compiled()
    calls = {"n": 0}
    runtime, scheduler = _build_runtime(
        project,
        compiled,
        contracts,
        node_runner=_op_runner(_sync_ops(calls=calls)),
    )
    store = runtime._objects  # noqa: SLF001
    targeted_apply = store.apply_write_sets_to_synchronized_paths
    real_install = workspace_mod._install_file
    installs = {"n": 0}

    def crash_second_install(path: Path, data: bytes, executable: bool) -> None:
        installs["n"] += 1
        if installs["n"] == 2:
            raise _InjectedCrash("during synchronized targeted apply")
        real_install(path, data, executable)

    def crash_during_apply(*args, **kwargs):  # type: ignore[no-untyped-def]
        monkeypatch.setattr(workspace_mod, "_install_file", crash_second_install)
        try:
            return targeted_apply(*args, **kwargs)
        finally:
            monkeypatch.setattr(workspace_mod, "_install_file", real_install)

    store.apply_write_sets_to_synchronized_paths = crash_during_apply  # type: ignore[method-assign]
    with pytest.raises(_InjectedCrash, match="during synchronized targeted apply"):
        runtime.run(compiled, "full", _context(project))

    events = read_events_strict(_context(project).change_dir)
    invocation_id = next(
        str(event["invocation_id"]) for event in events if event.get("type") == "graph_invocation_started"
    )
    assert calls["n"] == 1
    assert (project / "qa/changes/CH-1/results/update.json").read_text() == '{"updated":true}\n'
    assert (project / "qa/issues/ISSUE-1.json").read_text() == '{"version":1}\n'

    fresh_calls = {"n": 0}
    fresh, fresh_scheduler = _build_runtime(
        project,
        compiled,
        contracts,
        node_runner=_op_runner(_sync_ops(calls=fresh_calls)),
    )
    result = fresh.resume(invocation_id)

    assert result.exit_code == 0
    assert fresh_calls["n"] == 0
    assert (project / "qa/issues/ISSUE-1.json").read_text() == '{"version":2}\n'
    assert (project / "qa/changes/CH-1/results/update.json").read_text() == '{"updated":true}\n'
    assert (project / "app/source.py").read_text() == "unrelated live version 2\n"


def _versioned_sync_ops(version: int) -> dict[str, OperationFn]:
    ops = default_operations()

    def update_issue(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        del task, context
        issue = workspace.project_root / "qa/issues/ISSUE-1.json"
        issue.write_text(json.dumps({"version": version}) + "\n", encoding="utf-8")
        result = workspace.change_dir / "results/update.json"
        result.parent.mkdir(parents=True)
        result.write_text(json.dumps({"version": version}) + "\n", encoding="utf-8")
        return TaskResult(status="succeeded")

    ops["operation:update-issue"] = update_issue
    return ops


def _two_change_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    for change_id in ("CH-A", "CH-B", "CH-C"):
        (project / "qa/changes" / change_id).mkdir(parents=True)
    issue = project / "qa/issues/ISSUE-1.json"
    issue.parent.mkdir(parents=True)
    issue.write_text('{"version": 1}\n', encoding="utf-8")
    write_aa_config(project)
    return project


def test_applied_marker_prevents_stale_replay_after_later_change_advances_resource(
    tmp_path: Path,
) -> None:
    project = _two_change_project(tmp_path)
    compiled, contracts = _sync_compiled()
    runtime_a, scheduler_a = _build_runtime(
        project,
        compiled,
        contracts,
        node_runner=_op_runner(_versioned_sync_ops(2)),
        change_id="CH-A",
    )
    runtime_b, scheduler_b = _build_runtime(
        project,
        compiled,
        contracts,
        node_runner=_op_runner(_versioned_sync_ops(3)),
        change_id="CH-B",
    )
    a_applied = threading.Event()
    continue_a = threading.Event()
    execute_a = scheduler_a.execute  # noqa: SLF001

    def pause_after_a_releases_lock(*args, **kwargs):  # type: ignore[no-untyped-def]
        result = execute_a(*args, **kwargs)
        a_applied.set()
        if not continue_a.wait(timeout=5.0):
            raise RuntimeError("timed out waiting for Change B")
        return result

    scheduler_a.execute = pause_after_a_releases_lock  # type: ignore[method-assign]  # noqa: SLF001
    a_results: list[object] = []
    a_errors: list[BaseException] = []

    def run_a() -> None:
        try:
            a_results.append(runtime_a.run(compiled, "full", _context(project, change_id="CH-A")))
        except BaseException as exc:
            a_errors.append(exc)

    thread = threading.Thread(target=run_a)
    thread.start()
    assert a_applied.wait(timeout=5.0)
    try:
        result_b = runtime_b.run(compiled, "full", _context(project, change_id="CH-B"))
    finally:
        continue_a.set()
        thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert a_errors == []
    assert len(a_results) == 1
    assert getattr(a_results[0], "exit_code") == 0
    assert result_b.exit_code == 0
    assert (project / "qa/issues/ISSUE-1.json").read_text() == '{"version": 3}\n'


def test_prepared_publication_before_commit_does_not_permanently_block_later_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crash after prepare and before checkpoint commit must not permanently lock tokens."""
    import assurance_agent.workflow.graph.scheduler as sched_mod

    project = _two_change_project(tmp_path)
    compiled, contracts = _sync_compiled()
    runtime_a, scheduler_a = _build_runtime(
        project,
        compiled,
        contracts,
        node_runner=_op_runner(_versioned_sync_ops(2)),
        change_id="CH-A",
    )

    def crash_before_commit(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise _InjectedCrash("after publication prepare before checkpoint commit")

    monkeypatch.setattr(sched_mod, "commit_tree_pointer", crash_before_commit)
    with pytest.raises(_InjectedCrash, match="before checkpoint commit"):
        runtime_a.run(compiled, "full", _context(project, change_id="CH-A"))
    monkeypatch.undo()

    events_a = read_events_strict(project / "qa/changes/CH-A")
    assert not any(event.get("type") == "superstep_committed" for event in events_a)
    assert (project / "qa/issues/ISSUE-1.json").read_text() == '{"version": 1}\n'

    runtime_b, scheduler_b = _build_runtime(
        project,
        compiled,
        contracts,
        node_runner=_op_runner(_versioned_sync_ops(3)),
        change_id="CH-B",
    )
    result_b = runtime_b.run(compiled, "full", _context(project, change_id="CH-B"))
    assert result_b.exit_code == 0
    assert (project / "qa/issues/ISSUE-1.json").read_text() == '{"version": 3}\n'


def test_prepared_publication_blocks_later_change_after_apply_before_ack_crash(
    tmp_path: Path,
) -> None:
    project = _two_change_project(tmp_path)
    compiled, contracts = _sync_compiled()
    runtime_a, scheduler_a = _build_runtime(
        project,
        compiled,
        contracts,
        node_runner=_op_runner(_versioned_sync_ops(2)),
        change_id="CH-A",
    )
    apply_a = runtime_a._objects.apply_write_sets_to_synchronized_paths  # noqa: SLF001

    def crash_after_apply(*args, **kwargs):  # type: ignore[no-untyped-def]
        apply_a(*args, **kwargs)
        raise _InjectedCrash("after synchronized apply before publication acknowledgement")

    runtime_a._objects.apply_write_sets_to_synchronized_paths = crash_after_apply  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(_InjectedCrash, match="before publication acknowledgement"):
        runtime_a.run(compiled, "full", _context(project, change_id="CH-A"))
    assert (project / "qa/issues/ISSUE-1.json").read_text() == '{"version": 2}\n'

    runtime_b, scheduler_b = _build_runtime(
        project,
        compiled,
        contracts,
        node_runner=_op_runner(_versioned_sync_ops(3)),
        change_id="CH-B",
    )
    blocked_b = runtime_b.run(compiled, "full", _context(project, change_id="CH-B"))
    assert blocked_b.status.status == "failed"
    assert (project / "qa/issues/ISSUE-1.json").read_text() == '{"version": 2}\n'

    events_a = read_events_strict(project / "qa/changes/CH-A")
    invocation_a = next(
        str(event["invocation_id"]) for event in events_a if event.get("type") == "graph_invocation_started"
    )
    fresh_a, _scheduler_a = _build_runtime(
        project,
        compiled,
        contracts,
        node_runner=_op_runner(_versioned_sync_ops(2)),
        change_id="CH-A",
    )
    assert fresh_a.resume(invocation_a).exit_code == 0

    runtime_c, _scheduler_c = _build_runtime(
        project,
        compiled,
        contracts,
        node_runner=_op_runner(_versioned_sync_ops(3)),
        change_id="CH-C",
    )
    assert runtime_c.run(compiled, "full", _context(project, change_id="CH-C")).exit_code == 0
    assert (project / "qa/issues/ISSUE-1.json").read_text() == '{"version": 3}\n'


def test_crash_after_write_set_freeze_before_success_retries_attempt(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _write_compiled()
    ops = _write_ops()
    clock = FakeClock()
    runtime, scheduler = _build_runtime(
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
        str(e["invocation_id"]) for e in events if e.get("type") == "graph_invocation_started"
    )

    clock.advance(3600)
    store.freeze_write_set = original_freeze  # type: ignore[method-assign]
    fresh, fresh_scheduler = _build_runtime(
        project, compiled, contracts, clock=clock, node_runner=_op_runner(ops)
    )
    result = fresh.resume(invocation_id)
    assert result.exit_code == 0
    events = read_events_strict(change)
    assert any(e.get("type") == "task_attempt_abandoned" for e in events)
    assert [e.get("type") for e in events].count("task_attempt_succeeded") == 1


def test_recovery_barrier_orders_reconcile_before_pending_commit_before_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _write_compiled()
    runtime, scheduler = _build_runtime(project, compiled, contracts, node_runner=_op_runner(_write_ops()))
    order: list[str] = []
    original_reconcile = runtime._reconcile_running
    original_commit_pending = runtime._commit_pending_write_sets
    original_replay = runtime._replay_committed_publications
    original_repair_ordinary = runtime._repair_ordinary_materialization

    def track_reconcile(projection, context):  # type: ignore[no-untyped-def]
        order.append("reconcile")
        return original_reconcile(projection, context)

    def track_commit_pending(projection, context):  # type: ignore[no-untyped-def]
        order.append("pending_commit")
        return original_commit_pending(projection, context)

    def track_replay(projection, context):  # type: ignore[no-untyped-def]
        order.append("replay_publications")
        return original_replay(projection, context)

    def track_repair_ordinary(projection, context):  # type: ignore[no-untyped-def]
        order.append("repair_ordinary")
        return original_repair_ordinary(projection, context)

    def track_plan(*args, **kwargs):  # type: ignore[no-untyped-def]
        order.append("plan")
        raise GraphRuntimeError("stop after first plan")

    monkeypatch.setattr(runtime, "_reconcile_running", track_reconcile)
    monkeypatch.setattr(runtime, "_commit_pending_write_sets", track_commit_pending)
    monkeypatch.setattr(runtime, "_replay_committed_publications", track_replay)
    monkeypatch.setattr(runtime, "_repair_ordinary_materialization", track_repair_ordinary)
    monkeypatch.setattr(runtime_mod, "plan_superstep", track_plan)

    with pytest.raises(GraphRuntimeError, match="stop after first plan"):
        runtime.run(compiled, "full", _context(project))

    assert "reconcile" in order
    assert order.index("reconcile") < order.index("plan")
    assert "pending_commit" in order
    assert order.index("pending_commit") < order.index("plan")
    assert "replay_publications" in order
    assert order.index("replay_publications") < order.index("plan")
    assert "repair_ordinary" in order
    assert order.index("repair_ordinary") < order.index("plan")


def test_pending_commit_recovery_failure_raises_graph_runtime_error(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _write_compiled()
    ops = _write_ops()
    runtime, scheduler = _build_runtime(project, compiled, contracts, node_runner=_op_runner(ops))

    def crash_commit(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise _InjectedCrash("after task_attempt_succeeded")

    scheduler._commit_wave = crash_commit  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(_InjectedCrash):
        runtime.run(compiled, "full", _context(project))

    change = _context(project).change_dir
    events = read_events_strict(change)
    invocation_id = next(
        str(e["invocation_id"]) for e in events if e.get("type") == "graph_invocation_started"
    )

    fresh, fresh_scheduler = _build_runtime(project, compiled, contracts, node_runner=_op_runner(ops))

    def refuse_pending(**_kwargs):  # type: ignore[no-untyped-def]
        raise ValueError("pending commit refused")

    fresh_scheduler.commit_pending_write_sets = refuse_pending  # type: ignore[method-assign]
    with pytest.raises(GraphRuntimeError, match="pending commit refused"):
        fresh.resume(invocation_id)


def test_recovery_barrier_blocks_plan_while_task_still_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _write_compiled()
    clock = FakeClock()
    runtime, scheduler = _build_runtime(
        project, compiled, contracts, clock=clock, node_runner=_op_runner(_write_ops())
    )

    def crash_run(prepared, plan, projection, context, leases):  # type: ignore[no-untyped-def]
        raise _InjectedCrash("after task_attempt_started")

    scheduler._run_attempt = crash_run  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(_InjectedCrash):
        runtime.run(compiled, "full", _context(project))

    change = _context(project).change_dir
    events = read_events_strict(change)
    started = next(e for e in events if e.get("type") == "task_attempt_started")
    invocation_id = next(
        str(e["invocation_id"]) for e in events if e.get("type") == "graph_invocation_started"
    )
    LeaseRegistry(change).upsert(
        new_lease(
            task_id=str(started["task_id"]),
            attempt_id=str(started["attempt_id"]),
            session_id=None,
            started_at=str(started["started_at"]),
            lease_expires_at=(clock.now() + timedelta(hours=1)).isoformat(),
        )
    )

    plan_calls: list[str] = []

    def reject_plan(*args, **kwargs):  # type: ignore[no-untyped-def]
        plan_calls.append("plan")
        raise AssertionError("plan_superstep must not run while a task is still running")

    monkeypatch.setattr(runtime_mod, "plan_superstep", reject_plan)
    fresh, fresh_scheduler = _build_runtime(
        project, compiled, contracts, clock=clock, node_runner=_op_runner(_write_ops())
    )
    with pytest.raises(GraphRuntimeError, match="recovery barrier stalled"):
        fresh.resume(invocation_id)
    assert plan_calls == []


def test_recovery_barrier_skips_plan_while_pending_write_sets_remain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _write_compiled()
    ops = _write_ops()
    runtime, scheduler = _build_runtime(project, compiled, contracts, node_runner=_op_runner(ops))

    def crash_commit(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise _InjectedCrash("after task_attempt_succeeded")

    scheduler._commit_wave = crash_commit  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(_InjectedCrash):
        runtime.run(compiled, "full", _context(project))

    change = _context(project).change_dir
    events = read_events_strict(change)
    invocation_id = next(
        str(e["invocation_id"]) for e in events if e.get("type") == "graph_invocation_started"
    )

    plan_calls: list[str] = []

    def reject_plan(*args, **kwargs):  # type: ignore[no-untyped-def]
        plan_calls.append("plan")
        raise AssertionError("plan_superstep must not run while pending write sets remain")

    monkeypatch.setattr(runtime_mod, "plan_superstep", reject_plan)
    fresh, fresh_scheduler = _build_runtime(project, compiled, contracts, node_runner=_op_runner(ops))

    def refuse_pending(**_kwargs):  # type: ignore[no-untyped-def]
        return False

    fresh_scheduler.commit_pending_write_sets = refuse_pending  # type: ignore[method-assign]
    with pytest.raises(GraphRuntimeError, match="recovery barrier stalled"):
        fresh.resume(invocation_id)
    assert plan_calls == []


_NESTED_SYNC_CONTRACTS = """\
schema_version: "1"
contracts:
  operation:sync-knowledge:
    handler: operation
    reads: [project:.aa/data-knowledge.yaml]
    writes: [project:.aa/data-knowledge.yaml]
    synchronized: [project:.aa/data-knowledge.yaml]
    exclusive: [project:knowledge-registry]
    authorization_writes: [project:.aa/data-knowledge.yaml]
    retryable_errors: []
  operation:plain:
    handler: operation
    side_effect_free: true
    retryable_errors: []
"""

_NESTED_SYNC_WORKFLOW = """\
name: nested-sync-runtime
params:
  run_mode: {type: enum, values: [full], default: full}
entrypoints:
  full: {graph: main, allow: "params.run_mode == 'full'"}
policies:
  retry:
    never: {max_attempts: 1, retry_on: []}
  timeout:
    local: {run_seconds: 60, heartbeat_seconds: 0.05}
  scheduler: {max_parallel_tasks: 4}
graphs:
  main:
    max_supersteps: 8
    nodes:
      child-run:
        uses: graph:leaf
        retry: never
        timeout: local
    edges:
      - {from: START, to: child-run}
      - {from: child-run, to: END}
  leaf:
    max_supersteps: 8
    nodes:
      sync-knowledge:
        uses: operation:sync-knowledge
        outputs: [project:.aa/data-knowledge.yaml]
        retry: never
        timeout: local
    edges:
      - {from: START, to: sync-knowledge}
      - {from: sync-knowledge, to: END}
gates: {}
"""


def _nested_sync_compiled() -> tuple[CompiledWorkflow, ExecutionContractCatalog]:
    contracts = parse_execution_contracts(_NESTED_SYNC_CONTRACTS)
    compiled = compile_workflow(parse_workflow_v2(_NESTED_SYNC_WORKFLOW), contracts)
    return compiled, contracts


def _nested_sync_project(tmp_path: Path) -> tuple[Path, TreeStore]:
    project = tmp_path / "proj"
    change = project / "qa/changes/CH-1"
    change.mkdir(parents=True)
    aa = project / ".aa"
    aa.mkdir()
    (aa / "data-knowledge.yaml").write_text("snapshot v1\n", encoding="utf-8")
    write_aa_config(project)
    store = TreeStore(change)
    store.capture(project)
    (aa / "data-knowledge.yaml").write_text("promoted live v2\n", encoding="utf-8")
    return project, store


def test_nested_drive_run_child_single_synchronized_capture(tmp_path: Path) -> None:
    """Outer _drive → run_child → nested _drive performs exactly one overlay capture."""
    project, store = _nested_sync_project(tmp_path)
    compiled, contracts = _nested_sync_compiled()
    overlay_calls: list[int] = []
    overlay_many = store.overlay_synchronized_paths_many
    nested_capture = {"active": False}

    def observed_overlay_many(tree_ids, project_root, paths):
        if nested_capture["active"]:
            overlay_calls.append(len(tree_ids))
        return overlay_many(tree_ids, project_root, paths)

    store.overlay_synchronized_paths_many = observed_overlay_many  # type: ignore[method-assign]

    ops = default_operations()

    def sync_knowledge(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        path = workspace.project_root / ".aa/data-knowledge.yaml"
        path.write_text("written from nested leaf\n", encoding="utf-8")
        return TaskResult(status="succeeded")

    ops["operation:sync-knowledge"] = sync_knowledge
    change = project / "qa/changes/CH-1"

    def build_node_runner(object_store, run_child):  # type: ignore[no-untyped-def]
        from assurance_agent.workflow.graph.handlers.subgraph import SubgraphHandler

        def wrapped_run_child(task, graph_id, workspace, context):  # type: ignore[no-untyped-def]
            nested_capture["active"] = True
            try:
                return run_child(task, graph_id, workspace, context)
            finally:
                nested_capture["active"] = False

        operation = OperationHandler(ops)
        return HandlerNodeRunner(
            {target: operation for target in ops},
            namespace_handlers={"graph": SubgraphHandler(wrapped_run_child)},
            compiled=compiled,
            object_store=object_store,
        )

    runtime = assemble_graph_runtime(
        project_root=project,
        change_dir=change,
        compiled=compiled,
        contracts=contracts,
        object_store=store,
        build_node_runner=build_node_runner,
        clock=SystemClock(),
    )
    result = runtime.run(compiled, "full", _context(project))

    assert result.exit_code == 0
    assert overlay_calls == [1]
    assert (project / ".aa/data-knowledge.yaml").read_text() == "written from nested leaf\n"


def test_nested_synchronized_publication_survives_later_sibling_subgraph(
    tmp_path: Path,
) -> None:
    """A nested publication belongs to the canonical SUT, not its parent sandbox."""
    workflow = _NESTED_SYNC_WORKFLOW.replace(
        "uses: graph:leaf\n        retry: never",
        "uses: graph:outer\n        retry: never",
        1,
    ).replace(
        "  leaf:\n    max_supersteps: 8",
        "  outer:\n"
        "    max_supersteps: 8\n"
        "    nodes:\n"
        "      publish:\n"
        "        uses: graph:leaf\n"
        "        retry: never\n"
        "        timeout: local\n"
        "      later:\n"
        "        uses: graph:maybe-sync\n"
        "        retry: never\n"
        "        timeout: local\n"
        "    edges:\n"
        "      - {from: START, to: publish}\n"
        "      - {from: publish, to: later}\n"
        "      - {from: later, to: END}\n"
        "  maybe-sync:\n"
        "    max_supersteps: 8\n"
        "    nodes:\n"
        "      plain:\n"
        "        uses: operation:plain\n"
        "        when: params.run_mode == 'full'\n"
        "        retry: never\n"
        "        timeout: local\n"
        "      sync-unused:\n"
        "        uses: operation:sync-knowledge\n"
        "        when: params.run_mode != 'full'\n"
        "        outputs: [project:.aa/data-knowledge.yaml]\n"
        "        retry: never\n"
        "        timeout: local\n"
        "    edges:\n"
        "      - {from: START, to: plain}\n"
        "      - {from: START, to: sync-unused}\n"
        "      - {from: plain, to: END}\n"
        "      - {from: sync-unused, to: END}\n"
        "  leaf:\n"
        "    max_supersteps: 8",
        1,
    )
    contracts = parse_execution_contracts(_NESTED_SYNC_CONTRACTS)
    compiled = compile_workflow(parse_workflow_v2(workflow), contracts)
    project, store = _nested_sync_project(tmp_path)
    ops = default_operations()

    def sync_knowledge(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        path = workspace.project_root / ".aa/data-knowledge.yaml"
        path.write_text("written from nested leaf\n", encoding="utf-8")
        return TaskResult(status="succeeded")

    def plain(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        return TaskResult(status="succeeded")

    ops["operation:sync-knowledge"] = sync_knowledge
    ops["operation:plain"] = plain
    change = project / "qa/changes/CH-1"

    def build_node_runner(object_store, run_child):  # type: ignore[no-untyped-def]
        from assurance_agent.workflow.graph.handlers.subgraph import SubgraphHandler

        operation = OperationHandler(ops)
        return HandlerNodeRunner(
            {target: operation for target in ops},
            namespace_handlers={"graph": SubgraphHandler(run_child)},
            compiled=compiled,
            object_store=object_store,
        )

    runtime = assemble_graph_runtime(
        project_root=project,
        change_dir=change,
        compiled=compiled,
        contracts=contracts,
        object_store=store,
        build_node_runner=build_node_runner,
        clock=SystemClock(),
    )
    result = runtime.run(compiled, "full", _context(project))

    assert result.exit_code == 0, result.reason
    assert result.status.status == "completed"
    assert (project / ".aa/data-knowledge.yaml").read_text() == "written from nested leaf\n"


# ---------------------------------------------------------------------------
# Task 14 Step 4/5 — publication seams + incompatible epoch fail-closed
# ---------------------------------------------------------------------------


def test_sync_apply_pending_resume_replays_before_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _make_project(tmp_path)
    _seed_synchronized_project(project)
    (project / "qa/issues/ISSUE-1.json").write_text(
        '{"version":1,"seed":"apply-pending"}\n', encoding="utf-8"
    )
    compiled, contracts = _sync_compiled()
    calls = {"n": 0}
    runtime, _scheduler = _build_runtime(
        project, compiled, contracts, node_runner=_op_runner(_sync_ops(calls=calls))
    )
    store = runtime._objects  # noqa: SLF001

    def crash_before_apply(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise _InjectedCrash("sync apply pending")

    store.apply_write_sets_to_synchronized_paths = crash_before_apply  # type: ignore[method-assign]
    with pytest.raises(_InjectedCrash, match="sync apply pending"):
        runtime.run(compiled, "full", _context(project))

    change = _context(project).change_dir
    events = read_events_strict(change)
    assert any(e.get("type") == "superstep_committed" for e in events)
    invocation_id = next(
        str(e["invocation_id"]) for e in events if e.get("type") == "graph_invocation_started"
    )
    assert (project / "qa/issues/ISSUE-1.json").read_text() == '{"version":1,"seed":"apply-pending"}\n'

    order: list[str] = []
    fresh_calls = {"n": 0}
    fresh, _ = _build_runtime(
        project, compiled, contracts, node_runner=_op_runner(_sync_ops(calls=fresh_calls))
    )
    original_replay = fresh._replay_committed_publications
    original_plan = runtime_mod.plan_superstep

    def track_replay(projection, context):  # type: ignore[no-untyped-def]
        order.append("replay_publications")
        return original_replay(projection, context)

    def track_plan(*args, **kwargs):  # type: ignore[no-untyped-def]
        order.append("plan")
        return original_plan(*args, **kwargs)

    monkeypatch.setattr(fresh, "_replay_committed_publications", track_replay)
    monkeypatch.setattr(runtime_mod, "plan_superstep", track_plan)
    result = fresh.resume(invocation_id)
    assert result.exit_code == 0
    assert fresh_calls["n"] == 0
    assert "replay_publications" in order
    assert order.index("replay_publications") < order.index("plan")
    assert (project / "qa/issues/ISSUE-1.json").read_text() == '{"version":2}\n'


def test_ordinary_pending_commit_resume_commits_frozen_write_set_once(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    compiled, contracts = _write_compiled()
    ops = _write_ops()
    calls = {"n": 0}
    base = OperationHandler(ops)

    class CountingHandler(OperationHandler):
        def execute(self, task, workspace, context):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            return base.execute(task, workspace, context)

    runtime, scheduler = _build_runtime(
        project,
        compiled,
        contracts,
        node_runner=HandlerNodeRunner({target: CountingHandler(ops) for target in ops}),
    )

    def crash_commit(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise _InjectedCrash("ordinary pending commit seam")

    scheduler._commit_wave = crash_commit  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(_InjectedCrash):
        runtime.run(compiled, "full", _context(project))
    change = _context(project).change_dir
    events = read_events_strict(change)
    assert any(e.get("type") == "task_attempt_succeeded" for e in events)
    assert not any(e.get("type") == "superstep_committed" for e in events)
    invocation_id = next(
        str(e["invocation_id"]) for e in events if e.get("type") == "graph_invocation_started"
    )
    assert calls["n"] == 1

    fresh_calls = {"n": 0}

    class FreshCounting(OperationHandler):
        def execute(self, task, workspace, context):  # type: ignore[no-untyped-def]
            fresh_calls["n"] += 1
            return base.execute(task, workspace, context)

    fresh, _ = _build_runtime(
        project,
        compiled,
        contracts,
        node_runner=HandlerNodeRunner({target: FreshCounting(ops) for target in ops}),
    )
    result = fresh.resume(invocation_id)
    assert result.exit_code == 0
    assert fresh_calls["n"] == 0
    assert [e.get("type") for e in read_events_strict(change)].count("task_attempt_succeeded") == 1
    assert (project / "tests" / "api" / "marker.py").read_text(encoding="utf-8") == "marker\n"


def test_incompatible_gate_semantics_refuses_before_pending_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import assurance_agent.workflow.orchestration.gate_semantics as gate_sem

    project = _make_project(tmp_path)
    compiled, contracts = _write_compiled()
    runtime, scheduler = _build_runtime(project, compiled, contracts, node_runner=_op_runner(_write_ops()))

    def crash_commit(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise _InjectedCrash("after success before commit")

    scheduler._commit_wave = crash_commit  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(_InjectedCrash):
        runtime.run(compiled, "full", _context(project))
    change = _context(project).change_dir
    invocation_id = next(
        str(e["invocation_id"])
        for e in read_events_strict(change)
        if e.get("type") == "graph_invocation_started"
    )
    marker_path = project / "tests" / "api" / "marker.py"
    marker_before = marker_path.read_text(encoding="utf-8") if marker_path.exists() else None

    fresh, _ = _build_runtime(project, compiled, contracts, node_runner=_op_runner(_write_ops()))
    monkeypatch.setattr(gate_sem, "gate_semantics_digest", lambda: "sha256:" + "0" * 64)
    with pytest.raises(GraphDefinitionChanged, match="gate semantics"):
        fresh.resume(invocation_id)
    marker_after = marker_path.read_text(encoding="utf-8") if marker_path.exists() else None
    assert marker_after == marker_before
    assert not any(e.get("type") == "superstep_committed" for e in read_events_strict(change))


def test_incompatible_assurance_profile_refuses_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import assurance_agent.verification.profile_manifest as profile_mod

    project = _make_project(tmp_path)
    compiled, contracts = _write_compiled()
    runtime, scheduler = _build_runtime(project, compiled, contracts, node_runner=_op_runner(_write_ops()))

    def crash_commit(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise _InjectedCrash("after success")

    scheduler._commit_wave = crash_commit  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(_InjectedCrash):
        runtime.run(compiled, "full", _context(project))
    change = _context(project).change_dir
    invocation_id = next(
        str(e["invocation_id"])
        for e in read_events_strict(change)
        if e.get("type") == "graph_invocation_started"
    )
    fresh, _ = _build_runtime(project, compiled, contracts, node_runner=_op_runner(_write_ops()))
    monkeypatch.setattr(profile_mod, "assurance_profile_digest", lambda: "sha256:" + "1" * 64)
    with pytest.raises(GraphDefinitionChanged, match="assurance profile"):
        fresh.resume(invocation_id)
