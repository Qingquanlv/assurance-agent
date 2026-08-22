from __future__ import annotations

import asyncio
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.errors import GraphEngineError
from graph_engine.plugin_api import (
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskHandler,
    TaskOutcome,
)
from graph_engine.runtime.activity import LedgerTaskActivityPort
from graph_engine.runtime.host_protocol import (
    TASK_HOST_WIRE_SCHEMA_VERSION,
    HostOperation,
    TaskHostCallIdentity,
    TaskHostCallResult,
    TaskHostCancelCall,
    TaskHostExecuteCall,
    TaskHostProtocolError,
    TaskHostReconcileCall,
    TaskHostTerminalReceipt,
    _MAX_STDERR_BYTES,
    decode_authenticated_frame,
    derive_wire_session_key,
    encode_authenticated_frame,
    scan_for_secret_leaks,
)
from graph_engine.runtime.host_receipts import (
    TerminalReceiptError,
    TerminalReceiptStore,
    prove_call_quiescent,
)
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.secret_sources import (
    InvocationRuntimeAuthorization,
    resolve_secret_source,
)
from graph_engine.runtime.workspace import SnapshotStore

_CALL_TIMEOUT_SECONDS = 30.0
_CANCEL_GRACE_SECONDS = 0.25
_TERMINATE_GRACE_SECONDS = 0.25
_PARENT_ALIVE_ENV = "GRAPH_ENGINE_PARENT_ALIVE_FD"
_ACTIVITY_RESPONSE_ENV = "GRAPH_ENGINE_ACTIVITY_RESPONSE_FD"
_CANCEL_ENV = "GRAPH_ENGINE_CANCEL_FD"


class UnsupportedProductionPlatform(GraphEngineError):
    """Raised when production task execution supports Linux and macOS only."""


class ProductionHostError(GraphEngineError):
    """Raised when the fixed production host cannot complete a call safely."""


@dataclass(frozen=True, slots=True)
class _BoundRuntime:
    handlers: Mapping[str, TaskHandler]
    store: SnapshotStore | None
    receipts: TerminalReceiptStore | None
    handler_import_roots: Mapping[str, tuple[str, ...]]


