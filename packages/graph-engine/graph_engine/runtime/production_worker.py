from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.plugin_api import (
    RecoverableTaskHandler,
    SecretPort,
    TaskActivitySnapshot,
    TaskContext,
    TaskHandler,
    TaskOutcome,
)
from graph_engine.runtime.host_protocol import (
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
)


@dataclass(frozen=True, slots=True)
class _WorkerJob:
    operation: str
    call: dict[str, JSONValue]
    attempt_root: str
    python_path: tuple[str, ...]


class _ParentActivityPort:
    __slots__ = ("_send",)

    def __init__(self, send: Callable[[dict[str, JSONValue]], dict[str, JSONValue]]) -> None:
        self._send = send

    def mark_dispatch_started(self, fingerprint: JSONValue) -> TaskActivitySnapshot:
        response = self._send(
            {"kind": "activity_rpc", "method": "mark_dispatch_started", "args": [fingerprint]}
        )
        return TaskActivitySnapshot.model_validate(response["snapshot"])

    def bind(self, reference: JSONValue) -> TaskActivitySnapshot:
        response = self._send({"kind": "activity_rpc", "method": "bind", "args": [reference]})
        return TaskActivitySnapshot.model_validate(response["snapshot"])


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

    async def reconcile(self, request: object, context: TaskContext, activity: TaskActivitySnapshot) -> object:
        result = self._callable(request, context, activity)
        if asyncio.iscoroutine(result):
            return await result
        return result

    async def cancel(self, request: object, context: TaskContext, activity: TaskActivitySnapshot) -> object:
        result = self._callable(request, context, activity)
        if asyncio.iscoroutine(result):
            return await result
        return result


def _read_exact(stream: Any, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise TaskHostProtocolError("worker control stream closed unexpectedly")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _stream_io(stream: object) -> object:
    return getattr(stream, "buffer", stream)


_READ_BUFFER = b""


def _read_frame(session_key: bytes, stream: object) -> dict[str, JSONValue]:
    global _READ_BUFFER
    buffer_obj = _stream_io(stream)
    buffer = _READ_BUFFER
    while True:
        if len(buffer) < 40:
            chunk = buffer_obj.read(max(4096, 40 - len(buffer)))
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
            chunk = buffer_obj.read(4096)
            if not chunk:
                raise TaskHostProtocolError("worker control stream closed unexpectedly") from error
            buffer += chunk
    document = json.loads(payload.decode("utf-8"))
    if not isinstance(document, dict):
        raise TaskHostProtocolError("worker frame must be a mapping")
    return cast(dict[str, JSONValue], document)


def _write_frame(session_key: bytes, stream: object, message: dict[str, JSONValue]) -> None:
    payload = canonical_json_bytes(message)
    stream_io = _stream_io(stream)
    stream_io.write(encode_authenticated_frame(session_key, payload))
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


async def _run_call(
    handler: TaskHandler,
    operation: str,
    call: TaskHostExecuteCall | TaskHostReconcileCall | TaskHostCancelCall,
    *,
    workspace_root: Path,
    secrets: SecretPort | None,
    activity_port: _ParentActivityPort | None,
    cancel_requested: Callable[[], bool],
) -> TaskHostCallResult:
    context = TaskContext(
        workspace_root=workspace_root,
        heartbeat=lambda: None,
        cancel_requested=cancel_requested,
        invocation=call.request.invocation,
        activity=activity_port,
        secrets=secrets,
    )
    if operation == "execute":
        execute_call = cast(TaskHostExecuteCall, call)
        outcome = await cast(_AsyncCallableHandler, handler).execute(execute_call.request, context)
        return TaskHostCallResult(operation="execute", outcome=outcome)
    if operation == "reconcile":
        reconcile_call = cast(TaskHostReconcileCall, call)
        if not hasattr(handler, "reconcile"):
            raise TaskHostProtocolError("handler does not support reconcile")
        reconcile_result = await cast(_AsyncCallableHandler, handler).reconcile(
            reconcile_call.request, context, reconcile_call.activity
        )
        return TaskHostCallResult(operation="reconcile", reconcile_result=reconcile_result)
    cancel_call = cast(TaskHostCancelCall, call)
    if not hasattr(handler, "cancel"):
        raise TaskHostProtocolError("handler does not support cancel")
    cancel_result = await cast(_AsyncCallableHandler, handler).cancel(
        cancel_call.request, context, cancel_call.activity
    )
    return TaskHostCallResult(operation="cancel", cancel_result=cancel_result)


def main() -> int:
    global _READ_BUFFER
    _READ_BUFFER = b""
    call_digest = os.environ.get("GRAPH_ENGINE_WORKER_CALL_DIGEST")
    if not call_digest:
        raise TaskHostProtocolError("worker call digest is missing")
    session_key = derive_wire_session_key(
        call_digest=call_digest,
        wire_schema_version=TASK_HOST_WIRE_SCHEMA_VERSION,
    )
    secrets: dict[str, bytes] = {}
    cancelled = {"value": False}
    host_call: TaskHostExecuteCall | TaskHostReconcileCall | TaskHostCancelCall | None = None
    parsed: _WorkerJob | None = None

    def cancel_requested() -> bool:
        return cancelled["value"]

    def activity_send(message: dict[str, JSONValue]) -> dict[str, JSONValue]:
        _write_frame(session_key, sys.stdout, message)
        response = _read_frame(session_key, sys.stdin)
        if response.get("kind") != "activity_response":
            raise TaskHostProtocolError("expected activity response from parent")
        return response

    while True:
        frame = _read_frame(session_key, sys.stdin)
        kind = frame.get("kind")
        if kind == "job":
            parsed = _WorkerJob(
                operation=str(frame["operation"]),
                call=cast(dict[str, JSONValue], frame["call"]),
                attempt_root=str(frame["attempt_root"]),
                python_path=tuple(str(item) for item in frame["python_path"]),
            )
            for entry in parsed.python_path:
                if entry not in sys.path:
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
        handler = _load_handler(host_call.capability_entrypoint)
        activity_port = (
            _ParentActivityPort(activity_send)
            if host_call.activity_rpc.activity_id is not None
            else None
        )
        result = asyncio.run(
            _run_call(
                handler,
                parsed.operation,
                host_call,
                workspace_root=Path(parsed.attempt_root),
                secrets=secret_port,
                activity_port=activity_port,
                cancel_requested=cancel_requested,
            )
        )
        _write_frame(
            session_key,
            sys.stdout,
            {"kind": "result", "payload": cast(JSONValue, result.model_dump(mode="json"))},
        )
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TaskHostProtocolError as error:
        sys.stderr.write(str(error))
        raise SystemExit(1) from error
