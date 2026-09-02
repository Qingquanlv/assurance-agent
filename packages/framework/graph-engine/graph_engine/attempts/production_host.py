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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.composition.lock import pinned_execution_host_lock
from graph_engine.errors import GraphEngineError
from graph_engine.plugin_api import (
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskHandler,
    TaskWorkspaceBinding,
)
from graph_engine.attempts.activity import LedgerTaskActivityPort
from graph_engine.attempts.host_protocol import (
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
    write_all_bytes,
)
from graph_engine.attempts.host_receipts import (
    TerminalReceiptError,
    TerminalReceiptStore,
    prove_call_quiescent,
)
from graph_engine.runtime.ledger import Ledger
from graph_engine.attempts.secret_sources import (
    InvocationRuntimeAuthorization,
    resolve_secret_source,
)
from graph_engine.attempts.workspace import TaskWorkspaceStore

_CALL_TIMEOUT_SECONDS = 30.0
_CANCEL_GRACE_SECONDS = 0.25
_TERMINATE_GRACE_SECONDS = 0.25
_PARENT_ALIVE_ENV = "GRAPH_ENGINE_PARENT_ALIVE_FD"
_ACTIVITY_RESPONSE_ENV = "GRAPH_ENGINE_ACTIVITY_RESPONSE_FD"
_CANCEL_ENV = "GRAPH_ENGINE_CANCEL_FD"

# Explicit trust-boundary gaps. Installed handler wheels are trusted code: an
# arbitrary handler can register an atexit hook that starts a new session after
# the final descendant scan. Preventing that requires an OS sandbox/cgroup/job
# boundary, not another in-process scan. Likewise, CPython does not expose a
# canonical resident code-image digest; the worker challenge authenticates the
# startup and current pinned source projections. Strict resident-image proof
# requires a signed/native launcher outside this host's current product boundary.
_TRUSTED_HANDLER_BOUNDARY_GAPS = {
    "handler-atexit-detached-escape": "requires an OS process-containment boundary",
    "resident-code-image-attestation": "requires a signed or native worker launcher",
}


def _host_fault_cut(_fault_id: str) -> None:
    """Internal no-op seam for exact production-host fault boundaries."""


class _BinaryStream(Protocol):
    def fileno(self) -> int: ...

    def read(self, size: int = -1) -> bytes: ...

    def write(self, data: bytes) -> int: ...

    def flush(self) -> None: ...


class UnsupportedProductionPlatform(GraphEngineError):
    """Raised when production task execution supports Linux and macOS only."""


class ProductionHostError(GraphEngineError):
    """Raised when the fixed production host cannot complete a call safely."""


