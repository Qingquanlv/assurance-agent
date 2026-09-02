from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from bootstrap_fixtures import synthetic_invocation_started
from graph_engine.runtime import production_host
from graph_engine.runtime import production_worker
from graph_engine.canonical import canonical_digest
from graph_engine.composition.lock import pinned_execution_host_lock
from graph_engine.plugin_api import (
    InvocationMetadata,
    ResourceClaims,
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskContext,
    TaskOutcome,
    TaskRequest,
    TaskWorkspaceBinding,
)
from graph_engine.attempts.host_protocol import (
    AttemptRootDescriptor,
    TaskActivityRpcIdentity,
    TaskHostCallIdentity,
    TaskHostCancelCall,
    TaskHostExecuteCall,
    TaskHostReconcileCall,
    TaskHostTerminalReceipt,
)
from graph_engine.runtime.events import (
    GraphStarted,
    NodeActivated,
    TaskActivityPrepared,
    TaskAttemptStarted,
    TaskLeaseAcquired,
    TokenConsumed,
    TokenOffered,
)
from graph_engine.attempts.host_receipts import (
    TerminalReceiptError,
    TerminalReceiptStore,
    prove_call_quiescent,
)
from graph_engine.attempts.activity import LedgerTaskActivityPort
from graph_engine.runtime.ledger import Ledger
from graph_engine.attempts.production_host import ProductionHostError, _ProductionTaskExecutionHost
from graph_engine.attempts.secret_sources import empty_runtime_authorization
from graph_engine.attempts.workspace import TaskWorkspaceStore


def _write_handler(tmp_path: Path, *, class_name: str, body: str) -> tuple[str, tuple[str, ...]]:
    module_path = tmp_path / f"{class_name.lower()}.py"
    module_path.write_text(
        "from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest\n\n"
        f"class {class_name}:\n"
        f"{textwrap.indent(body, '    ')}\n",
        encoding="utf-8",
    )
    return f"{module_path.stem}:{class_name}.execute", (str(tmp_path),)


class _EchoHandler:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request
        (context.write_root / "done.txt").write_text("ok\n", encoding="utf-8")
        return TaskOutcome.succeeded({"ok": True})


class _RecoverableEcho:
    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del request, context
        return TaskOutcome.succeeded({"ok": True})

    async def reconcile(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityReconcileResult:
        del request, context, activity
        return TaskActivityReconcileResult(status="not_dispatched")

    async def cancel(
        self,
        request: TaskRequest,
        context: TaskContext,
        activity: TaskActivitySnapshot,
    ) -> TaskActivityCancelResult:
        del request, context, activity
        return TaskActivityCancelResult(status="acknowledged")


def _request() -> TaskRequest:
    return TaskRequest(
        invocation_id="inv-1",
        task_id="task-1",
        graph_instance_id="graph-1",
        node_id="run",
        capability_id="test.echo.run",
        target_capability_id="test.echo.run",
        binding_data={},
        resource_ids=(),
        resource_digests={},
        resources=ResourceClaims(),
        invocation=InvocationMetadata(
            invocation_id="inv-1",
            lock_digest="a" * 64,
            composition_digest=canonical_digest({"lock_digest": "a" * 64}),
            entrypoint="main",
        ),
        attempt=1,
        input={},
    )


def _task_workspace_store(tmp_path: Path, *, name: str = "workspace") -> TaskWorkspaceStore:
    root = tmp_path / name
    project_root = root / "project"
    project_root.mkdir(parents=True)
    return TaskWorkspaceStore(
        project_root,
        root / "attempts",
        root / "promotion-receipts",
    )


def _begin_workspace(store: TaskWorkspaceStore) -> TaskWorkspaceBinding:
    return store.begin(
        task_id="task-1",
        attempt=1,
        output_paths=("after.txt", "done.txt", "held.txt", "orphan.pid"),
    )


def _execute_call(
    *,
    workspace: TaskWorkspaceBinding,
    capability_id: str = "test.echo.run",
    entrypoint: str = "echo_handler:EchoHandler.execute",
    activity_id: str | None = None,
    timeout_seconds: float = 30.0,
) -> TaskHostExecuteCall:
    host = pinned_execution_host_lock()
    return TaskHostExecuteCall(
        identity=TaskHostCallIdentity(
            invocation_id="inv-1",
            task_id="task-1",
            activation_id="activation-run",
            attempt=1,
            activity_id=activity_id,
            operation="execute",
            host_implementation_id=host.implementation_id,
            host_implementation_digest=host.implementation_digest,
        ),
        capability_id=capability_id,
        capability_entrypoint=entrypoint,
        request=_request().model_copy(
            update={"capability_id": capability_id, "target_capability_id": capability_id}
        ),
        attempt_root=_attempt_root(workspace),
        activity_rpc=TaskActivityRpcIdentity(
            invocation_id="inv-1",
            task_id="task-1",
            activation_id="activation-run",
            attempt=1,
            activity_id=activity_id,
        ),
        authorized_secret_handles=(),
        timeout_seconds=timeout_seconds,
    )


def _attempt_root(workspace: TaskWorkspaceBinding) -> AttemptRootDescriptor:
    return AttemptRootDescriptor(
        workspace_identity=workspace.identity,
        project_root_identity=workspace.project_root_identity,
        write_root_identity=workspace.write_root_identity,
        project_root_digest=workspace.identity.project_digest,
        write_root_digest=workspace.identity.write_root_digest,
        baseline_digest=canonical_digest(
            [item.model_dump(mode="json") for item in workspace.identity.baseline_files]
        ),
    )


def _activity_snapshot(workspace: TaskWorkspaceBinding) -> TaskActivitySnapshot:
    fingerprint = {"endpoint": "https://example.test"}
    return TaskActivitySnapshot(
        activity_id="activity-1",
        request_digest="2" * 64,
        workspace_identity=workspace.identity,
        dispatch_fingerprint=fingerprint,
        dispatch_fingerprint_digest=canonical_digest(fingerprint),
        state="dispatch_started",
    )


def _reconcile_call(*, workspace: TaskWorkspaceBinding, entrypoint: str) -> TaskHostReconcileCall:
    host = pinned_execution_host_lock()
    identity = TaskHostCallIdentity(
        invocation_id="inv-1",
        task_id="task-1",
        activation_id="activation-run",
        attempt=1,
        activity_id="activity-1",
        operation="reconcile",
        host_implementation_id=host.implementation_id,
        host_implementation_digest=host.implementation_digest,
    )
    return TaskHostReconcileCall(
        identity=identity,
        capability_id="test.echo.run",
        capability_entrypoint=entrypoint,
        request=_request(),
        attempt_root=_attempt_root(workspace),
        activity_rpc=TaskActivityRpcIdentity(
            invocation_id="inv-1",
            task_id="task-1",
            activation_id="activation-run",
            attempt=1,
            activity_id="activity-1",
        ),
        authorized_secret_handles=(),
        activity=_activity_snapshot(workspace),
    )


def _cancel_call(*, workspace: TaskWorkspaceBinding, entrypoint: str) -> TaskHostCancelCall:
    host = pinned_execution_host_lock()
    identity = TaskHostCallIdentity(
        invocation_id="inv-1",
        task_id="task-1",
        activation_id="activation-run",
        attempt=1,
        activity_id="activity-1",
        operation="cancel",
        host_implementation_id=host.implementation_id,
        host_implementation_digest=host.implementation_digest,
    )
    return TaskHostCancelCall(
        identity=identity,
        capability_id="test.echo.run",
        capability_entrypoint=entrypoint,
        request=_request(),
        attempt_root=_attempt_root(workspace),
        activity_rpc=TaskActivityRpcIdentity(
            invocation_id="inv-1",
            task_id="task-1",
            activation_id="activation-run",
            attempt=1,
            activity_id="activity-1",
        ),
        authorized_secret_handles=(),
        activity=_activity_snapshot(workspace),
    )


def _record_real_worker_spawns(
    monkeypatch: pytest.MonkeyPatch,
) -> list[production_host._WorkerProcess]:
    workers: list[production_host._WorkerProcess] = []
    original_spawn = production_host._ProcessSupervisor.spawn

    def recording_spawn(
        self: production_host._ProcessSupervisor,
        *,
        attempt_root: Path,
        call_digest: str,
    ) -> production_host._WorkerProcess:
        worker = original_spawn(self, attempt_root=attempt_root, call_digest=call_digest)
        workers.append(worker)
        return worker

    monkeypatch.setattr(production_host._ProcessSupervisor, "spawn", recording_spawn)
    return workers


def _process_is_running(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    return True


def _ledger_activity_snapshot(
    root: Path,
    call: TaskHostExecuteCall,
) -> TaskActivitySnapshot:
    ledger = Ledger(root / "invocations" / call.identity.invocation_id / "ledger")
    return LedgerTaskActivityPort(ledger=ledger, identity=call.activity_rpc).snapshot


def test_production_host_rejects_wrong_workspace(tmp_path: Path) -> None:
    store = _task_workspace_store(tmp_path)
    workspace = _begin_workspace(store)
    entrypoint, roots = _write_handler(
        tmp_path,
        class_name="EchoHandler",
        body="async def execute(self, request, context):\n    return TaskOutcome.succeeded({'ok': True})",
    )
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _EchoHandler()},
        store=store,
        handler_import_roots={"test.echo.run": roots},
    )
    workspace.write_root.rmdir()
    call = _execute_call(workspace=workspace, entrypoint=entrypoint)
    with pytest.raises(ProductionHostError, match="attempt workspace"):
        asyncio.run(host.execute(call))