class _ProductionTaskExecutionHost:
    __slots__ = ("_authorization", "_bound", "_root", "_read_buffers")

    def __init__(
        self,
        *,
        root: Path,
        authorization: InvocationRuntimeAuthorization,
    ) -> None:
        self._root = Path(root).absolute()
        self._authorization = authorization
        self._bound = _BoundRuntime(
            handlers={},
            store=None,
            receipts=None,
            handler_import_roots={},
        )
        self._read_buffers: dict[int, bytes] = {}

    def bind_invocation_runtime(
        self,
        *,
        handlers: Mapping[str, TaskHandler],
        store: SnapshotStore,
        receipts: TerminalReceiptStore | None = None,
        handler_import_roots: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._bound = _BoundRuntime(
            handlers=handlers,
            store=store,
            receipts=receipts,
            handler_import_roots=dict(handler_import_roots or {}),
        )

    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        return await self._invoke("execute", call)

    async def reconcile(self, call: TaskHostReconcileCall) -> TaskHostCallResult:
        promoted = self._result_from_installed_receipt(call.identity)
        if promoted is not None:
            return promoted
        return await self._invoke("reconcile", call)

    async def cancel(self, call: TaskHostCancelCall) -> TaskHostCallResult:
        promoted = self._result_from_installed_receipt(call.identity)
        if promoted is not None and promoted.reconcile_result is not None:
            return TaskHostCallResult(
                operation="cancel",
                cancel_result=TaskActivityCancelResult(
                    status="terminal",
                    outcome=promoted.reconcile_result.outcome,
                ),
            )
        return await self._invoke("cancel", call)

    def read_terminal_receipts(
        self, identity: TaskHostCallIdentity
    ) -> tuple[TaskHostTerminalReceipt, ...]:
        if self._bound.receipts is None:
            return ()
        return self._bound.receipts.authenticate(identity)

    def _result_from_installed_receipt(
        self, identity: TaskHostCallIdentity
    ) -> TaskHostCallResult | None:
        if self._bound.receipts is None or identity.activity_id is None:
            return None
        for operation in ("execute", "reconcile", "cancel"):
            check = identity.model_copy(update={"operation": operation})
            found = self._bound.receipts.authenticate(check)
            if found:
                return TaskHostCallResult(
                    operation="reconcile",
                    reconcile_result=TaskActivityReconcileResult(
                        status="terminal",
                        outcome=found[0].outcome,
                    ),
                )
        return None

    async def _invoke(
        self,
        operation: HostOperation,
        call: TaskHostExecuteCall | TaskHostReconcileCall | TaskHostCancelCall,
    ) -> TaskHostCallResult:
        if self._bound.store is None:
            raise ProductionHostError("production host is not bound to an invocation workspace")
        handler = self._bound.handlers.get(call.request.capability_id)
        if handler is None:
            raise ProductionHostError(f"missing installed handler: {call.request.capability_id}")
        attempt_root = self._attempt_root(call)
        call_digest = canonical_digest(cast(JSONValue, call.model_dump(mode="json")))
        session_key = derive_wire_session_key(
            call_digest=call_digest,
            wire_schema_version=TASK_HOST_WIRE_SCHEMA_VERSION,
        )
        secrets = self._resolve_authorized_secrets(call.authorized_secret_handles)
        supervisor = _ProcessSupervisor.for_platform()
        process = supervisor.spawn(attempt_root=attempt_root, call_digest=call_digest)
        try:
            return await asyncio.to_thread(
                self._drive_worker,
                process,
                operation,
                call,
                session_key,
                attempt_root,
                secrets,
            )
        finally:
            _revoke_secrets(secrets)
            supervisor.cleanup(process)

    def _drive_worker(
        self,
        process: "_WorkerProcess",
        operation: HostOperation,
        call: TaskHostExecuteCall | TaskHostReconcileCall | TaskHostCancelCall,
        session_key: bytes,
        attempt_root: Path,
        secrets: dict[str, bytes],
    ) -> TaskHostCallResult:
        assert process.stdin is not None and process.stdout is not None
        import_roots = self._bound.handler_import_roots.get(call.request.capability_id, ())
        self._write_frame(
            process.stdin,
            session_key,
            {
                "kind": "job",
                "operation": operation,
                "call": cast(JSONValue, call.model_dump(mode="json")),
                "attempt_root": str(attempt_root),
                "capability_id": call.capability_id,
                "capability_entrypoint": call.capability_entrypoint,
                "handler_import_roots": list(import_roots),
            },
        )
        for handle in call.authorized_secret_handles:
            self._write_frame(
                process.stdin,
                session_key,
                {"kind": "secret", "handle": handle, "value_hex": secrets[handle].hex()},
            )
        self._write_frame(process.stdin, session_key, {"kind": "go"})
        if process.stdin is not None:
            process.stdin.close()

        result = self._read_worker_result(process, call, session_key, secrets, process)
        stderr = process.read_bounded_stderr()
        scan_for_secret_leaks(stderr, secrets.values())
        exit_code = process.wait_with_escalation(session_key, grace_seconds=_TERMINATE_GRACE_SECONDS)
        if exit_code != 0:
            detail = process.read_bounded_stderr().decode("utf-8", errors="replace")
            message = f"worker exited with status {exit_code}"
            if detail:
                message = f"{message}: {detail}"
            raise ProductionHostError(message)
        quiescence = process.prove_quiescent(attempt_root=attempt_root)
        scan_for_secret_leaks(canonical_json_bytes(result.model_dump(mode="json")), secrets.values())
        _revoke_secrets(secrets)
        self._install_terminal_receipt(call, result, quiescence=quiescence)
        return result

    def _read_worker_result(
        self,
        process: "_WorkerProcess",
        call: TaskHostExecuteCall | TaskHostReconcileCall | TaskHostCancelCall,
        session_key: bytes,
        secrets: dict[str, bytes],
        worker: "_WorkerProcess",
    ) -> TaskHostCallResult:
        assert process.stdout is not None
        deadline = time.monotonic() + _CALL_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.send_cancel(session_key)
                process.terminate_group(grace_seconds=_CANCEL_GRACE_SECONDS)
                raise ProductionHostError("worker call timed out")
            try:
                frame = self._read_frame_with_timeout(
                    process.stdout,
                    session_key,
                    timeout=min(remaining, 0.1),
                )
            except ProductionHostError as error:
                if str(error) == "worker result pending":
                    continue
                detail = process.read_bounded_stderr().decode("utf-8", errors="replace")
                if detail:
                    raise ProductionHostError(f"{error}: {detail}") from error
                raise
            kind = frame.get("kind")
            if kind == "activity_rpc":
                response = self._handle_activity_rpc(call, frame)
                if worker.activity_response_w < 0:
                    raise ProductionHostError("activity rpc channel is unavailable")
                response_fd = os.dup(worker.activity_response_w)
                response_stream = os.fdopen(response_fd, "wb", buffering=0)
                try:
                    self._write_frame(response_stream, session_key, response)
                finally:
                    response_stream.close()
                continue
            if kind == "result":
                payload = frame.get("payload")
                if not isinstance(payload, dict):
                    raise ProductionHostError("worker returned a malformed result frame")
                return TaskHostCallResult.model_validate(payload)
            raise ProductionHostError(f"unexpected worker frame: {kind!r}")

    def _read_frame_with_timeout(
        self,
        stream: object,
        session_key: bytes,
        *,
        timeout: float,
    ) -> dict[str, JSONValue]:
        buffer_obj = self._stream_io(stream)
        fileno = buffer_obj.fileno()
        ready, _, _ = select.select([fileno], [], [], timeout)
        if not ready:
            raise ProductionHostError("worker result pending")
        return self._read_frame(stream, session_key)

    def _handle_activity_rpc(
        self,
        call: TaskHostExecuteCall | TaskHostReconcileCall | TaskHostCancelCall | None,
        frame: dict[str, JSONValue],
    ) -> dict[str, JSONValue]:
        if call is None:
            raise ProductionHostError("activity rpc received without call context")
        if call.activity_rpc.activity_id is None:
            raise ProductionHostError("activity rpc received without activity identity")
        ledger = self._open_ledger(call.identity.invocation_id)
        port = LedgerTaskActivityPort(ledger=ledger, identity=call.activity_rpc)
        method = str(frame.get("method"))
        args = frame.get("args")
        if not isinstance(args, list):
            raise ProductionHostError("activity rpc args must be a list")
        if method == "mark_dispatch_started":
            snapshot = port.mark_dispatch_started(args[0])
        elif method == "bind":
            snapshot = port.bind(args[0])
        elif method == "snapshot":
            snapshot = port.snapshot
        else:
            raise ProductionHostError(f"unsupported activity rpc method: {method!r}")
        return {
            "kind": "activity_response",
            "snapshot": cast(JSONValue, snapshot.model_dump(mode="json")),
        }

    def _activity_for_receipt(
        self,
        call: TaskHostExecuteCall | TaskHostReconcileCall | TaskHostCancelCall,
    ) -> TaskActivitySnapshot | None:
        activity = getattr(call, "activity", None)
        if activity is not None:
            return activity
        if call.identity.activity_id is None:
            return None
        ledger = self._open_ledger(call.identity.invocation_id)
        port = LedgerTaskActivityPort(ledger=ledger, identity=call.activity_rpc)
        try:
            return port.snapshot
        except Exception:
            return None

    def _install_terminal_receipt(
        self,
        call: TaskHostExecuteCall | TaskHostReconcileCall | TaskHostCancelCall,
        result: TaskHostCallResult,
        *,
        quiescence: str,
    ) -> None:
        if self._bound.receipts is None or call.identity.activity_id is None:
            return
        activity = self._activity_for_receipt(call)
        if activity is None:
            return
        if result.operation == "execute" and result.outcome is not None:
            outcome = result.outcome
        elif result.operation == "reconcile" and result.reconcile_result is not None:
            if result.reconcile_result.status != "terminal" or result.reconcile_result.outcome is None:
                return
            outcome = result.reconcile_result.outcome
        elif result.operation == "cancel" and result.cancel_result is not None:
            if result.cancel_result.status != "terminal" or result.cancel_result.outcome is None:
                return
            outcome = result.cancel_result.outcome
        else:
            return
        sink = self._bound.receipts.sink_for(call.identity)
        sink.install(
            TaskHostTerminalReceipt(
                host_implementation_digest=call.identity.host_implementation_digest,
                wire_schema_version=call.identity.wire_schema_version,
                invocation_id=call.identity.invocation_id,
                task_id=call.identity.task_id,
                activation_id=call.identity.activation_id,
                attempt=call.identity.attempt,
                activity_id=call.identity.activity_id,
                operation=call.identity.operation,
                request_digest=activity.request_digest,
                workspace_identity_digest=activity.workspace_identity.attempt_identity_digest,
                dispatch_fingerprint_digest=activity.dispatch_fingerprint_digest,
                reference_digest=activity.reference_digest,
                outcome=outcome,
                outcome_digest=canonical_digest(cast(JSONValue, outcome.model_dump(mode="json"))),
                terminal_proof_digest=None,
                quiescence_proof_digest=quiescence,
                host_call_id=sink.host_call_id,
            )
        )

    def _attempt_root(
        self, call: TaskHostExecuteCall | TaskHostReconcileCall | TaskHostCancelCall
    ) -> Path:
        assert self._bound.store is not None
        expected = self._bound.store.root / "attempts" / call.attempt_root.attempt_directory_id
        resolved = expected.resolve()
        attempts_root = (self._bound.store.root / "attempts").resolve()
        if resolved != attempts_root and attempts_root not in resolved.parents:
            raise ProductionHostError("attempt workspace escapes the bound attempt namespace")
        if not resolved.is_dir():
            raise ProductionHostError("attempt workspace is unavailable")
        return resolved

    def _open_ledger(self, invocation_id: str) -> Ledger:
        invocation_root = self._root / "invocations" / invocation_id
        return Ledger(invocation_root / "ledger")

    def _resolve_authorized_secrets(self, handles: tuple[str, ...]) -> dict[str, bytes]:
        resolved: dict[str, bytes] = {}
        bindings = {item.handle: item for item in self._authorization.secret_sources}
        for handle in handles:
            binding = bindings.get(handle)
            if binding is None:
                raise ProductionHostError(f"missing authorized secret handle: {handle}")
            resolved[handle] = resolve_secret_source(binding)
        return resolved

    @staticmethod
    def _stream_io(stream: object) -> object:
        return getattr(stream, "buffer", stream)

    def _write_frame(self, stream: object, session_key: bytes, message: dict[str, JSONValue]) -> None:
        payload = canonical_json_bytes(message)
        data = encode_authenticated_frame(session_key, payload)
        buffer = self._stream_io(stream)
        buffer.write(data)
        buffer.flush()

    def _read_frame(self, stream: object, session_key: bytes) -> dict[str, JSONValue]:
        buffer_obj = self._stream_io(stream)
        key = id(buffer_obj)
        buffer = self._read_buffers.pop(key, b"")
        while True:
            if len(buffer) < 40:
                chunk = buffer_obj.read(max(4096, 40 - len(buffer)))
                if not chunk:
                    raise ProductionHostError("worker control stream closed unexpectedly")
                buffer += chunk
            try:
                payload, remainder = decode_authenticated_frame(session_key, buffer)
                self._read_buffers[key] = remainder
                break
            except TaskHostProtocolError as error:
                if str(error) != "incomplete authenticated wire frame":
                    raise ProductionHostError(str(error)) from error
                chunk = buffer_obj.read(4096)
                if not chunk:
                    raise ProductionHostError("worker control stream closed unexpectedly") from error
                buffer += chunk
        document = json.loads(payload.decode("utf-8"))
        if not isinstance(document, dict):
            raise ProductionHostError("worker frame must be a mapping")
        return cast(dict[str, JSONValue], document)


@dataclass(slots=True)
class _WorkerProcess:
    popen: subprocess.Popen[bytes]
    process_group: int
    parent_alive_w: int
    activity_response_w: int
    cancel_w: int
    _stderr_chunks: list[bytes]
    _session_key: bytes | None = None
    _stderr_thread: threading.Thread | None = None

    @property
    def stdin(self) -> object | None:
        return self.popen.stdin

    @property
    def stdout(self) -> object | None:
        return self.popen.stdout

    def read_bounded_stderr(self) -> bytes:
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1.0)
        return b"".join(self._stderr_chunks)[:_MAX_STDERR_BYTES]

    def send_cancel(self, session_key: bytes) -> None:
        if self.popen.poll() is not None:
            return
        if self.cancel_w < 0:
            return
        try:
            payload = canonical_json_bytes({"kind": "cancel"})
            data = encode_authenticated_frame(session_key, payload)
            os.write(self.cancel_w, data)
        except OSError:
            return

    def wait_with_escalation(self, session_key: bytes, *, grace_seconds: float) -> int:
        deadline = time.monotonic() + _CALL_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            code = self.popen.poll()
            if code is not None:
                return code
            time.sleep(0.01)
        self.send_cancel(session_key)
        self.terminate_group(grace_seconds=grace_seconds)
        return self.popen.wait(timeout=grace_seconds + 1.0)

    def prove_quiescent(self, *, attempt_root: Path) -> str:
        descendants = _collect_process_group_descendants(
            process_group=self.process_group,
            exclude={self.popen.pid},
        )
        writers = _collect_workspace_writers(attempt_root)
        return prove_call_quiescent(writer_identities=writers, descendant_identities=descendants)

    def terminate_group(self, *, grace_seconds: float) -> None:
        try:
            os.killpg(self.process_group, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if self.popen.poll() is not None:
                return
            time.sleep(0.01)
        try:
            os.killpg(self.process_group, signal.SIGKILL)
        except ProcessLookupError:
            return


class _ProcessSupervisor:
    @classmethod
    def for_platform(cls) -> _ProcessSupervisor:
        if sys.platform == "win32":
            raise UnsupportedProductionPlatform(
                "production task execution supports Linux and macOS only"
            )
        if sys.platform not in {"darwin"} and not sys.platform.startswith("linux"):
            raise UnsupportedProductionPlatform(
                "production task execution supports Linux and macOS only"
            )
        return cls()

    def spawn(self, *, attempt_root: Path, call_digest: str) -> _WorkerProcess:
        read_fd, write_fd = os.pipe()
        response_r, response_w = os.pipe()
        cancel_r, cancel_w = os.pipe()
        command = [sys.executable, "-m", "graph_engine.runtime.production_worker"]
        env = {
            "PYTHONUNBUFFERED": "1",
            "GRAPH_ENGINE_WORKER_CALL_DIGEST": call_digest,
            _PARENT_ALIVE_ENV: str(read_fd),
            _ACTIVITY_RESPONSE_ENV: str(response_r),
            _CANCEL_ENV: str(cancel_r),
            "PATH": os.environ.get("PATH", ""),
        }
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(attempt_root),
            env=env,
            start_new_session=True,
            pass_fds=(read_fd, response_r, cancel_r),
        )
        os.close(read_fd)
        os.close(response_r)
        os.close(cancel_r)
        worker = _WorkerProcess(
            popen=process,
            process_group=process.pid,
            parent_alive_w=write_fd,
            activity_response_w=response_w,
            cancel_w=cancel_w,
            _stderr_chunks=[],
        )
        if process.stderr is not None:
            worker._stderr_thread = threading.Thread(
                target=_capture_stderr,
                args=(process.stderr, worker._stderr_chunks),
                daemon=True,
            )
            worker._stderr_thread.start()
        return worker

    def cleanup(self, process: _WorkerProcess) -> None:
        if process.parent_alive_w >= 0:
            try:
                os.close(process.parent_alive_w)
            except OSError:
                pass
        if process.activity_response_w >= 0:
            try:
                os.close(process.activity_response_w)
            except OSError:
                pass
        if process.cancel_w >= 0:
            try:
                os.close(process.cancel_w)
            except OSError:
                pass
        if process.popen.poll() is None:
            process.terminate_group(grace_seconds=_TERMINATE_GRACE_SECONDS)