@dataclass(frozen=True, slots=True)
class _BoundRuntime:
    handlers: Mapping[str, TaskHandler]
    store: TaskWorkspaceStore | None
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
        store: TaskWorkspaceStore,
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

    def read_terminal_receipts(self, identity: TaskHostCallIdentity) -> tuple[TaskHostTerminalReceipt, ...]:
        if self._bound.receipts is None:
            return ()
        return self._bound.receipts.authenticate(identity)

    def _result_from_installed_receipt(self, identity: TaskHostCallIdentity) -> TaskHostCallResult | None:
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
        pinned_host = pinned_execution_host_lock()
        if (
            call.identity.host_implementation_id != pinned_host.implementation_id
            or call.identity.host_implementation_digest != pinned_host.implementation_digest
            or call.identity.wire_schema_version != pinned_host.wire_schema_version
        ):
            raise ProductionHostError("production host implementation identity drifted")
        handler = self._bound.handlers.get(call.request.capability_id)
        if handler is None:
            raise ProductionHostError(f"missing installed handler: {call.request.capability_id}")
        workspace = self._attempt_binding(call)
        call_digest = canonical_digest(cast(JSONValue, call.model_dump(mode="json")))
        session_key = derive_wire_session_key(
            call_digest=call_digest,
            wire_schema_version=TASK_HOST_WIRE_SCHEMA_VERSION,
        )
        supervisor = _ProcessSupervisor.for_platform()
        _host_fault_cut("host-before-worker-spawn")
        secrets: dict[str, bytes] = {}
        process: _WorkerProcess | None = None
        try:
            secrets = self._resolve_authorized_secrets(call.authorized_secret_handles)
            process = supervisor.spawn(attempt_root=workspace.write_root, call_digest=call_digest)
            _host_fault_cut("host-after-spawn-before-dispatch")
            return await asyncio.to_thread(
                self._drive_worker,
                process,
                operation,
                call,
                session_key,
                workspace,
                secrets,
            )
        finally:
            _revoke_secrets(secrets)
            if process is not None:
                if process.stdout is not None:
                    stream_key = id(self._stream_io(process.stdout))
                    self._read_buffers.pop(stream_key, None)
                supervisor.cleanup(process)

    def _drive_worker(
        self,
        process: "_WorkerProcess",
        operation: HostOperation,
        call: TaskHostExecuteCall | TaskHostReconcileCall | TaskHostCancelCall,
        session_key: bytes,
        workspace: TaskWorkspaceBinding,
        secrets: dict[str, bytes],
    ) -> TaskHostCallResult:
        assert process.stdin is not None and process.stdout is not None
        deadline = time.monotonic() + float(call.timeout_seconds)
        self._verify_worker_identity(process, call, session_key, deadline=deadline)
        import_roots = self._bound.handler_import_roots.get(call.request.capability_id, ())
        self._write_frame(
            process.stdin,
            session_key,
            {
                "kind": "job",
                "operation": operation,
                "call": cast(JSONValue, call.model_dump(mode="json")),
                "project_root": str(workspace.project_root),
                "write_root": str(workspace.write_root),
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
        if call.authorized_secret_handles:
            _host_fault_cut("host-secret-channel-disconnect")
        self._write_frame(process.stdin, session_key, {"kind": "go"})

        result = self._read_worker_result(
            process,
            call,
            session_key,
            secrets,
            process,
            deadline=deadline,
        )
        stderr = process.read_bounded_stderr()
        scan_for_secret_leaks(stderr, secrets.values())
        exit_code = process.wait_with_escalation(session_key, grace_seconds=_TERMINATE_GRACE_SECONDS)
        if exit_code != 0:
            detail = process.read_bounded_stderr().decode("utf-8", errors="replace")
            message = f"worker exited with status {exit_code}"
            if detail:
                message = f"{message}: {detail}"
            raise ProductionHostError(message)
        quiescence = process.prove_quiescent(attempt_root=workspace.write_root)
        scan_for_secret_leaks(canonical_json_bytes(result.model_dump(mode="json")), secrets.values())
        _revoke_secrets(secrets)
        self._install_terminal_receipt(
            call,
            result,
            workspace=workspace,
            quiescence=quiescence,
        )
        return result

    def _verify_worker_identity(
        self,
        process: "_WorkerProcess",
        call: TaskHostExecuteCall | TaskHostReconcileCall | TaskHostCancelCall,
        session_key: bytes,
        *,
        deadline: float,
    ) -> None:
        assert process.stdin is not None and process.stdout is not None
        nonce = os.urandom(32).hex()
        self._write_frame(
            process.stdin,
            session_key,
            {"kind": "challenge", "nonce": nonce},
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProductionHostError("worker call timed out")
        try:
            attestation = self._read_frame_with_timeout(
                process.stdout,
                session_key,
                timeout=remaining,
            )
        except ProductionHostError as error:
            if str(error) == "worker result pending":
                raise ProductionHostError("worker call timed out") from error
            raise
        expected = call.identity
        if (
            attestation.get("kind") != "attestation"
            or attestation.get("nonce") != nonce
            or attestation.get("implementation_id") != expected.host_implementation_id
            or attestation.get("loaded_implementation_digest") != expected.host_implementation_digest
            or attestation.get("current_source_digest") != expected.host_implementation_digest
            or attestation.get("wire_schema_version") != expected.wire_schema_version
        ):
            raise ProductionHostError("worker implementation identity drifted")

    def _read_worker_result(
        self,
        process: "_WorkerProcess",
        call: TaskHostExecuteCall | TaskHostReconcileCall | TaskHostCancelCall,
        session_key: bytes,
        secrets: dict[str, bytes],
        worker: "_WorkerProcess",
        *,
        deadline: float,
    ) -> TaskHostCallResult:
        assert process.stdout is not None
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
                _host_fault_cut("host-before-activity-rpc")
                request_id = str(frame.get("id") or "")
                if not request_id:
                    raise ProductionHostError("activity rpc is missing a request id")
                assert process.stdin is not None
                try:
                    response = self._handle_activity_rpc(call, frame)
                except Exception as error:
                    self._write_frame(
                        process.stdin,
                        session_key,
                        {
                            "kind": "activity_error",
                            "id": request_id,
                            "message": str(error),
                        },
                    )
                    raise
                if str(frame.get("method")) == "bind":
                    _host_fault_cut("host-after-reference-bind")
                _host_fault_cut("host-during-activity-rpc")
                self._write_frame(process.stdin, session_key, {**response, "id": request_id})
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
        key = id(buffer_obj)
        buffer = self._read_buffers.pop(key, b"")
        deadline = time.monotonic() + timeout
        while True:
            if len(buffer) >= 40:
                try:
                    payload, remainder = decode_authenticated_frame(session_key, buffer)
                except TaskHostProtocolError as error:
                    if str(error) != "incomplete authenticated wire frame":
                        raise ProductionHostError(str(error)) from error
                else:
                    self._read_buffers[key] = remainder
                    document = json.loads(payload.decode("utf-8"))
                    if not isinstance(document, dict):
                        raise ProductionHostError("worker frame must be a mapping")
                    return cast(dict[str, JSONValue], document)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._read_buffers[key] = buffer
                raise ProductionHostError("worker result pending")
            ready, _, _ = select.select([fileno], [], [], remaining)
            if not ready:
                self._read_buffers[key] = buffer
                raise ProductionHostError("worker result pending")
            chunk = self._read_chunk(stream, 4096)
            if not chunk:
                self._read_buffers.pop(key, None)
                raise ProductionHostError("worker control stream closed unexpectedly")
            buffer += chunk

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
        workspace: TaskWorkspaceBinding,
        quiescence: str,
    ) -> None:
        if self._bound.receipts is None or call.identity.activity_id is None:
            return
        activity = self._activity_for_receipt(call)
        if activity is None:
            return
        if activity.workspace_identity != workspace.identity:
            raise ProductionHostError("terminal activity workspace identity drifted")
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
        assert self._bound.store is not None
        staged = self._bound.store.seal(workspace.identity)
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
                workspace_identity_digest=workspace.identity.identity_digest,
                project_root_digest=call.attempt_root.project_root_digest,
                write_root_digest=call.attempt_root.write_root_digest,
                baseline_digest=call.attempt_root.baseline_digest,
                staged_write_set_digest=staged.staged_digest,
                dispatch_fingerprint_digest=activity.dispatch_fingerprint_digest,
                reference_digest=activity.reference_digest,
                outcome=outcome,
                outcome_digest=canonical_digest(cast(JSONValue, outcome.model_dump(mode="json"))),
                terminal_proof_digest=None,
                quiescence_proof_digest=quiescence,
                host_call_id=sink.host_call_id,
            )
        )

    def _attempt_binding(
        self,
        call: TaskHostExecuteCall | TaskHostReconcileCall | TaskHostCancelCall,
    ) -> TaskWorkspaceBinding:
        assert self._bound.store is not None
        identity = call.attempt_root.workspace_identity
        try:
            binding = self._bound.store.begin(
                task_id=identity.task_id,
                attempt=identity.attempt,
                output_paths=identity.output_paths,
            )
        except Exception as error:
            raise ProductionHostError("attempt workspace is unavailable") from error
        if binding.identity != identity:
            raise ProductionHostError("attempt workspace identity differs from the host call")
        if (
            call.attempt_root.project_root_digest != identity.project_digest
            or call.attempt_root.write_root_digest != identity.write_root_digest
            or call.attempt_root.project_root_identity != binding.project_root_identity
            or call.attempt_root.write_root_identity != binding.write_root_identity
        ):
            raise ProductionHostError("attempt workspace root identity is not authenticated")
        return binding

    def _open_ledger(self, invocation_id: str) -> Ledger:
        invocation_root = self._root / "invocations" / invocation_id
        return Ledger(invocation_root / "ledger")

    def _resolve_authorized_secrets(self, handles: tuple[str, ...]) -> dict[str, bytes]:
        resolved: dict[str, bytes] = {}
        bindings = {item.handle: item for item in self._authorization.secret_sources}
        try:
            for handle in handles:
                binding = bindings.get(handle)
                if binding is None:
                    raise ProductionHostError(f"missing authorized secret handle: {handle}")
                resolved[handle] = resolve_secret_source(binding)
        except BaseException:
            _revoke_secrets(resolved)
            raise
        return resolved

    @staticmethod
    def _stream_io(stream: object) -> _BinaryStream:
        return cast(_BinaryStream, getattr(stream, "buffer", stream))

    def _write_frame(self, stream: object, session_key: bytes, message: dict[str, JSONValue]) -> None:
        payload = canonical_json_bytes(message)
        data = encode_authenticated_frame(session_key, payload)
        buffer = self._stream_io(stream)
        fileno = getattr(buffer, "fileno", None)
        if callable(fileno):
            write_all_bytes(cast(Callable[[], int], fileno)(), data)
            return
        buffer.write(data)
        buffer.flush()

    def _read_chunk(self, stream: object, size: int) -> bytes:
        buffer_obj = self._stream_io(stream)
        fileno = getattr(buffer_obj, "fileno", None)
        if callable(fileno):
            fd = cast(Callable[[], int], fileno)()
            if fd >= 0:
                return os.read(fd, size)
        chunk = buffer_obj.read(size)
        return chunk if chunk else b""

    def _read_frame(self, stream: object, session_key: bytes) -> dict[str, JSONValue]:
        buffer_obj = self._stream_io(stream)
        key = id(buffer_obj)
        buffer = self._read_buffers.pop(key, b"")
        while True:
            if len(buffer) < 40:
                chunk = self._read_chunk(stream, max(4096, 40 - len(buffer)))
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
                chunk = self._read_chunk(stream, 4096)
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
        call_owned = set(descendants)
        if self.popen.poll() is None:
            call_owned.add(str(self.popen.pid))
        writers = tuple(writer for writer in writers if writer in call_owned)
        return prove_call_quiescent(writer_identities=writers, descendant_identities=descendants)

    def terminate_group(self, *, grace_seconds: float) -> None:
        enumeration_error: TerminalReceiptError | None = None
        descendant_ids: set[int] = set()
        group_member_ids: set[int] = set()
        try:
            descendant_ids.update(_collect_process_tree_descendants(self.popen.pid))
        except TerminalReceiptError as error:
            enumeration_error = error
        try:
            group_member_ids.update(
                int(process_id)
                for process_id in _collect_process_group_descendants(
                    process_group=self.process_group,
                    exclude={self.popen.pid},
                )
            )
            descendant_ids.update(group_member_ids)
        except TerminalReceiptError as error:
            if enumeration_error is None:
                enumeration_error = error
        descendants = tuple(sorted(descendant_ids, reverse=True))
        for process_id in descendants:
            try:
                os.kill(process_id, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if self._has_authenticated_live_group_member(group_member_ids):
            try:
                os.killpg(self.process_group, signal.SIGTERM)
            except ProcessLookupError:
                if self.popen.poll() is None:
                    self.popen.terminate()
            except PermissionError:
                if self.popen.poll() is None:
                    self.popen.terminate()
        deadline = time.monotonic() + grace_seconds
        terminated = False
        while time.monotonic() < deadline:
            if self.popen.poll() is not None and not any(
                _process_exists(process_id) for process_id in descendants
            ):
                terminated = True
                break
            time.sleep(0.01)
        if not terminated:
            for process_id in descendants:
                try:
                    os.kill(process_id, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if self._has_authenticated_live_group_member(group_member_ids):
                try:
                    os.killpg(self.process_group, signal.SIGKILL)
                except ProcessLookupError:
                    if self.popen.poll() is None:
                        self.popen.kill()
                except PermissionError:
                    if self.popen.poll() is None:
                        self.popen.kill()
            try:
                self.popen.wait(timeout=grace_seconds + 1.0)
            except subprocess.TimeoutExpired:
                self.popen.kill()
                self.popen.wait(timeout=grace_seconds + 1.0)
        if enumeration_error is not None:
            raise enumeration_error

    def _has_authenticated_live_group_member(self, group_member_ids: set[int]) -> bool:
        if self.popen.poll() is None:
            return True
        return any(_process_is_in_group(process_id, self.process_group) for process_id in group_member_ids)


class _ProcessSupervisor:
    @classmethod
    def for_platform(cls) -> _ProcessSupervisor:
        if sys.platform == "win32":
            raise UnsupportedProductionPlatform("production task execution supports Linux and macOS only")
        if sys.platform not in {"darwin"} and not sys.platform.startswith("linux"):
            raise UnsupportedProductionPlatform("production task execution supports Linux and macOS only")
        return cls()

    def spawn(self, *, attempt_root: Path, call_digest: str) -> _WorkerProcess:
        owned_fds: set[int] = set()
        process: subprocess.Popen[bytes] | None = None
        try:
            read_fd, write_fd = _open_owned_pipe(owned_fds)
            response_r, response_w = _open_owned_pipe(owned_fds)
            cancel_r, cancel_w = _open_owned_pipe(owned_fds)
            command = [sys.executable, "-m", "graph_engine.attempts.production_worker"]
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
            _host_fault_cut("host-after-popen-before-worker-return")
            for descriptor in (read_fd, response_r, cancel_r):
                _close_owned_fd(descriptor, owned_fds)
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
            owned_fds.clear()
            return worker
        except BaseException:
            for descriptor in tuple(owned_fds):
                _close_owned_fd(descriptor, owned_fds)
            if process is not None:
                _reap_failed_spawn(process)
            raise

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
        try:
            process.terminate_group(grace_seconds=_TERMINATE_GRACE_SECONDS)
        finally:
            _close_process_streams(process.popen)


def _open_owned_pipe(owned_fds: set[int]) -> tuple[int, int]:
    read_fd, write_fd = os.pipe()
    owned_fds.update((read_fd, write_fd))
    return read_fd, write_fd


def _close_owned_fd(descriptor: int, owned_fds: set[int]) -> None:
    if descriptor not in owned_fds:
        return
    owned_fds.remove(descriptor)
    try:
        os.close(descriptor)
    except OSError:
        pass


def _close_process_streams(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass


def _reap_failed_spawn(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        if process.poll() is None:
            process.kill()
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1.0)
    finally:
        _close_process_streams(process)


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
        try:
            entries = os.listdir("/proc")
        except OSError as error:
            raise TerminalReceiptError("cannot enumerate process descendants") from error
        for entry in entries:
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
                ["/bin/ps", "-axo", "pid,pgid"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise TerminalReceiptError("cannot enumerate process descendants") from error
        if completed.returncode != 0:
            raise TerminalReceiptError("cannot enumerate process descendants")
        for line in completed.stdout.splitlines()[1:]:
            parts = line.strip().split()
            if len(parts) != 2:
                continue
            try:
                pid, pgid = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            if pgid == process_group and pid not in excluded:
                descendants.append(pid)
    if descendants:
        return tuple(str(pid) for pid in sorted(descendants))
    return ()


def _collect_process_tree_descendants(root_pid: int) -> tuple[int, ...]:
    parents: dict[int, int] = {}
    if sys.platform.startswith("linux"):
        try:
            entries = os.listdir("/proc")
        except OSError as error:
            raise TerminalReceiptError("cannot enumerate process descendants") from error
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                stat = (Path("/proc") / entry / "stat").read_text(encoding="ascii")
                parent_pid = int(stat.rpartition(") ")[2].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            parents[int(entry)] = parent_pid
    else:
        try:
            completed = subprocess.run(
                ["/bin/ps", "-axo", "pid=,ppid="],
                check=False,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise TerminalReceiptError("cannot enumerate process descendants") from error
        if completed.returncode != 0:
            raise TerminalReceiptError("cannot enumerate process descendants")
        for line in completed.stdout.splitlines():
            fields = line.split()
            if len(fields) != 2:
                continue
            try:
                process_id, parent_pid = (int(field) for field in fields)
            except ValueError:
                continue
            parents[process_id] = parent_pid
    descendants: set[int] = set()
    frontier = {root_pid}
    while frontier:
        children = {
            process_id
            for process_id, parent_pid in parents.items()
            if parent_pid in frontier and process_id not in descendants
        }
        if not children:
            break
        descendants.update(children)
        frontier = children
    return tuple(sorted(descendants, reverse=True))


def _process_exists(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except OSError:
        return False
    return True


def _process_is_in_group(process_id: int, process_group: int) -> bool:
    try:
        return os.getpgid(process_id) == process_group
    except OSError:
        return False


def _collect_workspace_writers(attempt_root: Path) -> tuple[str, ...]:
    resolved = attempt_root.resolve()
    writers: set[str] = set()
    completed: subprocess.CompletedProcess[str] | None = None
    for timeout in (2.0, 10.0):
        try:
            completed = subprocess.run(
                ["lsof", "-F", "p", "+D", str(resolved)],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            continue
        except OSError:
            pass
        break
    if completed is None:
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