def test_unrelated_workspace_holder_does_not_block_quiescence(tmp_path: Path) -> None:
    store = _task_workspace_store(tmp_path)
    workspace = _begin_workspace(store)
    held = workspace.write_root / "held.txt"
    held.write_text("open\n", encoding="utf-8")
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; open(r'''" + str(held) + "'''); time.sleep(60)"],
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and holder.poll() is not None:
            time.sleep(0.05)
        assert holder.poll() is None
        entrypoint, roots = _write_handler(
            tmp_path,
            class_name="EchoHandler",
            body="async def execute(self, request, context):\n    return TaskOutcome.succeeded({'ok': True})",
        )
        host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
        host.bind_invocation_runtime(
            handlers={"test.echo.run": _EchoHandler()},
            store=store,
            handler_import_roots={"test.echo.run": roots},
        )
        result = asyncio.run(host.execute(_execute_call(workspace=workspace, entrypoint=entrypoint)))
        assert result.outcome is not None
        assert result.outcome.status == "succeeded"
    finally:
        holder.kill()
        holder.wait(timeout=2)


def test_workspace_writer_probe_retries_a_transient_lsof_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeouts: list[float] = []

    def run_lsof(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args
        timeout = float(kwargs["timeout"])
        timeouts.append(timeout)
        if len(timeouts) == 1:
            raise subprocess.TimeoutExpired("lsof", timeout)
        return subprocess.CompletedProcess(["lsof"], 0, stdout="", stderr="")

    monkeypatch.setattr(production_host.subprocess, "run", run_lsof)

    assert production_host._collect_workspace_writers(tmp_path) == ()
    assert timeouts == [2.0, 10.0]


def test_worker_rejects_substituted_project_root_before_handler_execution(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    foreign_project = tmp_path / "foreign-project"
    foreign_project.mkdir()
    store = TaskWorkspaceStore(project_root, tmp_path / "attempts-v2", tmp_path / "receipts-v2")
    executed = False

    class MustNotRun:
        async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
            nonlocal executed
            del request, context
            executed = True
            return TaskOutcome.succeeded()

    try:
        binding = store.begin(task_id="task-1", attempt=1, output_paths=("out.txt",))
        baseline_digest = canonical_digest(
            [item.model_dump(mode="json") for item in binding.identity.baseline_files]
        )
        descriptor = AttemptRootDescriptor.model_validate(
            {
                "schema_version": "2",
                "workspace_identity": binding.identity.model_dump(mode="json"),
                "project_root_identity": binding.project_root_identity.model_dump(mode="json"),
                "write_root_identity": binding.write_root_identity.model_dump(mode="json"),
                "project_root_digest": binding.identity.project_digest,
                "write_root_digest": binding.identity.write_root_digest,
                "baseline_digest": baseline_digest,
            }
        )
        call = _execute_call(workspace=binding).model_copy(update={"attempt_root": descriptor})
        with pytest.raises(Exception, match="project root|root identity|authenticated root"):
            asyncio.run(
                production_worker._run_call(
                    MustNotRun(),
                    "execute",
                    call,
                    project_root=foreign_project,
                    write_root=binding.write_root,
                    secrets=None,
                    activity_port=None,
                    cancel_requested=lambda: False,
                )
            )
    finally:
        store.close()

    assert executed is False


def test_leftover_process_group_child_still_fails_quiescence(tmp_path: Path) -> None:
    store = _task_workspace_store(tmp_path)
    workspace = _begin_workspace(store)
    entrypoint, roots = _write_handler(
        tmp_path,
        class_name="OrphanHandler",
        body=(
            "async def execute(self, request, context):\n"
            "    import subprocess, sys, time\n"
            "    held = context.write_root / 'held.txt'\n"
            "    held.write_text('open\\n', encoding='utf-8')\n"
            "    child = subprocess.Popen(\n"
            "        [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
            "    )\n"
            "    (context.write_root / 'orphan.pid').write_text(str(child.pid), encoding='utf-8')\n"
            "    time.sleep(0.2)\n"
            "    return TaskOutcome.succeeded({'ok': True})\n"
        ),
    )
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _EchoHandler()},
        store=store,
        handler_import_roots={"test.echo.run": roots},
    )
    try:
        with pytest.raises(TerminalReceiptError, match="not quiescent"):
            asyncio.run(host.execute(_execute_call(workspace=workspace, entrypoint=entrypoint)))
    finally:
        pid_path = workspace.write_root / "orphan.pid"
        if pid_path.is_file():
            try:
                os.kill(int(pid_path.read_text(encoding="utf-8")), 9)
            except OSError:
                pass


