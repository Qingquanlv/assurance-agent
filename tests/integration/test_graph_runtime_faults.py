"""Process-kill fault injection at GraphRuntime persistence boundaries.

Each case launches a worker subprocess that SIGKILLs itself at a named seam,
then a fresh process resumes and asserts terminality, attempt/budget counts,
no repeated successes, no event deletion, and canonical tree consistency.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from assurance_agent.workflow.core.events import read_events, read_events_strict
from assurance_agent.workflow.graph.checkpoint import CheckpointStore, project_invocation
from tests.helpers_aa import write_aa_config

WORKER = Path(__file__).with_name("_graph_fault_worker.py")

FAULT_CASES = [
    ("before_attempt_started", "linear"),
    ("after_attempt_started", "linear"),
    ("handler_before_success", "linear"),
    ("sibling_success_before_commit", "siblings"),
    ("budget_success_transaction", "budget"),
    ("interrupt_event", "interrupt"),
    ("tree_pointer_superstep", "linear"),
    ("canonical_materialization", "linear"),
    ("checkpoint_snapshot_write", "linear"),
    ("heartbeat_replacement", "linear"),
]


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    change = project / "qa" / "changes" / "CH-1"
    change.mkdir(parents=True)
    (project / "tests" / "api").mkdir(parents=True)
    (project / "tests" / "e2e").mkdir(parents=True)
    write_aa_config(project)
    return project


def _run_worker(
    project: Path,
    sync: Path,
    *,
    mode: str,
    point: str = "",
    schema: str = "linear",
    invocation_id: str | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "AA_FAULT_PROJECT": str(project),
        "AA_FAULT_SYNC": str(sync),
        "AA_FAULT_MODE": mode,
        "AA_FAULT_SCHEMA": schema,
        "AA_FAULT_POINT": point,
    }
    if invocation_id is not None:
        env["AA_FAULT_INVOCATION"] = invocation_id
    sync.mkdir(parents=True, exist_ok=True)
    for name in ("READY", "HIT", "DONE"):
        path = sync / name
        if path.exists():
            path.unlink()
    return subprocess.run(
        [sys.executable, str(WORKER)],
        env=env,
        cwd=str(Path.cwd()),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _wait_for(path: Path, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def _spawn_and_kill(
    project: Path,
    sync: Path,
    *,
    point: str,
    schema: str,
) -> str:
    env = {
        **os.environ,
        "AA_FAULT_PROJECT": str(project),
        "AA_FAULT_SYNC": str(sync),
        "AA_FAULT_MODE": "run",
        "AA_FAULT_SCHEMA": schema,
        "AA_FAULT_POINT": point,
    }
    sync.mkdir(parents=True, exist_ok=True)
    for name in ("READY", "HIT", "DONE"):
        path = sync / name
        if path.exists():
            path.unlink()
    proc = subprocess.Popen(
        [sys.executable, str(WORKER)],
        env=env,
        cwd=str(Path.cwd()),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(sync / "READY")
        _wait_for(sync / "HIT", timeout=20.0)
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    change = project / "qa" / "changes" / "CH-1"
    events = read_events(change)
    invocation = next(
        (
            str(e["invocation_id"])
            for e in events
            if e.get("type") == "graph_invocation_started" and isinstance(e.get("invocation_id"), str)
        ),
        None,
    )
    stderr = ""
    if proc.stderr is not None:
        stderr = proc.stderr.read()
    assert invocation is not None, f"no invocation after kill; stderr={stderr}"
    return invocation


def _assert_recovery(change: Path, invocation_id: str) -> None:
    events = read_events_strict(change)
    assert events
    seqs = [seq for e in events if isinstance((seq := e.get("seq")), int)]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))

    projection = project_invocation(change, invocation_id)
    succeeded = [e for e in events if e.get("type") == "task_attempt_succeeded"]
    by_task: dict[str, int] = {}
    for event in succeeded:
        tid = str(event.get("task_id"))
        by_task[tid] = by_task.get(tid, 0) + 1
    assert all(count == 1 for count in by_task.values()), by_task

    budgets = [e for e in events if e.get("type") == "budget_consumed"]
    by_consumption: dict[str, int] = {}
    for event in budgets:
        cid = str(event.get("consumption_id"))
        by_consumption[cid] = by_consumption.get(cid, 0) + 1
    assert all(count == 1 for count in by_consumption.values()), by_consumption

    commits = [e for e in events if e.get("type") == "superstep_committed"]
    if commits:
        target = commits[-1].get("target_tree_id")
        assert isinstance(target, str) and target
        assert projection.current_tree_id == target


@pytest.mark.parametrize(("point", "schema"), FAULT_CASES)
def test_fault_kill_then_fresh_resume(tmp_path: Path, point: str, schema: str) -> None:
    project = _project(tmp_path)
    sync = tmp_path / "sync" / point
    invocation_id = _spawn_and_kill(project, sync, point=point, schema=schema)
    change = project / "qa" / "changes" / "CH-1"

    driver = change / "driver.json"
    if driver.exists():
        driver.write_text("{not-json", encoding="utf-8")
    snap_dir = change / ".graph-runtime" / "checkpoints"
    if snap_dir.exists():
        for snap in snap_dir.glob("*.json"):
            snap.write_text("{broken", encoding="utf-8")

    resume_sync = tmp_path / "sync" / f"{point}-resume"
    completed = _run_worker(
        project,
        resume_sync,
        mode="resume",
        schema=schema,
        invocation_id=invocation_id,
    )
    assert completed.returncode == 0, completed.stderr
    assert (resume_sync / "DONE").exists()
    done = (resume_sync / "DONE").read_text(encoding="utf-8")
    exit_code, _status, inv = done.split(":", 2)
    assert exit_code in {"0", "10", "20"}
    assert inv == invocation_id

    events = read_events_strict(change)
    attempts = [e for e in events if e.get("type") == "task_attempt_started"]
    assert 1 <= len(attempts) <= 6, len(attempts)
    _assert_recovery(change, invocation_id)

    fresh = CheckpointStore(change).project(invocation_id)
    assert fresh.terminal in {"completed", "stopped", "failed"}
    if schema == "budget":
        budget_events = [e for e in events if e.get("type") == "budget_consumed"]
        assert fresh.budgets.get("loop", 0) == len(budget_events)


def test_acceptance_corrupted_projections_rebuild_from_ledger(tmp_path: Path) -> None:
    project = _project(tmp_path)
    sync = tmp_path / "sync" / "clean"
    result = _run_worker(project, sync, mode="run", schema="linear")
    assert result.returncode == 0, result.stderr
    change = project / "qa" / "changes" / "CH-1"
    invocation_id = CheckpointStore(change).latest_root_invocation()
    assert invocation_id
    before = project_invocation(change, invocation_id)

    (change / "driver.json").write_text("{nope", encoding="utf-8")
    (change / "workflow-state.yaml").write_text("broken: [", encoding="utf-8")
    snap_dir = change / ".graph-runtime" / "checkpoints"
    if snap_dir.exists():
        for snap in snap_dir.glob("*.json"):
            snap.write_text("{", encoding="utf-8")

    after = CheckpointStore(change).project(invocation_id)
    assert after.terminal == before.terminal
    assert after.current_tree_id == before.current_tree_id
    assert "invocation_id:" in (change / "workflow-state.yaml").read_text(encoding="utf-8")
    assert "terminal: completed" in (change / "workflow-state.yaml").read_text(encoding="utf-8")


def test_acceptance_schema_digest_drift_refuses_resume(tmp_path: Path) -> None:
    from assurance_agent.workflow.graph.runtime import GraphDefinitionChanged
    from tests.integration._graph_fault_worker import _build

    project = _project(tmp_path)
    sync = tmp_path / "sync" / "digest"
    invocation_id = _spawn_and_kill(project, sync, point="after_attempt_started", schema="linear")
    change = project / "qa" / "changes" / "CH-1"
    assert project_invocation(change, invocation_id).terminal is None

    runtime, _compiled, _ = _build(project, "linear")
    runtime._schema_resolver = lambda digest: (_ for _ in ()).throw(  # noqa: SLF001
        RuntimeError(f"schema digest drift: {digest}")
    )
    with pytest.raises(GraphDefinitionChanged, match="digest"):
        runtime.resume(invocation_id)


def _build_recovery_runtime(
    project: Path,
    *,
    analyzer_error: str,
    fallback_calls: list[object],
):
    from assurance_agent.workflow.driver.operations_catalog import default_operations
    from assurance_agent.workflow.driver.runtime_factory import assemble_graph_runtime
    from assurance_agent.workflow.graph.compiler import compile_workflow
    from assurance_agent.workflow.graph.contracts import parse_execution_contracts
    from assurance_agent.workflow.graph.handlers.operation import OperationHandler
    from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
    from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2
    from assurance_agent.workflow.graph.task_runner import HandlerNodeRunner
    from tests.integration._graph_fault_worker import FakeClock

    contracts = parse_execution_contracts(
        """
