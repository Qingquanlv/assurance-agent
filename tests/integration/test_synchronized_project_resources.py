from __future__ import annotations

import hashlib
import json
import multiprocessing
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _synchronized_change_worker(
    project_root: str,
    change_id: str,
    invocation_id: str,
    base_tree_id: str,
    start_barrier,
    results,
) -> None:  # multiprocessing proxies intentionally stay runtime-typed
    from assurance_agent.workflow.core.events import read_events_strict
    from assurance_agent.workflow.graph.checkpoint import CheckpointStore, project_invocation
    from assurance_agent.workflow.graph.contracts import ResourceClaims, ResourcePath
    from assurance_agent.workflow.graph.models import (
        ExecutableTask,
        PlanResult,
        RuntimeContext,
        TaskResult,
    )
    from assurance_agent.workflow.graph.project_locks import ProjectResourceLockManager
    from assurance_agent.workflow.graph.scheduler import Scheduler
    from assurance_agent.workflow.graph.schema_v2 import (
        BackoffDef,
        RetryPolicyDef,
        TimeoutPolicyDef,
    )
    from assurance_agent.workflow.graph.workspace import TreeStore, WorkspaceBackend, WorkspaceError

    try:
        project = Path(project_root)
        change = project / "qa/changes" / change_id
        store = TreeStore(change)
        synchronized = (ResourcePath.parse("project:qa/issues/**"),)
        task_id = f"increment-{change_id}"
        task = ExecutableTask(
            task_id=task_id,
            invocation_id=invocation_id,
            checkpoint_ns=invocation_id,
            graph_id="main",
            node_id="increment",
            structural_path="main",
            input={"with": {}, "outputs": ["project:qa/issues/ISSUE-1.json"]},
            input_sha256=f"input-{change_id}",
            contract_digest="contract-1",
            retryable_errors=("conflict",),
            retry_policy=RetryPolicyDef(
                max_attempts=2,
                retry_on=["conflict"],
                backoff=BackoffDef(
                    initial_seconds=0.01,
                    multiplier=1.0,
                    max_seconds=0.01,
                    jitter=False,
                ),
            ),
            timeout_policy=TimeoutPolicyDef(run_seconds=10.0, heartbeat_seconds=0.1),
            target="operation:increment-issue",
            resources=ResourceClaims(
                reads=synchronized,
                writes=synchronized,
                synchronized=synchronized,
                exclusive=("project:issue-registry",),
                authorization_writes=synchronized,
            ),
            topology_rank=0,
            declaration_index=0,
        )

        class _IncrementRunner:
            def execute(self, task, workspace, context):
                issue = workspace.project_root / "qa/issues/ISSUE-1.json"
                payload = json.loads(issue.read_text(encoding="utf-8"))
                payload["version"] += 1
                payload["events"].append(change_id)
                # Keep the real fcntl lock held long enough to force process contention.
                time.sleep(0.15)
                issue.write_text(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                return TaskResult(status="succeeded")

        drift: list[str] = []
        targeted_apply = store.apply_write_sets_to_synchronized_paths

        def observed_apply(project_root, write_sets, paths):
            try:
                return targeted_apply(project_root, write_sets, paths)
            except WorkspaceError as exc:
                drift.append(str(exc))
                raise

        store.apply_write_sets_to_synchronized_paths = observed_apply  # type: ignore[method-assign]
        scheduler = Scheduler(
            checkpoints=CheckpointStore(change),
            object_store=store,
            workspace_backend=WorkspaceBackend(change),
            node_runner=_IncrementRunner(),
            project_lock_manager=ProjectResourceLockManager(project),
            project_lock_timeout_seconds=3.0,
        )
        projection = project_invocation(change, invocation_id).model_copy(
            update={
                "current_tree_id": base_tree_id,
                "root_tree_id": base_tree_id,
                "graph_digest": "g" * 64,
            }
        )
        context = RuntimeContext(
            project_root=project,
            repo_root=project,
            change_dir=change,
            change_id=change_id,
        )
        plan = PlanResult(
            superstep_id=f"ss-{change_id}",
            checkpoint_id="bootstrap",
            tasks=(task,),
        )
        start_barrier.wait(timeout=5.0)
        result = scheduler.execute(plan, projection, context)
        committed = sum(
            event.get("type") == "superstep_committed" for event in read_events_strict(change)
        )
        results.put(
            {
                "change_id": change_id,
                "committed": committed,
                "drift": drift,
                "failed": list(result.failed),
                "succeeded": list(result.succeeded),
            }
        )
    except BaseException as exc:
        results.put({"change_id": change_id, "error": repr(exc)})
        raise


class _AdvancingClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def now(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=timezone.utc)

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def _lock_process(
    project_root: str,
    label: str,
    acquired,
    holder_releasing,
    results,
) -> None:  # type annotations for multiprocessing proxies obscure the behavior under test
    from assurance_agent.workflow.graph.project_locks import ProjectResourceLockManager

    manager = ProjectResourceLockManager(Path(project_root))
    with manager.acquire(("project:issue-registry",), timeout_seconds=3.0):
        results.put((label, "acquired", holder_releasing.is_set()))
        acquired.set()
        if label == "first":
            time.sleep(0.2)
            holder_releasing.set()


def test_project_resource_locks_use_sorted_deterministic_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    try:
        from assurance_agent.workflow.graph import project_locks as lock_module
    except ImportError:
        pytest.fail("ProjectResourceLockManager is missing")

    project = tmp_path / "project"
    project.mkdir()
    manager = lock_module.ProjectResourceLockManager(project)
    observed: list[str] = []
    acquire_one = manager._acquire_one

    def record_acquire(handle, *, token: str, deadline: float) -> None:
        observed.append(token)
        acquire_one(handle, token=token, deadline=deadline)

    monkeypatch.setattr(manager, "_acquire_one", record_acquire)
    with manager.acquire(
        ("project:zeta", "project:alpha", "project:zeta"), timeout_seconds=0.0
    ):
        pass

    expected = [
        hashlib.sha256(f"{project.resolve()}\0{token}".encode()).hexdigest()
        for token in ("project:alpha", "project:zeta")
    ]
    assert observed == ["project:alpha", "project:zeta"]
    assert sorted(path.name for path in (project / "qa/.graph-runtime/locks").iterdir()) == sorted(
        expected
    )


def test_project_resource_lock_timeout_is_conflict(tmp_path: Path) -> None:
    try:
        from assurance_agent.workflow.graph.project_locks import (
            ProjectResourceConflict,
            ProjectResourceLockManager,
        )
    except ImportError:
        pytest.fail("ProjectResourceLockManager is missing")

    project = tmp_path / "project"
    project.mkdir()
    first = ProjectResourceLockManager(project)
    clock = _AdvancingClock()
    second = ProjectResourceLockManager(project, clock=clock, poll_interval_seconds=0.01)

    with first.acquire(("project:issue-registry",), timeout_seconds=1.0):
        with pytest.raises(ProjectResourceConflict) as caught:
            with second.acquire(("project:issue-registry",), timeout_seconds=0.03):
                pytest.fail("contending manager acquired a held lock")

    assert caught.value.error_kind == "conflict"
    assert clock.value == pytest.approx(0.03)


def test_project_resource_locks_release_after_exception(tmp_path: Path) -> None:
    try:
        from assurance_agent.workflow.graph.project_locks import ProjectResourceLockManager
    except ImportError:
        pytest.fail("ProjectResourceLockManager is missing")

    project = tmp_path / "project"
    project.mkdir()
    manager = ProjectResourceLockManager(project)
    with pytest.raises(RuntimeError, match="handler failed"):
        with manager.acquire(("project:issue-registry",), timeout_seconds=0.0):
            raise RuntimeError("handler failed")

    with ProjectResourceLockManager(project).acquire(
        ("project:issue-registry",), timeout_seconds=0.0
    ):
        pass


def test_project_resource_locks_exclude_two_real_processes(tmp_path: Path) -> None:
    try:
        from assurance_agent.workflow.graph.project_locks import ProjectResourceLockManager
    except ImportError:
        pytest.fail("ProjectResourceLockManager is missing")
    assert ProjectResourceLockManager is not None

    project = tmp_path / "project"
    project.mkdir()
    process_context = multiprocessing.get_context("spawn")
    first_acquired = process_context.Event()
    second_acquired = process_context.Event()
    holder_releasing = process_context.Event()
    results = process_context.Queue()
    first = process_context.Process(
        target=_lock_process,
        args=(str(project), "first", first_acquired, holder_releasing, results),
    )
    second = process_context.Process(
        target=_lock_process,
        args=(str(project), "second", second_acquired, holder_releasing, results),
    )

    first.start()
    assert first_acquired.wait(3.0)
    second.start()
    first.join(5.0)
    second.join(5.0)

    assert first.exitcode == 0
    assert second.exitcode == 0
    # Drain explicitly so the assertion is independent of process completion order.
    records = {}
    for _ in range(2):
        label, state, saw_release = results.get(timeout=1.0)
        records[label] = (state, saw_release)
    assert records["first"] == ("acquired", False)
    assert records["second"] == ("acquired", True)


def test_two_changes_apply_exact_synchronized_updates_without_lost_events(
    tmp_path: Path,
) -> None:
    from assurance_agent.workflow.core.events import append_event_strict
    from assurance_agent.workflow.graph.workspace import TreeStore

    project = tmp_path / "project"
    issue = project / "qa/issues/ISSUE-1.json"
    issue.parent.mkdir(parents=True)
    issue.write_text('{"events":[],"version":0}\n', encoding="utf-8")
    change_ids = ("CH-A", "CH-B")
    for change_id in change_ids:
        (project / "qa/changes" / change_id).mkdir(parents=True)

    bases: dict[str, tuple[str, str]] = {}
    for change_id in change_ids:
        change = project / "qa/changes" / change_id
        invocation_id = f"inv-{change_id}"
        tree_id = TreeStore(change).capture(project)
        append_event_strict(
            change,
            {
                "source": "graph",
                "type": "graph_invocation_started",
                "invocation_id": invocation_id,
                "entrypoint": "full",
                "graph_id": "main",
                "graph_digest": "g" * 64,
                "contract_digests": {},
                "params": {},
                "params_sha256": "p" * 64,
                "root_tree_id": tree_id,
                "max_parallel_tasks": 1,
                "checkpoint_ns": invocation_id,
                "structural_path": "main",
            },
        )
        bases[change_id] = (invocation_id, tree_id)

    process_context = multiprocessing.get_context("spawn")
    start_barrier = process_context.Barrier(2)
    results = process_context.Queue()
    processes = [
        process_context.Process(
            target=_synchronized_change_worker,
            args=(
                str(project),
                change_id,
                bases[change_id][0],
                bases[change_id][1],
                start_barrier,
                results,
            ),
        )
        for change_id in change_ids
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10.0)

    assert [process.exitcode for process in processes] == [0, 0]
    reports = {}
    for _ in processes:
        report = results.get(timeout=1.0)
        reports[report["change_id"]] = report
    assert not any("error" in report for report in reports.values())
    for change_id in change_ids:
        assert reports[change_id]["succeeded"] == [f"increment-{change_id}"]
        assert reports[change_id]["failed"] == []
        assert reports[change_id]["committed"] == 1
        assert reports[change_id]["drift"] == []

    final_issue = json.loads(issue.read_text(encoding="utf-8"))
    assert final_issue["version"] == 2
    assert set(final_issue["events"]) == {"CH-A", "CH-B"}