def test_production_host_rejects_forged_terminal_receipt(tmp_path: Path) -> None:
    store = TerminalReceiptStore.create(tmp_path / "receipts")
    identity = TaskHostCallIdentity(
        invocation_id="inv-1",
        task_id="task-1",
        activation_id="activation-run",
        attempt=1,
        activity_id="activity-1",
        operation="execute",
        host_implementation_id="graph.engine.task-host",
        host_implementation_digest="f" * 64,
    )
    sink = store.sink_for(identity)
    outcome = TaskOutcome.succeeded({"ok": True})
    receipt = TaskHostTerminalReceipt(
        host_implementation_digest=identity.host_implementation_digest,
        invocation_id="inv-2",
        task_id=identity.task_id,
        activation_id=identity.activation_id,
        attempt=identity.attempt,
        activity_id=identity.activity_id,
        operation=identity.operation,
        request_digest="0" * 64,
        workspace_identity_digest="1" * 64,
        project_root_digest="2" * 64,
        write_root_digest="3" * 64,
        baseline_digest="4" * 64,
        staged_write_set_digest="5" * 64,
        outcome=outcome,
        outcome_digest=canonical_digest(outcome.model_dump(mode="json")),
        quiescence_proof_digest=prove_call_quiescent(),
        host_call_id=sink.host_call_id,
    )
    with pytest.raises(TerminalReceiptError, match="foreign terminal receipt"):
        sink.install(receipt)


def test_production_host_worker_crash_before_response(tmp_path: Path) -> None:
    store = _task_workspace_store(tmp_path)
    workspace = _begin_workspace(store)
    entrypoint, roots = _write_handler(
        tmp_path,
        class_name="MissingHandler",
        body="async def execute(self, request, context):\n    raise RuntimeError('worker crash')",
    )
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _EchoHandler()},
        store=store,
        handler_import_roots={"test.echo.run": roots},
    )
    with pytest.raises(ProductionHostError, match="worker (exited|control stream closed)"):
        asyncio.run(host.execute(_execute_call(workspace=workspace, entrypoint=entrypoint)))


def test_production_host_reconcile_from_installed_receipt(tmp_path: Path) -> None:
    store = _task_workspace_store(tmp_path)
    workspace = _begin_workspace(store)
    receipts = TerminalReceiptStore.create(tmp_path / "receipts")
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _RecoverableEcho()},
        store=store,
        receipts=receipts,
    )
    reconcile = _reconcile_call(
        workspace=workspace,
        entrypoint="recoverable:RecoverableEcho.reconcile",
    )
    outcome = TaskOutcome.failed("transient", "already finished")
    sink = receipts.sink_for(reconcile.identity.model_copy(update={"operation": "execute"}))
    sink.install(
        TaskHostTerminalReceipt(
            host_implementation_digest=reconcile.identity.host_implementation_digest,
            wire_schema_version=reconcile.identity.wire_schema_version,
            invocation_id=reconcile.identity.invocation_id,
            task_id=reconcile.identity.task_id,
            activation_id=reconcile.identity.activation_id,
            attempt=reconcile.identity.attempt,
            activity_id=reconcile.identity.activity_id,
            operation="execute",
            request_digest=reconcile.activity.request_digest,
            workspace_identity_digest=workspace.identity.identity_digest,
            project_root_digest=workspace.identity.project_digest,
            write_root_digest=workspace.identity.write_root_digest,
            baseline_digest=reconcile.attempt_root.baseline_digest,
            staged_write_set_digest=store.seal(workspace.identity).staged_digest,
            dispatch_fingerprint_digest=reconcile.activity.dispatch_fingerprint_digest,
            reference_digest=reconcile.activity.reference_digest,
            outcome=outcome,
            outcome_digest=canonical_digest(outcome.model_dump(mode="json")),
            terminal_proof_digest=None,
            quiescence_proof_digest=prove_call_quiescent(),
            host_call_id=sink.host_call_id,
        )
    )
    result = asyncio.run(host.reconcile(reconcile))
    assert result.reconcile_result is not None
    assert result.reconcile_result.status == "terminal"
    assert result.reconcile_result.outcome == outcome


