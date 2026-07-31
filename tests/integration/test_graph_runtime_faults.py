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
    resume_reason: str = "fault-test resume",
    resume_who: str = "fault-worker",
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "AA_FAULT_PROJECT": str(project),
        "AA_FAULT_SYNC": str(sync),
        "AA_FAULT_MODE": mode,
        "AA_FAULT_SCHEMA": schema,
        "AA_FAULT_POINT": point,
        "AA_FAULT_RESUME_REASON": resume_reason,
        "AA_FAULT_RESUME_WHO": resume_who,
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
    from assurance_agent.workflow.graph.compiler import compile_workflow
    from assurance_agent.workflow.graph.contracts import parse_execution_contracts
    from assurance_agent.workflow.graph.handlers.operation import OperationHandler, default_operations
    from assurance_agent.workflow.graph.models import ExecutableTask, RuntimeContext, TaskResult
    from assurance_agent.workflow.graph.runtime import GraphRuntime
    from assurance_agent.workflow.graph.scheduler import Scheduler
    from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2
    from assurance_agent.workflow.graph.task_runner import HandlerNodeRunner
    from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceBackend
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
    store = TreeStore(change)
    checkpoints = CheckpointStore(change)
    workspaces = WorkspaceBackend(change)
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
    handler = OperationHandler(ops)
    runner = HandlerNodeRunner({target: handler for target in ops})
    scheduler = Scheduler(
        checkpoints=checkpoints,
        object_store=store,
        clock=clock,
        workspace_backend=workspaces,
        node_runner=runner,
        max_parallel_tasks=1,
        contracts=contracts,
    )
    runtime = GraphRuntime(
        checkpoint_store=checkpoints,
        object_store=store,
        workspace_backend=workspaces,
        contracts=contracts,
        node_runner=runner,
        scheduler=scheduler,
        schema_resolver=lambda _digest: compiled,
        clock=clock,
    )
    return runtime, compiled, scheduler, change


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

    def crash_before_fallback(plan, projection, context, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal crashed
        if not crashed and any(task.node_id == "fallback" for task in plan.tasks):
            crashed = True
            fresh = runtime._checkpoints.project(projection.invocation_id)  # noqa: SLF001
            assert len(fresh.recoveries) == 1
            raise SimulatedCrash("after recovery event, before fallback dispatch")
        return original_execute(plan, projection, context, **kwargs)

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


def test_start_invocation_commits_root_before_drive(tmp_path: Path) -> None:
    from assurance_agent.workflow.graph.checkpoint import project_invocation
    from assurance_agent.workflow.graph.models import RuntimeContext
    from assurance_agent.workflow.graph.runtime import GraphRuntimeError
    from tests.integration._graph_fault_worker import _build

    project = _project(tmp_path)
    runtime, compiled, change = _build(project, "linear")
    context = RuntimeContext(
        project_root=project,
        repo_root=project,
        change_dir=change,
        change_id="CH-1",
        params={"run_mode": "full"},
    )
    invocation_id = runtime.start_invocation(compiled, "full", context)
    projection = project_invocation(change, invocation_id)
    assert projection.invocation_id == invocation_id
    assert projection.entrypoint == "full"
    assert projection.parent_invocation_id is None
    assert projection.terminal is None

    def drive_fault(inv_id: str, ctx: RuntimeContext):  # noqa: ANN001
        raise GraphRuntimeError("drive fault after start commit")

    runtime._drive = drive_fault  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(GraphRuntimeError, match="drive fault"):
        runtime.drive_started(invocation_id)
    assert project_invocation(change, invocation_id).invocation_id == invocation_id

    fresh_project = _project(tmp_path / "fresh")
    fresh_runtime, fresh_compiled, fresh_change = _build(fresh_project, "linear")
    fresh_context = RuntimeContext(
        project_root=fresh_project,
        repo_root=fresh_project,
        change_dir=fresh_change,
        change_id="CH-1",
        params={"run_mode": "full"},
    )
    composed = fresh_runtime.run(fresh_compiled, "full", fresh_context)

    split_project = _project(tmp_path / "split")
    split_runtime, split_compiled, split_change = _build(split_project, "linear")
    split_context = RuntimeContext(
        project_root=split_project,
        repo_root=split_project,
        change_dir=split_change,
        change_id="CH-1",
        params={"run_mode": "full"},
    )
    split_start = split_runtime.start_invocation(split_compiled, "full", split_context)
    split_drive = split_runtime.drive_started(split_start)
    assert split_drive.status.status == composed.status.status
    assert split_drive.exit_code == composed.exit_code


# ---------------------------------------------------------------------------
# Task 9: v5 manual-revision power-loss recovery on a synthetic nested graph
# ---------------------------------------------------------------------------

REVISION_FAULT_POINTS = [
    "revision_target_objects",
    "manual_plan_revision_append",
    "graph_resumed_ordinal_0",
    "graph_resumed_ordinal_1",
    "graph_resumed_ordinal_2",
]


def _spawn_resume_and_kill(
    project: Path,
    sync: Path,
    *,
    point: str,
    invocation_id: str,
    schema: str = "v5_revision",
) -> None:
    env = {
        **os.environ,
        "AA_FAULT_PROJECT": str(project),
        "AA_FAULT_SYNC": str(sync),
        "AA_FAULT_MODE": "resume",
        "AA_FAULT_SCHEMA": schema,
        "AA_FAULT_POINT": point,
        "AA_FAULT_INVOCATION": invocation_id,
        "AA_FAULT_RESUME_REASON": "revise synth plan",
        "AA_FAULT_RESUME_WHO": "reviewer",
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
        _wait_for(sync / "HIT", timeout=30.0)
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    assert (sync / "HIT").read_text(encoding="utf-8") == point


def test_v5_manual_revision_resumes_root_to_leaf(tmp_path: Path) -> None:
    from tests.integration._graph_fault_worker import (
        assert_revision_resume_chain,
        edit_recorded_revision_view,
        fix_and_proceed_command,
        prepare_interrupted_v5_graph,
    )

    runtime, compiled, context, root_id = prepare_interrupted_v5_graph(tmp_path)
    del compiled, context
    edit_recorded_revision_view(root_id, b"# revised plan\n")
    result = runtime.resume(root_id, fix_and_proceed_command(root_id))
    assert result.status.status == "completed"
    assert_revision_resume_chain(root_id, expected_ordinals=(0, 1, 2))


def test_v5_nested_revision_view_survives_and_propagates(tmp_path: Path) -> None:
    from assurance_agent.workflow.core.events import read_events_strict
    from assurance_agent.workflow.graph.checkpoint import project_invocation
    from tests.integration._graph_fault_worker import (
        _REVISION_FIXTURES,
        edit_recorded_revision_view,
        fix_and_proceed_command,
        prepare_interrupted_v5_graph,
    )

    runtime, _compiled, _context, root_id = prepare_interrupted_v5_graph(tmp_path)
    fx = _REVISION_FIXTURES[root_id]
    change = fx["change"]
    assert isinstance(change, Path)
    view_root = change / str(fx["revision_view"])
    assert view_root.is_dir()
    assert (view_root / "plans" / "synth-plan.md").is_file()

    root_before = project_invocation(change, root_id)
    pending = runtime.status(root_id).pending_interrupts[0]
    cycle_id = pending.revision_owner_invocation_id
    assert cycle_id
    leaf_before = project_invocation(change, cycle_id)
    started_ids = {
        e.get("invocation_id")
        for e in read_events_strict(change)
        if e.get("type") == "graph_invocation_started"
    }
    branch_ids = [
        cid
        for cid in started_ids
        if isinstance(cid, str) and project_invocation(change, cid).parent_invocation_id == root_id
    ]
    assert len(branch_ids) == 1
    branch_before = project_invocation(change, branch_ids[0])

    edit_recorded_revision_view(root_id, b"# revised plan\n")
    done = runtime.resume(root_id, fix_and_proceed_command(root_id))
    assert done.status.status == "completed"
    assert view_root.is_dir(), "revision view must survive leaf TaskWorkspace cleanup"

    events = read_events_strict(change)
    revisions = [e for e in events if e.get("type") == "manual_plan_revision"]
    assert len(revisions) == 1
    revision = revisions[0]
    assert revision["invocation_id"] == cycle_id
    assert revision["base_tree_id"] == leaf_before.current_tree_id
    assert revision["target_tree_id"] != revision["base_tree_id"]

    post_nodes = {
        e.get("node_id")
        for e in events
        if e.get("type") == "task_attempt_started"
        and e.get("invocation_id") == cycle_id
        and int(e["seq"]) > int(revision["seq"])  # type: ignore[arg-type]
    }
    assert {"review", "mechanical", "gate"} <= post_nodes

    resumes = [
        e
        for e in events
        if e.get("type") == "graph_resumed"
        and e.get("revision_transition_id") == revision["revision_transition_id"]
    ]
    assert [e.get("revision_ordinal") for e in resumes] == [0, 1, 2]
    assert resumes[0]["invocation_id"] == root_id
    assert resumes[1]["invocation_id"] == branch_ids[0]
    assert resumes[2]["invocation_id"] == cycle_id
    assert resumes[0].get("parent_anchor_ref") is None
    assert resumes[1].get("parent_anchor_ref") is not None
    assert resumes[2].get("parent_anchor_ref") is not None

    root_after = project_invocation(change, root_id)
    branch_after = project_invocation(change, branch_ids[0])
    leaf_after = project_invocation(change, cycle_id)
    assert leaf_after.current_tree_id != leaf_before.current_tree_id
    assert root_after.current_tree_id != root_before.current_tree_id
    assert branch_after.current_tree_id != branch_before.current_tree_id


@pytest.mark.parametrize(
    "mutator",
    ["noop", "extra_file", "missing_file", "symlink", "outside_path"],
)
def test_v5_revision_invalid_edits_leave_interrupt_unresolved(tmp_path: Path, mutator: str) -> None:
    from assurance_agent.workflow.graph.runtime import GraphRuntimeError
    from tests.integration._graph_fault_worker import (
        _REVISION_FIXTURES,
        fix_and_proceed_command,
        prepare_interrupted_v5_graph,
    )

    runtime, _compiled, _context, root_id = prepare_interrupted_v5_graph(tmp_path)
    fx = _REVISION_FIXTURES[root_id]
    change = fx["change"]
    assert isinstance(change, Path)
    view = change / str(fx["revision_view"])
    plan = view / "plans" / "synth-plan.md"
    if mutator == "noop":
        pass
    elif mutator == "extra_file":
        plan.write_text("# revised plan\n", encoding="utf-8")
        (view / "plans" / "extra.md").write_text("nope\n", encoding="utf-8")
    elif mutator == "missing_file":
        plan.unlink()
    elif mutator == "symlink":
        plan.unlink()
        plan.symlink_to("/tmp/synth-plan-escape")
    elif mutator == "outside_path":
        plan.write_text("# revised plan\n", encoding="utf-8")
        outside = view / "review" / "tamper.json"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text('{"decision":"pass"}', encoding="utf-8")
    with pytest.raises(GraphRuntimeError):
        runtime.resume(root_id, fix_and_proceed_command(root_id))
    status = runtime.status(root_id)
    assert status.status == "interrupted"
    assert status.pending_interrupts


def test_v5_accept_risk_and_stop_do_not_ingest_revision_view(tmp_path: Path) -> None:
    from assurance_agent.workflow.core.events import read_events_strict
    from assurance_agent.workflow.graph.models import ResumeCommand
    from tests.integration._graph_fault_worker import (
        _REVISION_FIXTURES,
        edit_recorded_revision_view,
        prepare_interrupted_v5_graph,
    )

    runtime, _compiled, _context, root_id = prepare_interrupted_v5_graph(tmp_path)
    fx = _REVISION_FIXTURES[root_id]
    interrupt_id = str(fx["interrupt_id"])
    edit_recorded_revision_view(root_id, b"# should be ignored\n")
    done = runtime.resume(
        root_id,
        ResumeCommand(
            interrupt_id=interrupt_id,
            action="accept_risk",
            reason="accept as-is",
            who="reviewer",
        ),
    )
    assert done.status.status == "completed"
    events = read_events_strict(fx["change"])  # type: ignore[arg-type]
    assert not any(e.get("type") == "manual_plan_revision" for e in events)

    runtime2, _c2, _ctx2, root2 = prepare_interrupted_v5_graph(tmp_path / "stop")
    fx2 = _REVISION_FIXTURES[root2]
    edit_recorded_revision_view(root2, b"# ignored on stop\n")
    stopped = runtime2.resume(
        root2,
        ResumeCommand(
            interrupt_id=str(fx2["interrupt_id"]),
            action="stop",
            reason="abort",
            who="reviewer",
        ),
    )
    assert stopped.exit_code == 20
    events2 = read_events_strict(fx2["change"])  # type: ignore[arg-type]
    assert not any(e.get("type") == "manual_plan_revision" for e in events2)


def test_v5_source_decision_cannot_cross_revised_tree_epoch(tmp_path: Path) -> None:
    """A pre-revision source decision must not authorize the post-revision gate epoch."""
    import hashlib
    import json
    from dataclasses import replace

    from assurance_agent.workflow.core.events import append_event_strict, read_events_strict
    from assurance_agent.workflow.graph.schema_v2 import parse_workflow_v2
    from assurance_agent.workflow.orchestration.gates import GateEvaluationContext, check_gate_in_view
    from tests.integration._graph_fault_worker import (
        _REVISION_FIXTURES,
        _V5_REVISION,
        edit_recorded_revision_view,
        fix_and_proceed_command,
        prepare_interrupted_v5_graph,
    )

    runtime, _compiled, _context, root_id = prepare_interrupted_v5_graph(tmp_path)
    fx = _REVISION_FIXTURES[root_id]
    change = fx["change"]
    assert isinstance(change, Path)
    pending = runtime.status(root_id).pending_interrupts[0]
    source_attempt = pending.source_gate_attempt_id
    source_tree = pending.source_gate_tree_id
    assert source_attempt and source_tree

    edit_recorded_revision_view(root_id, b"# revised plan\n")
    done = runtime.resume(root_id, fix_and_proceed_command(root_id))
    assert done.status.status == "completed"
    events = read_events_strict(change)
    revision = next(e for e in events if e.get("type") == "manual_plan_revision")
    target_tree = str(revision["target_tree_id"])
    leaf_id = str(revision["invocation_id"])
    assert target_tree != source_tree

    # Dedicated audit log so post-revision gate attempts cannot shadow the
    # pre-revision accept_risk override (mirrors unit epoch-binding proof).
    epoch_root = tmp_path / "epoch-proof"
    epoch_change = epoch_root / "qa" / "changes" / "CH-1"
    review_dir = epoch_change / "review"
    review_dir.mkdir(parents=True)
    review_path = review_dir / "synth-plan-review.json"
    checks_path = review_dir / "synth-plan-checks.json"
    review_path.write_text(json.dumps({"decision": "needs_human_review"}), encoding="utf-8")
    checks_path.write_text(json.dumps({"status": "ready", "layer": "synth"}), encoding="utf-8")
    audited = {
        "review/synth-plan-review.json": hashlib.sha256(review_path.read_bytes()).hexdigest(),
        "review/synth-plan-checks.json": hashlib.sha256(checks_path.read_bytes()).hexdigest(),
    }
    append_event_strict(
        epoch_change,
        {
            "source": "graph",
            "type": "graph_invocation_started",
            "invocation_id": leaf_id,
            "entrypoint": "leaf",
            "graph_id": "g-leaf",
            "graph_digest": "dg",
            "event_schema_version": 5,
            "contract_digests": {},
            "policy_digest": "p",
            "policy_origin": "packaged_default",
            "gate_semantics_digest": "s",
            "assurance_profile_digest": "a",
            "params": {},
            "params_sha256": "",
            "root_tree_id": source_tree,
            "max_parallel_tasks": 1,
            "checkpoint_ns": leaf_id,
            "structural_path": "g-leaf",
        },
    )
    append_event_strict(
        epoch_change,
        {
            "source": "graph",
            "type": "task_attempt_succeeded",
            "invocation_id": leaf_id,
            "checkpoint_ns": leaf_id,
            "superstep_id": "ss-1",
            "task_id": "gate-task",
            "attempt_id": source_attempt,
            "gate_report": {"gate_id": "synth-plan-gate", "verdict": "needs_human_review"},
        },
    )
    append_event_strict(
        epoch_change,
        {
            "source": "graph",
            "type": "graph_interrupted",
            "invocation_id": leaf_id,
            "checkpoint_ns": leaf_id,
            "interrupt_id": "epoch-interrupt",
            "node_id": "human-review",
            "checkpoint": "synth-plan-gate",
            "actions": ["accept_risk", "stop", "fix_and_proceed"],
            "audited_reads_sha256": audited,
            "source_gate_attempt_id": source_attempt,
            "source_gate_tree_id": source_tree,
        },
    )
    append_event_strict(
        epoch_change,
        {
            "source": "graph",
            "type": "graph_resumed",
            "invocation_id": leaf_id,
            "checkpoint_ns": leaf_id,
            "interrupt_id": "epoch-interrupt",
            "action": "accept_risk",
            "reason": "accepted pre-revision",
            "who": "reviewer",
            "audited_reads_sha256": audited,
            "payload": {},
            "source_gate_attempt_id": source_attempt,
            "source_gate_tree_id": source_tree,
        },
    )

    schema = parse_workflow_v2(_V5_REVISION)
    base_ctx = GateEvaluationContext(
        project_root=epoch_root,
        repo_root=epoch_root,
        change_dir=epoch_change,
        change_id="CH-1",
        params={},
        state_values={},
        node_results={},
        audit_events_dir=epoch_change,
        event_schema_version=5,
        invocation_id=leaf_id,
    )
    matched = replace(base_ctx, committed_tree_id=source_tree)
    assert check_gate_in_view(schema.gates, "synth-plan-gate", matched).verdict.value == "pass"

    mismatched = replace(base_ctx, committed_tree_id=target_tree)
    assert check_gate_in_view(schema.gates, "synth-plan-gate", mismatched).verdict.value == (
        "needs_human_review"
    )


def test_v5_resume_none_after_revision_commit_ignores_mutated_view(tmp_path: Path) -> None:
    """After manual_plan_revision commits, resume(None) repairs from durable trees only."""
    from assurance_agent.workflow.core.events import read_events_strict
    from assurance_agent.workflow.graph.workspace import TreeStore
    from tests.integration._graph_fault_worker import (
        _REVISION_FIXTURES,
        _build_v5_revision,
        edit_recorded_revision_view,
        prepare_interrupted_v5_graph,
    )

    _runtime, _compiled, context, root_id = prepare_interrupted_v5_graph(tmp_path)
    committed_plan = b"# revised plan\n"
    edit_recorded_revision_view(root_id, committed_plan)
    fx = _REVISION_FIXTURES[root_id]
    change = fx["change"]
    assert isinstance(change, Path)
    project = context.project_root
    view = change / str(fx["revision_view"])

    kill_sync = tmp_path / "sync" / "post-commit-mutate"
    _spawn_resume_and_kill(
        project,
        kill_sync,
        point="manual_plan_revision_append",
        invocation_id=root_id,
    )
    events_after_kill = read_events_strict(change)
    revisions_after_kill = [e for e in events_after_kill if e.get("type") == "manual_plan_revision"]
    assert len(revisions_after_kill) == 1
    assert not any(
        e.get("type") == "graph_resumed" and e.get("revision_transition_id") is not None
        for e in events_after_kill
    )
    target_tree = str(revisions_after_kill[0]["target_tree_id"])
    store = TreeStore(change)
    assert store.read_bytes(target_tree, "change:plans/synth-plan.md") == committed_plan

    # Mutate the mutable revision view after the durable commit.
    (view / "plans" / "synth-plan.md").write_bytes(b"# mutated after commit\n")

    runtime2, _compiled2, _change2 = _build_v5_revision(project)
    done = runtime2.resume(root_id, None)
    assert done.status.status == "completed", done.reason

    events = read_events_strict(change)
    revisions = [e for e in events if e.get("type") == "manual_plan_revision"]
    assert len(revisions) == 1
    assert revisions[0]["target_tree_id"] == target_tree
    assert store.read_bytes(target_tree, "change:plans/synth-plan.md") == committed_plan
    resumes = [
        e
        for e in events
        if e.get("type") == "graph_resumed"
        and e.get("revision_transition_id") == revisions[0]["revision_transition_id"]
    ]
    assert [e.get("revision_ordinal") for e in resumes] == [0, 1, 2]


@pytest.mark.parametrize("point", REVISION_FAULT_POINTS)
def test_v5_revision_fault_prefix_recovers_without_duplicates(tmp_path: Path, point: str) -> None:
    from assurance_agent.workflow.core.events import read_events_strict
    from tests.integration._graph_fault_worker import (
        _REVISION_FIXTURES,
        edit_recorded_revision_view,
        prepare_interrupted_v5_graph,
    )

    _runtime, _compiled, context, root_id = prepare_interrupted_v5_graph(tmp_path)
    edit_recorded_revision_view(root_id, b"# revised plan\n")
    project = context.project_root
    change = _REVISION_FIXTURES[root_id]["change"]
    assert isinstance(change, Path)

    kill_sync = tmp_path / "sync" / point
    _spawn_resume_and_kill(project, kill_sync, point=point, invocation_id=root_id)
    events_after_kill = read_events_strict(change)
    revisions_after_kill = [e for e in events_after_kill if e.get("type") == "manual_plan_revision"]
    resumes_after_kill = [
        e
        for e in events_after_kill
        if e.get("type") == "graph_resumed" and e.get("revision_transition_id") is not None
    ]
    if point == "revision_target_objects":
        assert revisions_after_kill == []
    elif point == "manual_plan_revision_append":
        assert len(revisions_after_kill) == 1
        assert resumes_after_kill == []
    else:
        assert len(revisions_after_kill) == 1
        expected_prefix = int(point.rsplit("_", 1)[-1]) + 1
        assert len(resumes_after_kill) == expected_prefix

    resume_sync = tmp_path / "sync" / f"{point}-resume"
    completed = _run_worker(
        project,
        resume_sync,
        mode="resume",
        schema="v5_revision",
        invocation_id=root_id,
        timeout=60.0,
        resume_reason="revise synth plan",
        resume_who="reviewer",
    )
    assert completed.returncode == 0, completed.stderr
    events = read_events_strict(change)
    revisions = [e for e in events if e.get("type") == "manual_plan_revision"]
    assert len(revisions) == 1
    transition_id = revisions[0]["revision_transition_id"]
    resumes = [
        e
        for e in events
        if e.get("type") == "graph_resumed" and e.get("revision_transition_id") == transition_id
    ]
    assert [e.get("revision_ordinal") for e in resumes] == [0, 1, 2]
    assert (resume_sync / "DONE").read_text(encoding="utf-8").split(":", 2)[1] == "completed"


@pytest.mark.parametrize("corruption", ["gap", "reorder", "duplicate", "altered_payload"])
def test_v5_revision_prefix_conflict_fails_before_planner(tmp_path: Path, corruption: str) -> None:
    from assurance_agent.workflow.core.events import read_events_strict
    from assurance_agent.workflow.core.progression import transaction
    from assurance_agent.workflow.graph.runtime import GraphIntegrityError
    from tests.integration._graph_fault_worker import (
        _REVISION_FIXTURES,
        edit_recorded_revision_view,
        fix_and_proceed_command,
        prepare_interrupted_v5_graph,
    )

    runtime, _compiled, context, root_id = prepare_interrupted_v5_graph(tmp_path)
    edit_recorded_revision_view(root_id, b"# revised plan\n")
    change = _REVISION_FIXTURES[root_id]["change"]
    assert isinstance(change, Path)
    project = context.project_root

    kill_sync = tmp_path / "sync" / f"conflict-{corruption}"
    _spawn_resume_and_kill(
        project,
        kill_sync,
        point="graph_resumed_ordinal_0",
        invocation_id=root_id,
    )

    events = read_events_strict(change)
    resumes = [
        e for e in events if e.get("type") == "graph_resumed" and e.get("revision_transition_id") is not None
    ]
    assert len(resumes) == 1
    revision = next(e for e in events if e.get("type") == "manual_plan_revision")

    planner_calls: list[str] = []
    original_drive = runtime._drive  # noqa: SLF001

    def drive_guard(invocation_id, ctx):  # type: ignore[no-untyped-def]
        planner_calls.append("drive")
        return original_drive(invocation_id, ctx)

    runtime._drive = drive_guard  # type: ignore[method-assign]  # noqa: SLF001

    forged = {k: v for k, v in resumes[0].items() if k not in {"seq", "ts"}}
    if corruption == "gap":
        forged["revision_ordinal"] = 2
        forged["invocation_id"] = revision["invocation_id"]
    elif corruption == "reorder":
        forged["revision_ordinal"] = 1
    elif corruption == "duplicate":
        pass
    else:
        forged["reason"] = "tampered reason"
    with transaction(change) as txn:
        txn.append_strict(forged)

    with pytest.raises(GraphIntegrityError, match="manual_plan_revision_prefix_conflict"):
        runtime.resume(root_id, None)
    assert planner_calls == []
    with pytest.raises(GraphIntegrityError, match="manual_plan_revision_prefix_conflict"):
        runtime.resume(root_id, fix_and_proceed_command(root_id))
    assert planner_calls == []
