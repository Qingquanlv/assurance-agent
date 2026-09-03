from __future__ import annotations

import asyncio
import importlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Protocol, cast

from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.composition.lock import ExecutionHostLock, pinned_execution_host_lock
from graph_engine.plugin_api import (
    DirectoryIdentity,
    SecretPort,
    TaskActivityCancelResult,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskContext,
    TaskHandler,
    TaskOutcome,
)
from graph_engine.attempts.host_protocol import (
    TASK_HOST_WIRE_SCHEMA_VERSION,
    TaskHostCallResult,
    TaskHostCancelCall,
    TaskHostExecuteCall,
    TaskHostProtocolError,
    TaskHostReconcileCall,
    authorized_secret_port,
    decode_authenticated_frame,
    derive_wire_session_key,
    encode_authenticated_frame,
    write_all_bytes,
)

_PARENT_ALIVE_ENV = "GRAPH_ENGINE_PARENT_ALIVE_FD"
_ACTIVITY_RESPONSE_ENV = "GRAPH_ENGINE_ACTIVITY_RESPONSE_FD"
_CANCEL_ENV = "GRAPH_ENGINE_CANCEL_FD"


class _BinaryStream(Protocol):
    def fileno(self) -> int: ...

    def read(self, size: int = -1) -> bytes: ...

    def write(self, data: bytes) -> int: ...

    def flush(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _WorkerJob:
    operation: str
    call: dict[str, JSONValue]
    project_root: str
    write_root: str
    capability_id: str
    capability_entrypoint: str
    handler_import_roots: tuple[str, ...]


class _ParentActivityPort:
    __slots__ = ("_send",)

    def __init__(self, send: Callable[[dict[str, JSONValue]], dict[str, JSONValue]]) -> None:
        self._send = send

    @property
    def snapshot(self) -> TaskActivitySnapshot:
        response = self._send({"kind": "activity_rpc", "method": "snapshot", "args": []})
        return TaskActivitySnapshot.model_validate(response["snapshot"])

    def mark_dispatch_started(self, fingerprint: JSONValue) -> TaskActivitySnapshot:
        response = self._send(
            {"kind": "activity_rpc", "method": "mark_dispatch_started", "args": [fingerprint]}
        )
        return TaskActivitySnapshot.model_validate(response["snapshot"])

    def bind(self, reference: JSONValue) -> TaskActivitySnapshot:
        response = self._send({"kind": "activity_rpc", "method": "bind", "args": [reference]})
        return TaskActivitySnapshot.model_validate(response["snapshot"])


class _ParentDeathSupervisor:
    __slots__ = ("_normal_shutdown",)

    def __init__(self) -> None:
        self._normal_shutdown = threading.Event()

    def begin_normal_shutdown(self) -> None:
        self._normal_shutdown.set()

    @property
    def normal_shutdown_started(self) -> bool:
        return self._normal_shutdown.is_set()


def _install_parent_death_supervision(parent_alive_fd: int | None) -> _ParentDeathSupervisor:
    supervisor = _ParentDeathSupervisor()

    def _watch_parent() -> None:
        if parent_alive_fd is None:
            while True:
                time.sleep(0.1)
                if supervisor.normal_shutdown_started:
                    return
                if os.getppid() == 1:
                    _kill_worker_process_tree()
            return
        while True:
            try:
                chunk = os.read(parent_alive_fd, 1)
            except OSError:
                if supervisor.normal_shutdown_started:
                    return
                _kill_worker_process_tree()
            if chunk == b"":
                if supervisor.normal_shutdown_started:
                    return
                _kill_worker_process_tree()

    thread = threading.Thread(target=_watch_parent, daemon=True)
    thread.start()
    return supervisor


def _process_table() -> dict[int, tuple[int, int]]:
    table: dict[int, tuple[int, int]] = {}
    if sys.platform.startswith("linux"):
        try:
            entries = os.listdir("/proc")
        except OSError as error:
            raise TaskHostProtocolError("cannot enumerate worker descendants") from error
        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                stat = (Path("/proc") / entry / "stat").read_text(encoding="ascii")
                fields = stat.rpartition(") ")[2].split()
                table[pid] = (int(fields[1]), int(fields[2]))
            except (OSError, ValueError, IndexError):
                continue
        return table
    try:
        completed = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,pgid="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TaskHostProtocolError("cannot enumerate worker descendants") from error
    if completed.returncode != 0:
        raise TaskHostProtocolError("cannot enumerate worker descendants")
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        try:
            pid, parent_pid, process_group = (int(field) for field in fields)
        except ValueError:
            continue
        table[pid] = (parent_pid, process_group)
    return table


def _descendant_processes(root_pid: int, *, detached_only: bool = False) -> tuple[int, ...]:
    table = _process_table()
    descendants: set[int] = set()
    frontier = {root_pid}
    while frontier:
        children = {
            pid
            for pid, (parent_pid, _process_group) in table.items()
            if parent_pid in frontier and pid not in descendants
        }
        if not children:
            break
        descendants.update(children)
        frontier = children
    process_group = os.getpgrp()
    return tuple(
        sorted(
            (
                process_id
                for process_id in descendants
                if _process_exists(process_id)
                and (not detached_only or table[process_id][1] != process_group)
            ),
            reverse=True,
        )
    )


def _terminate_worker_descendants() -> tuple[int, ...]:
    descendants = _descendant_processes(os.getpid())
    for process_id in descendants:
        try:
            os.kill(process_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        reaped = False
        while True:
            try:
                child_pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if child_pid == 0:
                break
            reaped = True
        if not any(_process_exists(process_id) for process_id in descendants):
            break
        if not reaped:
            time.sleep(0.01)
    return descendants


def _process_exists(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    return True


def _kill_worker_process_tree() -> NoReturn:
    """Fail closed when the trusted host disappears, including detached descendants."""
    try:
        _terminate_worker_descendants()
    except Exception:
        # Parent loss is terminal. Even if enumeration fails, the worker group
        # must not survive without its trusted host.
        pass
    try:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    except ProcessLookupError:
        pass
    os._exit(1)


def _load_handler(callable_path: str) -> TaskHandler:
    module_name, _, attribute = callable_path.partition(":")
    if not module_name or not attribute:
        raise TaskHostProtocolError(f"invalid capability entrypoint: {callable_path!r}")
    module = importlib.import_module(module_name)
    if "." in attribute:
        class_name, method_name = attribute.rsplit(".", 1)
        handler_cls = getattr(module, class_name)
        instance = handler_cls() if isinstance(handler_cls, type) else handler_cls
        target = getattr(instance, method_name)
    else:
        target = getattr(module, attribute)
    if not callable(target):
        raise TaskHostProtocolError(f"capability entrypoint is not callable: {callable_path!r}")
    return cast(TaskHandler, _AsyncCallableHandler(target))


class _AsyncCallableHandler:
    __slots__ = ("_callable",)

    def __init__(self, callable_impl: Callable[..., Any]) -> None:
        self._callable = callable_impl

    async def execute(self, request: object, context: TaskContext) -> TaskOutcome:
        result = self._callable(request, context)
        if asyncio.iscoroutine(result):
            return await result
        return cast(TaskOutcome, result)

    async def reconcile(
        self, request: object, context: TaskContext, activity: TaskActivitySnapshot
    ) -> TaskActivityReconcileResult:
        result = self._callable(request, context, activity)
        if asyncio.iscoroutine(result):
            result = await result
        return cast(TaskActivityReconcileResult, result)

    async def cancel(
        self, request: object, context: TaskContext, activity: TaskActivitySnapshot
    ) -> TaskActivityCancelResult:
        result = self._callable(request, context, activity)
        if asyncio.iscoroutine(result):
            result = await result
        return cast(TaskActivityCancelResult, result)


def _stream_io(stream: object) -> _BinaryStream:
    return cast(_BinaryStream, getattr(stream, "buffer", stream))


_READ_BUFFER = b""


def _read_chunk(stream: object, size: int) -> bytes:
    stream_io = _stream_io(stream)
    fileno = getattr(stream_io, "fileno", None)
    if callable(fileno):
        fd = cast(Callable[[], int], fileno)()
        if fd >= 0:
            return os.read(fd, size)
    chunk = stream_io.read(size)
    return chunk if chunk else b""


def _read_frame(session_key: bytes, stream: object) -> dict[str, JSONValue]:
    global _READ_BUFFER
    buffer = _READ_BUFFER
    while True:
        if len(buffer) < 40:
            chunk = _read_chunk(stream, max(4096, 40 - len(buffer)))
            if not chunk:
                raise TaskHostProtocolError("worker control stream closed unexpectedly")
            buffer += chunk
        try:
            payload, remainder = decode_authenticated_frame(session_key, buffer)
            _READ_BUFFER = remainder
            break
        except TaskHostProtocolError as error:
            if str(error) != "incomplete authenticated wire frame":
                raise
            chunk = _read_chunk(stream, 4096)
            if not chunk:
                raise TaskHostProtocolError("worker control stream closed unexpectedly") from error
            buffer += chunk
    document = json.loads(payload.decode("utf-8"))
    if not isinstance(document, dict):
        raise TaskHostProtocolError("worker frame must be a mapping")
    return cast(dict[str, JSONValue], document)


def _write_frame(session_key: bytes, stream: object, message: dict[str, JSONValue]) -> None:
    payload = canonical_json_bytes(message)
    data = encode_authenticated_frame(session_key, payload)
    stream_io = _stream_io(stream)
    fileno = getattr(stream_io, "fileno", None)
    if callable(fileno):
        write_all_bytes(cast(Callable[[], int], fileno)(), data)
        return
    stream_io.write(data)
    stream_io.flush()


def _parse_call(
    operation: str, payload: dict[str, JSONValue]
) -> TaskHostExecuteCall | TaskHostReconcileCall | TaskHostCancelCall:
    if operation == "execute":
        return TaskHostExecuteCall.model_validate(payload)
    if operation == "reconcile":
        return TaskHostReconcileCall.model_validate(payload)
    if operation == "cancel":
        return TaskHostCancelCall.model_validate(payload)
    raise TaskHostProtocolError(f"unsupported worker operation: {operation!r}")


def _revoke_secrets(secrets: dict[str, bytes]) -> None:
    for handle in list(secrets):
        material = secrets.pop(handle)
        mutable = bytearray(material)
        for index in range(len(mutable)):
            mutable[index] = 0
        del mutable


async def _run_call(
    handler: TaskHandler,
    operation: str,
    call: TaskHostExecuteCall | TaskHostReconcileCall | TaskHostCancelCall,
    *,
    project_root: Path,
    write_root: Path,
    secrets: SecretPort | None,
    activity_port: _ParentActivityPort | None,
    cancel_requested: Callable[[], bool],
) -> TaskHostCallResult:
    project_root, project_fd = _authenticate_root(
        project_root,
        call.attempt_root.project_root_identity,
        label="project root",
    )
    write_fd: int | None = None
    try:
        write_root, write_fd = _authenticate_root(
            write_root,
            call.attempt_root.write_root_identity,
            label="write root",
        )
        context = TaskContext(
            project_root=project_root,
            write_root=write_root,
            workspace_identity=call.attempt_root.workspace_identity,
            heartbeat=lambda: None,
            cancel_requested=cancel_requested,
            invocation=call.request.invocation,
            activity=activity_port,
            secrets=secrets,
        )
        if operation == "execute":
            execute_call = cast(TaskHostExecuteCall, call)
            outcome = await cast(_AsyncCallableHandler, handler).execute(execute_call.request, context)
            result = TaskHostCallResult(operation="execute", outcome=outcome)
        elif operation == "reconcile":
            reconcile_call = cast(TaskHostReconcileCall, call)
            if not hasattr(handler, "reconcile"):
                raise TaskHostProtocolError("handler does not support reconcile")
            reconcile_result = await cast(_AsyncCallableHandler, handler).reconcile(
                reconcile_call.request, context, reconcile_call.activity
            )
            result = TaskHostCallResult(operation="reconcile", reconcile_result=reconcile_result)
        else:
            cancel_call = cast(TaskHostCancelCall, call)
            if not hasattr(handler, "cancel"):
                raise TaskHostProtocolError("handler does not support cancel")
            cancel_result = await cast(_AsyncCallableHandler, handler).cancel(
                cancel_call.request, context, cancel_call.activity
            )
            result = TaskHostCallResult(operation="cancel", cancel_result=cancel_result)
        _reauthenticate_pinned_root(
            project_root,
            project_fd,
            call.attempt_root.project_root_identity,
            label="project root",
        )
        _reauthenticate_pinned_root(
            write_root,
            write_fd,
            call.attempt_root.write_root_identity,
            label="write root",
        )
        return result
    finally:
        if write_fd is not None:
            os.close(write_fd)
        os.close(project_fd)


def _authenticate_root(path: Path, expected: DirectoryIdentity, *, label: str) -> tuple[Path, int]:
    supplied = Path(path)
    if not supplied.is_absolute():
        raise TaskHostProtocolError(f"{label} must be an absolute authenticated root")
    try:
        resolved = supplied.resolve(strict=True)
    except OSError as error:
        raise TaskHostProtocolError(f"{label} is unavailable") from error
    if supplied != resolved or not resolved.is_dir():
        raise TaskHostProtocolError(f"{label} must be a canonical authenticated root")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            resolved,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        actual = DirectoryIdentity.capture(resolved, descriptor=descriptor)
    except (OSError, ValueError) as error:
        if descriptor is not None:
            os.close(descriptor)
        raise TaskHostProtocolError(f"{label} descriptor identity is unavailable") from error
    assert descriptor is not None
    if actual != expected:
        os.close(descriptor)
        raise TaskHostProtocolError(f"{label} identity differs from the authenticated descriptor")
    return resolved, descriptor


def _reauthenticate_pinned_root(
    path: Path,
    descriptor: int,
    expected: DirectoryIdentity,
    *,
    label: str,
) -> None:
    try:
        actual = DirectoryIdentity.capture(path, descriptor=descriptor)
    except (OSError, ValueError) as error:
        raise TaskHostProtocolError(f"{label} changed while the worker was running") from error
    if actual != expected:
        raise TaskHostProtocolError(f"{label} changed while the worker was running")


def main() -> int:
    global _READ_BUFFER
    _READ_BUFFER = b""
    call_digest = os.environ.get("GRAPH_ENGINE_WORKER_CALL_DIGEST")
    if not call_digest:
        raise TaskHostProtocolError("worker call digest is missing")
    parent_alive_raw = os.environ.get(_PARENT_ALIVE_ENV)
    parent_alive_fd = int(parent_alive_raw) if parent_alive_raw else None
    activity_response_raw = os.environ.get(_ACTIVITY_RESPONSE_ENV)
    activity_response_fd = int(activity_response_raw) if activity_response_raw else None
    cancel_raw = os.environ.get(_CANCEL_ENV)
    cancel_fd = int(cancel_raw) if cancel_raw else None
    parent_supervisor = _install_parent_death_supervision(parent_alive_fd)
    session_key = derive_wire_session_key(
        call_digest=call_digest,
        wire_schema_version=TASK_HOST_WIRE_SCHEMA_VERSION,
    )
    secrets: dict[str, bytes] = {}
    cancelled = {"value": False}
    host_call: TaskHostExecuteCall | TaskHostReconcileCall | TaskHostCancelCall | None = None
    parsed: _WorkerJob | None = None
    startup_host_identity: ExecutionHostLock = pinned_execution_host_lock()
    attested = False

    def cancel_requested() -> bool:
        return cancelled["value"]

    activity_response_stream = (
        os.fdopen(activity_response_fd, "rb", buffering=0) if activity_response_fd is not None else None
    )

    def _watch_cancel() -> None:
        if cancel_fd is None:
            return
        buffer = b""
        while True:
            try:
                chunk = os.read(cancel_fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while True:
                try:
                    payload, buffer = decode_authenticated_frame(session_key, buffer)
                except TaskHostProtocolError as error:
                    if str(error) == "incomplete authenticated wire frame":
                        break
                    return
                document = json.loads(payload.decode("utf-8"))
                if isinstance(document, dict) and document.get("kind") == "cancel":
                    cancelled["value"] = True

    if cancel_fd is not None:
        threading.Thread(target=_watch_cancel, daemon=True).start()

    def activity_send(message: dict[str, JSONValue]) -> dict[str, JSONValue]:
        request_id = uuid.uuid4().hex
        _write_frame(session_key, sys.stdout, {**message, "id": request_id})
        document = _read_frame(session_key, sys.stdin)
        kind = document.get("kind")
        if kind == "activity_error":
            raise TaskHostProtocolError(str(document.get("message") or "activity rpc failed"))
        if kind != "activity_response":
            raise TaskHostProtocolError("expected activity response from parent")
        if str(document.get("id") or "") not in {"", request_id}:
            raise TaskHostProtocolError("activity response id mismatch")
        return document

    try:
        while True:
            frame = _read_frame(session_key, sys.stdin)
            kind = frame.get("kind")
            if kind == "challenge":
                if attested:
                    raise TaskHostProtocolError("worker identity was challenged more than once")
                nonce = str(frame.get("nonce") or "")
                if len(nonce) != 64:
                    raise TaskHostProtocolError("worker identity challenge nonce is invalid")
                current_host_identity = pinned_execution_host_lock()
                _write_frame(
                    session_key,
                    sys.stdout,
                    {
                        "kind": "attestation",
                        "nonce": nonce,
                        "implementation_id": startup_host_identity.implementation_id,
                        "loaded_implementation_digest": (startup_host_identity.implementation_digest),
                        "current_source_digest": current_host_identity.implementation_digest,
                        "wire_schema_version": startup_host_identity.wire_schema_version,
                    },
                )
                attested = True
                continue
            if kind == "job":
                if not attested:
                    raise TaskHostProtocolError("worker identity was not attested before dispatch")
                import_roots = frame.get("handler_import_roots")
                if not isinstance(import_roots, list):
                    raise TaskHostProtocolError("worker import roots must be a list")
                parsed = _WorkerJob(
                    operation=str(frame["operation"]),
                    call=cast(dict[str, JSONValue], frame["call"]),
                    project_root=str(frame["project_root"]),
                    write_root=str(frame["write_root"]),
                    capability_id=str(frame["capability_id"]),
                    capability_entrypoint=str(frame["capability_entrypoint"]),
                    handler_import_roots=tuple(str(item) for item in import_roots),
                )
                for entry in parsed.handler_import_roots:
                    if entry and entry not in sys.path:
                        sys.path.insert(0, entry)
                host_call = _parse_call(parsed.operation, parsed.call)
                continue
            if kind == "secret":
                assert host_call is not None
                handle = str(frame["handle"])
                if handle not in host_call.authorized_secret_handles:
                    raise TaskHostProtocolError("secret handle was not authorized for this call")
                secrets[handle] = bytes.fromhex(str(frame["value_hex"]))
                continue
            if kind == "cancel":
                cancelled["value"] = True
                continue
            if kind != "go":
                raise TaskHostProtocolError("worker expected go frame")
            assert parsed is not None and host_call is not None
            secret_port = authorized_secret_port(secrets) if secrets else None
            handler = _load_handler(parsed.capability_entrypoint)
            activity_port = (
                _ParentActivityPort(activity_send) if host_call.activity_rpc.activity_id is not None else None
            )
            result = asyncio.run(
                _run_call(
                    handler,
                    parsed.operation,
                    host_call,
                    project_root=Path(parsed.project_root),
                    write_root=Path(parsed.write_root),
                    secrets=secret_port,
                    activity_port=activity_port,
                    cancel_requested=cancel_requested,
                )
            )
            live_descendants = _descendant_processes(os.getpid(), detached_only=True)
            if live_descendants:
                _terminate_worker_descendants()
                raise TaskHostProtocolError("handler left live descendant processes")
            _revoke_secrets(secrets)
            _write_frame(
                session_key,
                sys.stdout,
                {"kind": "result", "payload": cast(JSONValue, result.model_dump(mode="json"))},
            )
            return 0
    finally:
        parent_supervisor.begin_normal_shutdown()
        _revoke_secrets(secrets)
        if parent_alive_fd is not None:
            try:
                os.close(parent_alive_fd)
            except OSError:
                pass
        if activity_response_fd is not None:
            try:
                if activity_response_stream is not None:
                    activity_response_stream.close()
            except OSError:
                pass
        if cancel_fd is not None:
            try:
                os.close(cancel_fd)
            except OSError:
                pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TaskHostProtocolError as error:
        sys.stderr.write(str(error))
        raise SystemExit(1) from error