def test_production_host_cancel_runs_through_worker(tmp_path: Path) -> None:
    store = _task_workspace_store(tmp_path)
    workspace = _begin_workspace(store)
    entrypoint, roots = _write_handler(
        tmp_path,
        class_name="RecoverableEcho",
        body=(
            "async def cancel(self, request, context, activity):\n"
            "    from graph_engine.plugin_api import TaskActivityCancelResult\n"
            "    return TaskActivityCancelResult(status='acknowledged')"
        ),
    )
    cancel_entrypoint = entrypoint.replace(".execute", ".cancel")
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _RecoverableEcho()},
        store=store,
        handler_import_roots={"test.echo.run": roots},
    )
    result = asyncio.run(host.cancel(_cancel_call(workspace=workspace, entrypoint=cancel_entrypoint)))
    assert result.cancel_result is not None
    assert result.cancel_result.status == "acknowledged"


def test_production_host_parent_alive_pipe_is_wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from graph_engine.runtime import production_host as module

    captured: dict[str, int] = {}

    original_spawn = module._ProcessSupervisor.spawn

    def recording_spawn(self: module._ProcessSupervisor, *, attempt_root: Path, call_digest: str) -> object:
        worker = original_spawn(self, attempt_root=attempt_root, call_digest=call_digest)
        captured["parent_alive_w"] = worker.parent_alive_w
        return worker

    monkeypatch.setattr(module._ProcessSupervisor, "spawn", recording_spawn)
    store = _task_workspace_store(tmp_path)
    workspace = _begin_workspace(store)
    entrypoint, roots = _write_handler(
        tmp_path,
        class_name="EchoHandler",
        body="async def execute(self, request, context):\n    return TaskOutcome.succeeded({'ok': True})",
    )
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _EchoHandler()},
        store=store,
        handler_import_roots={"test.echo.run": roots},
    )
    asyncio.run(host.execute(_execute_call(workspace=workspace, entrypoint=entrypoint)))
    assert captured["parent_alive_w"] >= 0


def test_spawn_failure_after_popen_closes_every_pipe_and_reaps_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt_root = tmp_path / "attempt"
    attempt_root.mkdir()
    opened_pipes: list[tuple[int, int]] = []
    started: list[subprocess.Popen[bytes]] = []
    original_pipe = os.pipe
    original_popen = production_host.subprocess.Popen

    def recording_pipe() -> tuple[int, int]:
        descriptors = original_pipe()
        opened_pipes.append(descriptors)
        return descriptors

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = original_popen(*args, **kwargs)
        started.append(process)
        return process

    def fail_after_popen(fault_id: str) -> None:
        if fault_id == "host-after-popen-before-worker-return":
            raise RuntimeError(fault_id)

    monkeypatch.setattr(production_host.os, "pipe", recording_pipe)
    monkeypatch.setattr(production_host.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(production_host, "_host_fault_cut", fail_after_popen)

    try:
        with pytest.raises(RuntimeError, match="host-after-popen-before-worker-return"):
            production_host._ProcessSupervisor.for_platform().spawn(
                attempt_root=attempt_root,
                call_digest="d" * 64,
            )

        assert len(started) == 1
        assert started[0].wait(timeout=3.0) is not None
        assert started[0].stdin is not None and started[0].stdin.closed
        assert started[0].stdout is not None and started[0].stdout.closed
        assert started[0].stderr is not None and started[0].stderr.closed
        for descriptors in opened_pipes:
            for descriptor in descriptors:
                with pytest.raises(OSError):
                    os.fstat(descriptor)
    finally:
        for process in started:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3.0)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        for descriptors in opened_pipes:
            for descriptor in descriptors:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


@pytest.mark.parametrize("failing_pipe_call", [2, 3])
def test_pipe_setup_failure_closes_every_previously_created_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_pipe_call: int,
) -> None:
    attempt_root = tmp_path / "attempt"
    attempt_root.mkdir()
    opened: list[tuple[int, int]] = []
    original_pipe = os.pipe

    def fail_during_pipe_setup() -> tuple[int, int]:
        if len(opened) + 1 == failing_pipe_call:
            raise OSError("pipe setup failed")
        descriptors = original_pipe()
        opened.append(descriptors)
        return descriptors

    monkeypatch.setattr(production_host.os, "pipe", fail_during_pipe_setup)

    try:
        with pytest.raises(OSError, match="pipe setup failed"):
            production_host._ProcessSupervisor.for_platform().spawn(
                attempt_root=attempt_root,
                call_digest="d" * 64,
            )

        for descriptors in opened:
            for descriptor in descriptors:
                with pytest.raises(OSError):
                    os.fstat(descriptor)
    finally:
        for descriptors in opened:
            for descriptor in descriptors:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def test_partial_worker_control_frame_cannot_block_past_absolute_deadline(tmp_path: Path) -> None:
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb", buffering=0)
    session_key = b"k" * 32
    partial = production_host.encode_authenticated_frame(session_key, b"{}")[:20]
    os.write(write_fd, partial)
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    observed: list[BaseException] = []

    def read_partial_frame() -> None:
        try:
            host._read_frame_with_timeout(stream, session_key, timeout=0.1)
        except BaseException as error:
            observed.append(error)

    reader = threading.Thread(target=read_partial_frame, daemon=True)
    reader.start()
    reader.join(timeout=0.5)
    try:
        assert reader.is_alive() is False
        assert len(observed) == 1
        assert isinstance(observed[0], ProductionHostError)
        assert str(observed[0]) == "worker result pending"
    finally:
        os.close(write_fd)
        reader.join(timeout=1.0)
        stream.close()


@pytest.mark.parametrize("enumeration_failure", ["ps-timeout", "proc-unavailable"])
def test_descendant_enumeration_failure_still_reaps_worker_and_closes_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enumeration_failure: str,
) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        start_new_session=True,
    )
    worker = production_host._WorkerProcess(
        popen=process,
        process_group=process.pid,
        parent_alive_w=-1,
        activity_response_w=-1,
        cancel_w=-1,
        _stderr_chunks=[],
    )
    if enumeration_failure == "ps-timeout":
        monkeypatch.setattr(production_host.sys, "platform", "darwin")

        def timeout_ps(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired("ps", 0.1)

        monkeypatch.setattr(production_host.subprocess, "run", timeout_ps)
    else:
        monkeypatch.setattr(production_host.sys, "platform", "linux")

        def unavailable_proc(_path: str) -> list[str]:
            raise OSError("proc unavailable")

        monkeypatch.setattr(production_host.os, "listdir", unavailable_proc)

    try:
        with pytest.raises(TerminalReceiptError, match="cannot enumerate process descendants"):
            production_host._ProcessSupervisor.for_platform().cleanup(worker)

        assert process.wait(timeout=3.0) is not None
        assert process.stdin is not None and process.stdin.closed
        assert process.stdout is not None and process.stdout.closed
        assert process.stderr is not None and process.stderr.closed
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3.0)
        for process_stream in (process.stdin, process.stdout, process.stderr):
            if process_stream is not None:
                process_stream.close()