schema_version: "1"
contracts:
  operation:analyzer:
    handler: operation
    side_effect_free: false
    writes: ["repo:tests/api/**"]
    authorization_writes: ["repo:tests/api/**"]
    retryable_errors: [timeout]
  operation:fallback: {handler: operation, side_effect_free: true}
  operation:recovered: {handler: operation, side_effect_free: true}
"""
    )
    compiled = compile_workflow(
        parse_workflow_v2(
            """
schema_version: "2"
name: runtime-recovery
entrypoints:
  full: {graph: main}
policies:
  retry:
    analyzer-retry:
      max_attempts: 2
      retry_on: [timeout]
      backoff: {initial_seconds: 0.01, multiplier: 1.0, max_seconds: 0.01, jitter: false}
graphs:
  main:
    max_supersteps: 8
    nodes:
      analyzer:
        uses: operation:analyzer
        outputs: ["repo:tests/api/analyzer.py"]
        retry: analyzer-retry
        recover:
          errors: [timeout]
          via: fallback
          continue_to: recovered
      fallback: {uses: operation:fallback}
      recovered: {uses: operation:recovered}
    edges:
      - {from: START, to: analyzer}
      - {from: analyzer, to: END}
      - {from: recovered, to: END}
"""
        ),
        contracts,
    )
    change = project / "qa" / "changes" / "CH-1"
    clock = FakeClock()
    ops = default_operations()

    def analyze(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        marker = workspace.project_root / "tests" / "api" / "analyzer.py"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("failed analyzer output\n", encoding="utf-8")
        return TaskResult(
            status="failed",
            error_kind=analyzer_error,  # type: ignore[arg-type]
            error="analyzer timed out" if analyzer_error == "timeout" else "forbidden analyzer write",
        )

    def fallback(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        fallback_calls.append(task.recovery)
        assert task.recovery is not None
        assert task.recovery.error_kind == "timeout"
        assert task.recovery.message == "analyzer timed out"
        return TaskResult(status="succeeded")

    def recovered(task: ExecutableTask, workspace, context: RuntimeContext) -> TaskResult:
        return TaskResult(status="succeeded")

    ops.update(
        {
            "operation:analyzer": analyze,
            "operation:fallback": fallback,
            "operation:recovered": recovered,
        }
    )

    def build(_store, _run_child):  # type: ignore[no-untyped-def]
        handler = OperationHandler(ops)
        return HandlerNodeRunner({target: handler for target in ops})

    runtime = assemble_graph_runtime(
        project_root=project,
        change_dir=change,
        compiled=compiled,
        contracts=contracts,
        build_node_runner=build,
        clock=clock,
    )
    return runtime, compiled, runtime._scheduler, change  # noqa: SLF001


def test_recovery_event_survives_crash_before_fallback_dispatch(tmp_path: Path) -> None:
    class SimulatedCrash(RuntimeError):
        pass

    project = _project(tmp_path)
    fallback_calls: list[object] = []
    runtime, compiled, scheduler, change = _build_recovery_runtime(
        project,
        analyzer_error="timeout",
        fallback_calls=fallback_calls,
    )
    original_execute = scheduler.execute
    crashed = False

    def crash_before_fallback(plan, projection, context):  # type: ignore[no-untyped-def]
        nonlocal crashed
        if not crashed and any(task.node_id == "fallback" for task in plan.tasks):
            crashed = True
            assert len(projection.recoveries) == 1
            raise SimulatedCrash("after recovery event, before fallback dispatch")
        return original_execute(plan, projection, context)

    scheduler.execute = crash_before_fallback  # type: ignore[method-assign]
    from assurance_agent.workflow.graph.models import RuntimeContext

    context = RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=change,
        change_id="CH-1",
    )
    with pytest.raises(SimulatedCrash):
        runtime.run(compiled, "full", context)

    invocation_id = CheckpointStore(change).latest_root_invocation("full")
    assert invocation_id is not None
    crashed_projection = project_invocation(change, invocation_id)
    assert len(crashed_projection.recoveries) == 1
    assert [
        task.attempts_used for task in crashed_projection.tasks.values() if task.node_id == "analyzer"
    ] == [2]
    assert not any(task.node_id == "fallback" for task in crashed_projection.tasks.values())
    assert not (project / "tests" / "api" / "analyzer.py").exists()

    resumed, _compiled, _scheduler, _change = _build_recovery_runtime(
        project,
        analyzer_error="timeout",
        fallback_calls=fallback_calls,
    )
    result = resumed.resume(invocation_id)

    assert result.status.status == "completed"
    assert len(fallback_calls) == 1
    recovery = fallback_calls[0]
    assert recovery is not None
    assert recovery.error_kind == "timeout"  # type: ignore[union-attr]
    assert recovery.message == "analyzer timed out"  # type: ignore[union-attr]
    final = project_invocation(change, invocation_id)
    analyzer = next(task for task in final.tasks.values() if task.node_id == "analyzer")
    assert analyzer.status == "failed"
    assert analyzer.outputs_committed is False
    assert not (project / "tests" / "api" / "analyzer.py").exists()
    events = read_events_strict(change)
    assert sum(event.get("type") == "task_recovery_routed" for event in events) == 1
    assert (
        sum(
            event.get("type") == "task_attempt_started" and event.get("node_id") == "fallback"
            for event in events
        )
        == 1
    )


def test_forbidden_write_failure_does_not_enter_recovery(tmp_path: Path) -> None:
    project = _project(tmp_path)
    fallback_calls: list[object] = []
    runtime, compiled, _scheduler, change = _build_recovery_runtime(
        project,
        analyzer_error="forbidden_write",
        fallback_calls=fallback_calls,
    )
    from assurance_agent.workflow.graph.models import RuntimeContext

    result = runtime.run(
        compiled,
        "full",
        RuntimeContext(
            project_root=project,
            repo_root=project,
            change_dir=change,
            change_id="CH-1",
        ),
    )

    assert result.status.status == "failed"
    assert fallback_calls == []
    projection = project_invocation(change, result.invocation_id)
    assert projection.recoveries == {}
    assert not (project / "tests" / "api" / "analyzer.py").exists()