def _capture_stderr(stream: object, chunks: list[bytes]) -> None:
    total = 0
    while total < _MAX_STDERR_BYTES:
        data = stream.read(4096)  # type: ignore[attr-defined]
        if not data:
            break
        chunks.append(data)
        total += len(data)


def _revoke_secrets(secrets: dict[str, bytes]) -> None:
    for handle in list(secrets):
        material = secrets.pop(handle)
        mutable = bytearray(material)
        for index in range(len(mutable)):
            mutable[index] = 0
        del mutable


def _collect_process_group_descendants(
    *,
    process_group: int,
    exclude: set[int] | None = None,
) -> tuple[str, ...]:
    excluded = exclude or set()
    descendants: list[int] = []
    if sys.platform.startswith("linux"):
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid in excluded:
                continue
            try:
                with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
                    stat = handle.read()
                pgid = int(stat.rpartition(") ")[2].split()[2])
            except (OSError, ValueError, IndexError):
                continue
            if pgid == process_group and pid not in excluded:
                descendants.append(pid)
    else:
        try:
            completed = subprocess.run(
                ["ps", "-axo", "pid,pgid"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            raise TerminalReceiptError("cannot enumerate process descendants") from None
        for line in completed.stdout.splitlines()[1:]:
            parts = line.strip().split()
            if len(parts) != 2:
                continue
            pid, pgid = int(parts[0]), int(parts[1])
            if pgid == process_group and pid not in excluded:
                descendants.append(pid)
    if descendants:
        return tuple(str(pid) for pid in sorted(descendants))
    return ()


def _collect_workspace_writers(attempt_root: Path) -> tuple[str, ...]:
    resolved = attempt_root.resolve()
    writers: set[str] = set()
    try:
        completed = subprocess.run(
            ["lsof", "-F", "p", "+D", str(resolved)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        if sys.platform.startswith("linux"):
            writers.update(_linux_workspace_writers(resolved))
        else:
            raise TerminalReceiptError("cannot prove workspace writer quiescence") from None
    else:
        current: str | None = None
        for line in completed.stdout.splitlines():
            if line.startswith("p"):
                current = line[1:]
            elif line.startswith("f") and current is not None:
                writers.add(current)
    if writers:
        return tuple(sorted(writers))
    return ()


def _linux_workspace_writers(attempt_root: Path) -> set[str]:
    writers: set[str] = set()
    prefix = str(attempt_root)
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        fd_dir = Path("/proc") / entry / "fd"
        try:
            for link in fd_dir.iterdir():
                target = os.readlink(link)
                if target.startswith(prefix):
                    writers.add(entry)
                    break
        except OSError:
            continue
    return writers


__all__ = [
    "ProductionHostError",
    "UnsupportedProductionPlatform",
]