def test_cleanup_of_reaped_worker_without_live_group_members_never_signals_stale_pgid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        start_new_session=True,
    )
    process.wait(timeout=3.0)
    worker = production_host._WorkerProcess(
        popen=process,
        process_group=process.pid,
        parent_alive_w=-1,
        activity_response_w=-1,
        cancel_w=-1,
        _stderr_chunks=[],
    )
    monkeypatch.setattr(production_host, "_collect_process_tree_descendants", lambda _pid: ())
    monkeypatch.setattr(
        production_host,
        "_collect_process_group_descendants",
        lambda **_kwargs: (),
    )

    def reject_stale_group_signal(_process_group: int, _signal: int) -> None:
        raise AssertionError("cleanup signalled an unauthenticated stale process group")

    monkeypatch.setattr(production_host.os, "killpg", reject_stale_group_signal)

    production_host._ProcessSupervisor.for_platform().cleanup(worker)

    assert process.stdin is not None and process.stdin.closed
    assert process.stdout is not None and process.stdout.closed
    assert process.stderr is not None and process.stderr.closed


def test_parent_alive_read_error_kills_worker_instead_of_disabling_supervision() -> None:
    child_pid = os.fork()
    if child_pid == 0:
        os.setsid()
        production_worker._install_parent_death_supervision(2**30)
        time.sleep(2.0)
        os._exit(91)

    _child, status = os.waitpid(child_pid, 0)
    assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL


def test_explicit_normal_worker_shutdown_does_not_trigger_parent_death_kill() -> None:
    child_pid = os.fork()
    if child_pid == 0:
        os.setsid()
        read_fd, write_fd = os.pipe()
        supervisor = production_worker._install_parent_death_supervision(read_fd)
        supervisor.begin_normal_shutdown()
        os.close(read_fd)
        os.close(write_fd)
        time.sleep(0.2)
        os._exit(0)

    _child, status = os.waitpid(child_pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0


def test_parent_process_crash_with_live_descendants_cleans_group_and_leaves_no_receipt(
    tmp_path: Path,
) -> None:
    store = _task_workspace_store(tmp_path)
    workspace = _begin_workspace(store)
    receipts = TerminalReceiptStore.create(tmp_path / "terminal-receipts")
    entrypoint, roots = _write_handler(
        tmp_path,
        class_name="ParentCrashHandler",
        body=(
            "async def execute(self, request, context):\n"
            "    import asyncio, os, subprocess, sys\n"
            "    child = subprocess.Popen(\n"
            "        [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
            "        start_new_session=True,\n"
            "    )\n"
            "    (context.write_root / 'held.txt').write_text(str(os.getpid()), encoding='utf-8')\n"
            "    (context.write_root / 'orphan.pid').write_text(str(child.pid), encoding='utf-8')\n"
            "    while True:\n"
            "        await asyncio.sleep(0.1)"
        ),
    )
    call = _execute_call(
        workspace=workspace,
        entrypoint=entrypoint,
        activity_id="activity-1",
    )
    host_pid = os.fork()
    if host_pid == 0:
        host = _ProductionTaskExecutionHost(
            root=tmp_path,
            authorization=empty_runtime_authorization(),
        )
        host.bind_invocation_runtime(
            handlers={"test.echo.run": _EchoHandler()},
            store=store,
            receipts=receipts,
            handler_import_roots={"test.echo.run": roots},
        )
        asyncio.run(host.execute(call))
        os._exit(92)

    worker_pid: int | None = None
    descendant_pid: int | None = None
    host_reaped = False
    try:
        worker_path = workspace.write_root / "held.txt"
        descendant_path = workspace.write_root / "orphan.pid"
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if worker_path.is_file() and descendant_path.is_file():
                worker_pid = int(worker_path.read_text(encoding="utf-8"))
                descendant_pid = int(descendant_path.read_text(encoding="utf-8"))
                break
            time.sleep(0.05)
        assert worker_pid is not None
        assert descendant_pid is not None

        os.kill(host_pid, signal.SIGKILL)
        _child, status = os.waitpid(host_pid, 0)
        host_reaped = True
        assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and (
            _process_is_running(worker_pid) or _process_is_running(descendant_pid)
        ):
            time.sleep(0.05)

        assert _process_is_running(worker_pid) is False
        assert _process_is_running(descendant_pid) is False
        assert receipts.authenticate(call.identity) == ()
    finally:
        if not host_reaped:
            try:
                os.kill(host_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(host_pid, 0)
        if worker_pid is not None:
            try:
                os.killpg(worker_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if descendant_pid is not None:
            try:
                os.kill(descendant_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_successful_handler_cannot_leave_a_detached_descendant_or_install_a_receipt(
    tmp_path: Path,
) -> None:
    store = _task_workspace_store(tmp_path)
    workspace = _begin_workspace(store)
    receipts = TerminalReceiptStore.create(tmp_path / "terminal-receipts")
    entrypoint, roots = _write_handler(
        tmp_path,
        class_name="DetachedDescendantHandler",
        body=(
            "async def execute(self, request, context):\n"
            "    import subprocess, sys\n"
            "    child = subprocess.Popen(\n"
            "        [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
            "        start_new_session=True,\n"
            "    )\n"
            "    (context.write_root / 'detached.pid').write_text(str(child.pid), encoding='utf-8')\n"
            "    return TaskOutcome.succeeded({'ok': True})"
        ),
    )
    call = _execute_call(workspace=workspace, entrypoint=entrypoint, activity_id="activity-1")
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _EchoHandler()},
        store=store,
        receipts=receipts,
        handler_import_roots={"test.echo.run": roots},
    )

    with pytest.raises(ProductionHostError, match="live descendant"):
        asyncio.run(host.execute(call))

    descendant_pid = int((workspace.write_root / "detached.pid").read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _process_is_running(descendant_pid):
        time.sleep(0.05)
    try:
        assert _process_is_running(descendant_pid) is False
        assert receipts.authenticate(call.identity) == ()
    finally:
        try:
            os.kill(descendant_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_production_host_cancel_escalates_on_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from graph_engine.runtime import production_host as module

    monkeypatch.setattr(module, "_CALL_TIMEOUT_SECONDS", 0.2)
    store = _task_workspace_store(tmp_path)
    workspace = _begin_workspace(store)
    entrypoint, roots = _write_handler(
        tmp_path,
        class_name="SlowHandler",
        body=(
            "async def execute(self, request, context):\n"
            "    import asyncio\n"
            "    while True:\n"
            "        await asyncio.sleep(0.05)\n"
        ),
    )
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _EchoHandler()},
        store=store,
        handler_import_roots={"test.echo.run": roots},
    )
    with pytest.raises(ProductionHostError, match="timed out"):
        asyncio.run(
            host.execute(
                _execute_call(
                    workspace=workspace,
                    entrypoint=entrypoint,
                    timeout_seconds=0.2,
                )
            )
        )


def test_production_host_honors_call_timeout_longer_than_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graph_engine.runtime import production_host as module

    monkeypatch.setattr(module, "_CALL_TIMEOUT_SECONDS", 0.2)
    store = _task_workspace_store(tmp_path)
    workspace = _begin_workspace(store)
    entrypoint, roots = _write_handler(
        tmp_path,
        class_name="PauseHandler",
        body=(
            "async def execute(self, request, context):\n"
            "    import asyncio\n"
            "    await asyncio.sleep(0.4)\n"
            "    return TaskOutcome.succeeded({'ok': True})\n"
        ),
    )
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _EchoHandler()},
        store=store,
        handler_import_roots={"test.echo.run": roots},
    )
    result = asyncio.run(
        host.execute(
            _execute_call(
                workspace=workspace,
                entrypoint=entrypoint,
                timeout_seconds=2.0,
            )
        )
    )
    assert result.outcome is not None
    assert result.outcome.status == "succeeded"


