from __future__ import annotations

import asyncio
import json
import os
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
from graph_engine.plugin_api import RecoverableTaskHandler, TaskHandler
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
    TerminalReceiptStore,
    prove_call_quiescent,
)
from graph_engine.runtime.ledger import Ledger
from graph_engine.runtime.secret_sources import (
    InvocationRuntimeAuthorization,
    resolve_secret_source,
)
from graph_engine.runtime.workspace import SnapshotStore


class UnsupportedProductionPlatform(GraphEngineError):
    """Raised when production task execution supports Linux and macOS only."""


class ProductionHostError(GraphEngineError):
    """Raised when the fixed production host cannot complete a call safely."""


@dataclass(frozen=True, slots=True)
class _BoundRuntime:
    handlers: Mapping[str, TaskHandler]
    store: SnapshotStore | None
    receipts: TerminalReceiptStore | None


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
        self._bound = _BoundRuntime(handlers={}, store=None, receipts=None)
        self._read_buffers: dict[int, bytes] = {}

    def bind_invocation_runtime(
        self,
        *,
        handlers: Mapping[str, TaskHandler],
        store: SnapshotStore,
        receipts: TerminalReceiptStore | None = None,
    ) -> None:
        self._bound = _BoundRuntime(handlers=handlers, store=store, receipts=receipts)

    async def execute(self, call: TaskHostExecuteCall) -> TaskHostCallResult:
        return await self._invoke("execute", call)

    async def reconcile(self, call: TaskHostReconcileCall) -> TaskHostCallResult:
        return await self._invoke("reconcile", call)

    async def cancel(self, call: TaskHostCancelCall) -> TaskHostCallResult:
        return await self._invoke("cancel", call)

    def read_terminal_receipts(
        self, identity: TaskHostCallIdentity
    ) -> tuple[TaskHostTerminalReceipt, ...]:
        if self._bound.receipts is None:
            return ()
        return self._bound.receipts.authenticate(identity)

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
        self._write_frame(
            process.stdin,
            session_key,
            {
                "kind": "job",
                "operation": operation,
                "call": cast(JSONValue, call.model_dump(mode="json")),
                "attempt_root": str(attempt_root),
                "python_path": list(_python_path_entries()),
            },
        )
        for handle in call.authorized_secret_handles:
            self._write_frame(
                process.stdin,
                session_key,
                {"kind": "secret", "handle": handle, "value_hex": secrets[handle].hex()},
            )
        self._write_frame(process.stdin, session_key, {"kind": "go"})
        if call.activity_rpc.activity_id is None and process.stdin is not None:
            process.stdin.close()
        result: TaskHostCallResult | None = None
        try:
            while True:
                frame = self._read_frame(process.stdout, session_key)
                kind = frame.get("kind")
                if kind == "activity_rpc":
                    response = self._handle_activity_rpc(call, frame)
                    self._write_frame(process.stdin, session_key, response)
                    continue
                if kind == "result":
                    payload = frame.get("payload")
                    if not isinstance(payload, dict):
                        raise ProductionHostError("worker returned a malformed result frame")
                    result = TaskHostCallResult.model_validate(payload)
                    break
                raise ProductionHostError(f"unexpected worker frame: {kind!r}")
        except ProductionHostError as error:
            detail = process.read_bounded_stderr().decode("utf-8", errors="replace")
            if detail:
                raise ProductionHostError(f"{error}: {detail}") from error
            raise
        stderr = process.read_bounded_stderr()
        scan_for_secret_leaks(stderr, secrets.values())
        exit_code = process.wait()
        if exit_code != 0:
            detail = process.read_bounded_stderr().decode("utf-8", errors="replace")
            message = f"worker exited with status {exit_code}"
            if detail:
                message = f"{message}: {detail}"
            raise ProductionHostError(message)
        process.prove_quiescent()
        if call.identity.activity_id is not None and isinstance(
            self._bound.handlers.get(call.request.capability_id), RecoverableTaskHandler
        ):
            self._install_terminal_receipt(call, result)
        scan_for_secret_leaks(canonical_json_bytes(result.model_dump(mode="json")), secrets.values())
        return result

    def _handle_activity_rpc(
        self,
        call: TaskHostExecuteCall | TaskHostReconcileCall | TaskHostCancelCall,
        frame: dict[str, JSONValue],
    ) -> dict[str, JSONValue]:
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
        else:
            raise ProductionHostError(f"unsupported activity rpc method: {method!r}")
        return {
            "kind": "activity_response",
            "snapshot": cast(JSONValue, snapshot.model_dump(mode="json")),
        }

    def _install_terminal_receipt(
        self,
        call: TaskHostExecuteCall | TaskHostReconcileCall | TaskHostCancelCall,
        result: TaskHostCallResult,
    ) -> None:
        if self._bound.receipts is None or call.identity.activity_id is None:
            return
        activity = getattr(call, "activity", None)
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
        quiescence = prove_call_quiescent()
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
    _stderr_chunks: list[bytes]
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

    def wait(self) -> int:
        return self.popen.wait(timeout=30)

    def prove_quiescent(self) -> None:
        prove_call_quiescent()

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
        command = [sys.executable, "-m", "graph_engine.runtime.production_worker"]
        env = {
            "PYTHONUNBUFFERED": "1",
            "GRAPH_ENGINE_WORKER_CALL_DIGEST": call_digest,
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
        )
        worker = _WorkerProcess(
            popen=process,
            process_group=process.pid,
            parent_alive_w=-1,
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
        if process.popen.poll() is None:
            process.terminate_group(grace_seconds=0.25)


def _capture_stderr(stream: object, chunks: list[bytes]) -> None:
    total = 0
    while total < _MAX_STDERR_BYTES:
        data = stream.read(4096)  # type: ignore[attr-defined]
        if not data:
            break
        chunks.append(data)
        total += len(data)


def _python_path_entries() -> tuple[str, ...]:
    return tuple(path for path in sys.path if path)


__all__ = [
    "ProductionHostError",
    "UnsupportedProductionPlatform",
]