def test_production_host_crash_after_receipt_leaves_durable_receipt(tmp_path: Path) -> None:
    store = _task_workspace_store(tmp_path)
    workspace = _begin_workspace(store)
    receipts = TerminalReceiptStore.create(tmp_path / "receipts")
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _RecoverableEcho()},
        store=store,
        receipts=receipts,
    )
    reconcile = _reconcile_call(
        workspace=workspace,
        entrypoint="recoverable:RecoverableEcho.reconcile",
    )
    outcome = TaskOutcome.failed("transient", "already finished")
    sink = receipts.sink_for(reconcile.identity.model_copy(update={"operation": "execute"}))
    sink.install(
        TaskHostTerminalReceipt(
            host_implementation_digest=reconcile.identity.host_implementation_digest,
            wire_schema_version=reconcile.identity.wire_schema_version,
            invocation_id=reconcile.identity.invocation_id,
            task_id=reconcile.identity.task_id,
            activation_id=reconcile.identity.activation_id,
            attempt=reconcile.identity.attempt,
            activity_id=reconcile.identity.activity_id,
            operation="execute",
            request_digest=reconcile.activity.request_digest,
            workspace_identity_digest=workspace.identity.identity_digest,
            project_root_digest=workspace.identity.project_digest,
            write_root_digest=workspace.identity.write_root_digest,
            baseline_digest=reconcile.attempt_root.baseline_digest,
            staged_write_set_digest=store.seal(workspace.identity).staged_digest,
            dispatch_fingerprint_digest=reconcile.activity.dispatch_fingerprint_digest,
            reference_digest=reconcile.activity.reference_digest,
            outcome=outcome,
            outcome_digest=canonical_digest(outcome.model_dump(mode="json")),
            terminal_proof_digest=None,
            quiescence_proof_digest=prove_call_quiescent(),
            host_call_id=sink.host_call_id,
        )
    )
    result = asyncio.run(host.reconcile(reconcile))
    assert result.reconcile_result is not None
    promoted = receipts.authenticate(reconcile.identity.model_copy(update={"operation": "execute"}))
    assert len(promoted) == 1
    assert promoted[0].outcome == outcome


def _prepare_activity_ledger(root: Path, workspace: TaskWorkspaceBinding) -> None:
    ledger = Ledger(root / "invocations" / "inv-1" / "ledger")
    ledger.append_batch(
        (
            synthetic_invocation_started(),
            GraphStarted(graph_instance_id="root", graph_id="root"),
            TokenOffered(
                token_id="tok-1",
                graph_instance_id="root",
                source=None,
                target="run",
                payload=None,
            ),
            TokenConsumed(token_id="tok-1", graph_instance_id="root", node_id="run"),
            NodeActivated(
                activation_id="activation-run",
                graph_instance_id="root",
                node_id="run",
                token_ids=("tok-1",),
            ),
            TaskAttemptStarted(activation_id="activation-run", attempt=1, lease_expires_at="11"),
            TaskLeaseAcquired(
                task_id="task-1",
                activation_id="activation-run",
                attempt=1,
                owner_id="worker-1",
                acquired_at=1.0,
                heartbeat_at=1.0,
                expires_at=11.0,
            ),
            TaskActivityPrepared(
                activity_id="activity-1",
                task_id="task-1",
                activation_id="activation-run",
                attempt=1,
                request_digest="2" * 64,
                workspace_identity=workspace.identity,
            ),
        ),
        expected_next_seq=1,
    )


def test_host_before_worker_spawn_fault_has_no_child_dispatch_or_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _task_workspace_store(tmp_path)
    workspace = _begin_workspace(store)
    _prepare_activity_ledger(tmp_path, workspace)
    receipts = TerminalReceiptStore.create(tmp_path / "terminal-receipts")
    entrypoint, roots = _write_handler(
        tmp_path,
        class_name="MustNotSpawnHandler",
        body=(
            "async def execute(self, request, context):\n"
            "    (context.write_root / 'after.txt').write_text('ran', encoding='utf-8')\n"
            "    return TaskOutcome.succeeded({'ok': True})"
        ),
    )
    call = _execute_call(
        workspace=workspace,
        entrypoint=entrypoint,
        activity_id="activity-1",
    )
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _EchoHandler()},
        store=store,
        receipts=receipts,
        handler_import_roots={"test.echo.run": roots},
    )
    workers = _record_real_worker_spawns(monkeypatch)

    def stop_before_spawn(fault_id: str) -> None:
        if fault_id == "host-before-worker-spawn":
            raise RuntimeError(fault_id)

    monkeypatch.setattr(production_host, "_host_fault_cut", stop_before_spawn, raising=False)
    with pytest.raises(RuntimeError, match="host-before-worker-spawn"):
        asyncio.run(host.execute(call))

    assert workers == []
    assert not (workspace.write_root / "after.txt").exists()
    assert host.read_terminal_receipts(call.identity) == ()
    assert _ledger_activity_snapshot(tmp_path, call).state == "prepared"


def test_host_rejects_worker_source_drift_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _task_workspace_store(tmp_path)
    workspace = _begin_workspace(store)
    call = _execute_call(workspace=workspace).model_copy(
        update={
            "identity": _execute_call(workspace=workspace).identity.model_copy(
                update={"host_implementation_digest": "f" * 64}
            )
        }
    )
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(handlers={"test.echo.run": _EchoHandler()}, store=store)

    def must_not_spawn(*_args: object, **_kwargs: object) -> production_host._WorkerProcess:
        raise AssertionError("worker source drift reached spawn")

    monkeypatch.setattr(production_host._ProcessSupervisor, "spawn", must_not_spawn)

    with pytest.raises(ProductionHostError, match="implementation.*drift"):
        asyncio.run(host.execute(call))

    assert not (workspace.write_root / "after.txt").exists()


def test_worker_attestation_rejects_source_drift_after_spawn_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _task_workspace_store(tmp_path)
    workspace = _begin_workspace(store)
    entrypoint, roots = _write_handler(
        tmp_path,
        class_name="MustNotRunAfterSourceDriftHandler",
        body=(
            "async def execute(self, request, context):\n"
            "    (context.write_root / 'after.txt').write_text('ran', encoding='utf-8')\n"
            "    return TaskOutcome.succeeded({'ok': True})"
        ),
    )
    call = _execute_call(workspace=workspace, entrypoint=entrypoint)
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _EchoHandler()},
        store=store,
        handler_import_roots={"test.echo.run": roots},
    )
    worker_source = Path(production_host.__file__).resolve().parents[1] / "runtime" / "production_worker.py"
    original_source = worker_source.read_bytes()
    drifted = {"value": False}

    def mutate_after_spawn(fault_id: str) -> None:
        if fault_id == "host-after-spawn-before-dispatch" and not drifted["value"]:
            worker_source.write_bytes(original_source + b"\n# post-spawn source drift\n")
            drifted["value"] = True

    monkeypatch.setattr(production_host, "_host_fault_cut", mutate_after_spawn)
    try:
        with pytest.raises(ProductionHostError, match="worker implementation identity drifted"):
            asyncio.run(host.execute(call))
    finally:
        worker_source.write_bytes(original_source)

    assert drifted["value"] is True
    assert not (workspace.write_root / "after.txt").exists()


def test_host_after_spawn_before_dispatch_fault_cleans_child_without_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _task_workspace_store(tmp_path)
    workspace = _begin_workspace(store)
    _prepare_activity_ledger(tmp_path, workspace)
    receipts = TerminalReceiptStore.create(tmp_path / "terminal-receipts")
    entrypoint, roots = _write_handler(
        tmp_path,
        class_name="MustNotDispatchHandler",
        body=(
            "async def execute(self, request, context):\n"
            "    (context.write_root / 'after.txt').write_text('ran', encoding='utf-8')\n"
            "    return TaskOutcome.succeeded({'ok': True})"
        ),
    )
    call = _execute_call(
        workspace=workspace,
        entrypoint=entrypoint,
        activity_id="activity-1",
    )
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _EchoHandler()},
        store=store,
        receipts=receipts,
        handler_import_roots={"test.echo.run": roots},
    )
    workers = _record_real_worker_spawns(monkeypatch)

    def stop_before_dispatch(fault_id: str) -> None:
        if fault_id == "host-after-spawn-before-dispatch":
            raise RuntimeError(fault_id)

    monkeypatch.setattr(production_host, "_host_fault_cut", stop_before_dispatch, raising=False)
    with pytest.raises(RuntimeError, match="host-after-spawn-before-dispatch"):
        asyncio.run(host.execute(call))

    assert len(workers) == 1
    assert workers[0].popen.poll() is not None
    assert not (workspace.write_root / "after.txt").exists()
    assert host.read_terminal_receipts(call.identity) == ()
    assert _ledger_activity_snapshot(tmp_path, call).state == "prepared"


def test_host_during_activity_rpc_fault_cleans_child_and_reconciles_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _task_workspace_store(tmp_path)
    workspace = _begin_workspace(store)
    _prepare_activity_ledger(tmp_path, workspace)
    receipts = TerminalReceiptStore.create(tmp_path / "terminal-receipts")
    execute_entrypoint, roots = _write_handler(
        tmp_path,
        class_name="RpcInterruptedHandler",
        body=(
            "async def execute(self, request, context):\n"
            "    if context.activity is None:\n"
            "        raise ValueError('activity port is required')\n"
            "    context.activity.mark_dispatch_started({'endpoint': 'https://provider.invalid'})\n"
            "    (context.write_root / 'after.txt').write_text('ran', encoding='utf-8')\n"
            "    return TaskOutcome.succeeded({'ok': True})\n\n"
            "async def reconcile(self, request, context, activity):\n"
            "    from graph_engine.plugin_api import TaskActivityReconcileResult\n"
            "    return TaskActivityReconcileResult(status='running')"
        ),
    )
    call = _execute_call(
        workspace=workspace,
        entrypoint=execute_entrypoint,
        activity_id="activity-1",
    )
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _RecoverableEcho()},
        store=store,
        receipts=receipts,
        handler_import_roots={"test.echo.run": roots},
    )
    workers = _record_real_worker_spawns(monkeypatch)

    def stop_during_rpc(fault_id: str) -> None:
        if fault_id == "host-during-activity-rpc":
            raise RuntimeError(fault_id)

    monkeypatch.setattr(production_host, "_host_fault_cut", stop_during_rpc, raising=False)
    with pytest.raises(RuntimeError, match="host-during-activity-rpc"):
        asyncio.run(host.execute(call))

    assert len(workers) == 1
    assert workers[0].popen.poll() is not None
    assert not (workspace.write_root / "after.txt").exists()
    assert host.read_terminal_receipts(call.identity) == ()
    activity = _ledger_activity_snapshot(tmp_path, call)
    assert activity.state == "dispatch_started"
    assert activity.dispatch_fingerprint == {"endpoint": "https://provider.invalid"}
    ledger = Ledger(tmp_path / "invocations" / call.identity.invocation_id / "ledger")
    assert [item.event.kind for item in ledger.read_all()].count("task_activity_dispatch_started") == 1

    monkeypatch.setattr(production_host, "_host_fault_cut", lambda _fault_id: None)
    reconcile = _reconcile_call(
        workspace=workspace,
        entrypoint=execute_entrypoint.replace(".execute", ".reconcile"),
    ).model_copy(update={"activity": activity})
    result = asyncio.run(host.reconcile(reconcile))

    assert result.reconcile_result is not None
    assert result.reconcile_result.status == "running"
    assert len(workers) == 2
    assert all(worker.popen.poll() is not None for worker in workers)
    assert host.read_terminal_receipts(call.identity) == ()
    assert [item.event.kind for item in ledger.read_all()].count("task_activity_dispatch_started") == 1


def test_host_after_reference_bind_fault_preserves_bound_activity_for_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _task_workspace_store(tmp_path)
    workspace = _begin_workspace(store)
    _prepare_activity_ledger(tmp_path, workspace)
    receipts = TerminalReceiptStore.create(tmp_path / "terminal-receipts")
    execute_entrypoint, roots = _write_handler(
        tmp_path,
        class_name="BindInterruptedHandler",
        body=(
            "async def execute(self, request, context):\n"
            "    if context.activity is None:\n"
            "        raise ValueError('activity port is required')\n"
            "    context.activity.mark_dispatch_started({'endpoint': 'https://provider.invalid'})\n"
            "    context.activity.bind({'session_id': 'session-1'})\n"
            "    (context.write_root / 'after.txt').write_text('ran', encoding='utf-8')\n"
            "    return TaskOutcome.succeeded({'ok': True})\n\n"
            "async def reconcile(self, request, context, activity):\n"
            "    from graph_engine.plugin_api import TaskActivityReconcileResult\n"
            "    return TaskActivityReconcileResult(\n"
            "        status='running', reference={'session_id': 'session-1'}\n"
            "    )"
        ),
    )
    call = _execute_call(
        workspace=workspace,
        entrypoint=execute_entrypoint,
        activity_id="activity-1",
    )
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _RecoverableEcho()},
        store=store,
        receipts=receipts,
        handler_import_roots={"test.echo.run": roots},
    )

    def stop_after_bind(fault_id: str) -> None:
        if fault_id == "host-after-reference-bind":
            raise RuntimeError(fault_id)

    monkeypatch.setattr(production_host, "_host_fault_cut", stop_after_bind, raising=False)
    with pytest.raises(RuntimeError, match="host-after-reference-bind"):
        asyncio.run(host.execute(call))

    assert not (workspace.write_root / "after.txt").exists()
    assert host.read_terminal_receipts(call.identity) == ()
    activity = _ledger_activity_snapshot(tmp_path, call)
    assert activity.state == "bound"
    assert activity.reference == {"session_id": "session-1"}
    ledger = Ledger(tmp_path / "invocations" / call.identity.invocation_id / "ledger")
    kinds = [item.event.kind for item in ledger.read_all()]
    assert kinds.count("task_activity_dispatch_started") == 1
    assert kinds.count("task_activity_bound") == 1

    monkeypatch.setattr(production_host, "_host_fault_cut", lambda _fault_id: None)
    reconcile = _reconcile_call(
        workspace=workspace,
        entrypoint=execute_entrypoint.replace(".execute", ".reconcile"),
    ).model_copy(update={"activity": activity})
    result = asyncio.run(host.reconcile(reconcile))

    assert result.reconcile_result is not None
    assert result.reconcile_result.status == "running"
    assert result.reconcile_result.reference == {"session_id": "session-1"}
    replayed_kinds = [item.event.kind for item in ledger.read_all()]
    assert replayed_kinds.count("task_activity_dispatch_started") == 1
    assert replayed_kinds.count("task_activity_bound") == 1


def test_production_host_activity_snapshot_rpc_completes(tmp_path: Path) -> None:
    store = _task_workspace_store(tmp_path)
    workspace = _begin_workspace(store)
    _prepare_activity_ledger(tmp_path, workspace)
    entrypoint, roots = _write_handler(
        tmp_path,
        class_name="SnapshotHandler",
        body=(
            "async def execute(self, request, context):\n"
            "    if context.activity is None:\n"
            "        raise ValueError('activity port is required')\n"
            "    snapshot = context.activity.snapshot\n"
            "    (context.write_root / 'after.txt').write_text(snapshot.activity_id, encoding='utf-8')\n"
            "    return TaskOutcome.succeeded({'ok': True})\n"
        ),
    )
    host = _ProductionTaskExecutionHost(root=tmp_path, authorization=empty_runtime_authorization())
    host.bind_invocation_runtime(
        handlers={"test.echo.run": _EchoHandler()},
        store=store,
        handler_import_roots={"test.echo.run": roots},
    )
    result = asyncio.run(
        asyncio.wait_for(
            host.execute(
                _execute_call(
                    workspace=workspace,
                    entrypoint=entrypoint,
                    activity_id="activity-1",
                )
            ),
            timeout=5.0,
        )
    )
    assert result.outcome is not None
    assert result.outcome.status == "succeeded"
    assert (workspace.write_root / "after.txt").read_text(encoding="utf-8") == "activity-1"
