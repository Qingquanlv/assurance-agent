from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agent_runtime_contracts import AgentRunRequest, AgentRunResult, ResultContract
from agent_runtime_contracts.schema import (
    canonical_digest,
    reject_credentials_in_digest_input,
    thaw_json,
    validate_structured_result,
)
from graph_engine.plugin_api import TaskOutcome, TaskRequest

from agent_runtime_cursor.process import CursorProcessReceipt
from agent_runtime_cursor.redaction import failure_message, reject_canaries_in_payload

ADAPTER_ID = "runtime.cursor"
ADAPTER_VERSION = "0.1.0"

_KNOWN_EVENT_TYPES = frozenset({"system", "user", "assistant", "tool_call", "tool_result", "result"})


class CursorProtocolError(ValueError):
    """Raised when stream-json violates the pinned Cursor protocol profile."""


@dataclass(frozen=True, slots=True)
class StreamLimits:
    max_output_bytes: int
    max_line_bytes: int
    max_record_count: int
    max_json_nesting: int
    max_stderr_bytes: int
    max_elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class StreamInput:
    stdout: bytes
    stderr: bytes
    exit_code: int
    elapsed_seconds: float
    expected_cwd: str
    expected_version: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedCursorStream:
    session_id: str | None
    init: dict[str, Any]
    terminal: dict[str, Any]
    exit_code: int


def parse_stream(stream: StreamInput, limits: StreamLimits) -> ParsedCursorStream:
    if stream.elapsed_seconds > limits.max_elapsed_seconds:
        raise CursorProtocolError("elapsed-time limit")
    if len(stream.stderr) > limits.max_stderr_bytes:
        raise CursorProtocolError("stderr byte limit")
    if len(stream.stdout) > limits.max_output_bytes:
        raise CursorProtocolError("total byte limit")

    records = _bounded_records(stream.stdout, limits)
    init: dict[str, Any] | None = None
    terminal: dict[str, Any] | None = None
    session_id: str | None = None
    for record in records:
        event_type = record.get("type")
        if not isinstance(event_type, str) or event_type not in _KNOWN_EVENT_TYPES:
            raise CursorProtocolError("known structural event types are required")
        session_id = _consistent_session(record, session_id)
        if event_type == "system" and record.get("subtype") == "init":
            if init is not None:
                raise CursorProtocolError("exactly one system init is required")
            if terminal is not None:
                raise CursorProtocolError("exactly one system init is required")
            init = record
            _authenticate_init(record, stream)
            continue
        if init is None:
            raise CursorProtocolError("exactly one system init is required")
        if event_type == "result":
            if terminal is not None:
                raise CursorProtocolError("exactly one terminal record is required")
            terminal = record
    if init is None:
        raise CursorProtocolError("exactly one system init is required")
    if terminal is None:
        raise CursorProtocolError("terminal record is required")
    _authenticate_exit(terminal, stream.exit_code)
    return ParsedCursorStream(
        session_id=session_id,
        init=init,
        terminal=terminal,
        exit_code=stream.exit_code,
    )


def _bounded_records(stdout: bytes, limits: StreamLimits) -> list[dict[str, Any]]:
    chunks = stdout.split(b"\n")
    trailing_incomplete = bool(stdout) and not stdout.endswith(b"\n")
    records: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        if chunk == b"":
            continue
        if len(chunk) > limits.max_line_bytes:
            raise CursorProtocolError("line byte limit")
        last = index == len(chunks) - 1
        try:
            decoded = chunk.decode("utf-8")
            payload = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            if last and trailing_incomplete:
                raise CursorProtocolError("truncated final line") from error
            raise CursorProtocolError("malformed JSON") from error
        if not isinstance(payload, dict):
            raise CursorProtocolError("known structural event types are required")
        if _json_nesting(payload) > limits.max_json_nesting:
            raise CursorProtocolError("JSON nesting limit")
        records.append(payload)
        if len(records) > limits.max_record_count:
            raise CursorProtocolError("record count limit")
    return records


def _json_nesting(value: object) -> int:
    if isinstance(value, dict):
        if not value:
            return 1
        return 1 + max(_json_nesting(item) for item in value.values())
    if isinstance(value, list):
        if not value:
            return 1
        return 1 + max(_json_nesting(item) for item in value)
    return 0


def _consistent_session(record: dict[str, Any], current: str | None) -> str | None:
    session = record.get("session_id")
    if session is None:
        return current
    if not isinstance(session, str) or not session:
        raise CursorProtocolError("session identity changed")
    if current is not None and session != current:
        raise CursorProtocolError("session identity changed")
    return session


def _authenticate_init(record: dict[str, Any], stream: StreamInput) -> None:
    cwd = record.get("cwd")
    if cwd != stream.expected_cwd:
        raise CursorProtocolError("init cwd must match the attempt workspace")
    version = record.get("version")
    if stream.expected_version is not None and version is not None and version != stream.expected_version:
        raise CursorProtocolError("init version mismatch")


def _authenticate_exit(terminal: dict[str, Any], exit_code: int) -> None:
    is_error = terminal.get("is_error")
    subtype = terminal.get("subtype")
    success = subtype == "success" and is_error is False
    failed = is_error is True or subtype == "error"
    if success == failed:
        raise CursorProtocolError("exit status is inconsistent with the terminal record")
    if success and exit_code != 0:
        raise CursorProtocolError("exit status is inconsistent with the terminal record")
    if failed and exit_code == 0:
        raise CursorProtocolError("exit status is inconsistent with the terminal record")


def reduce_terminal(
    parsed: ParsedCursorStream,
    *,
    receipt: CursorProcessReceipt,
    agent_run: AgentRunRequest,
    request: TaskRequest,
    canaries: Sequence[str | bytes] = (),
) -> TaskOutcome:
    if parsed.terminal.get("is_error") is True or parsed.terminal.get("subtype") == "error":
        return TaskOutcome.failed(
            "external_effect",
            failure_message(_provider_error_message(parsed.terminal), canaries=canaries),
            retryable=False,
        )
    try:
        structured = _structured_result(parsed.terminal)
        schema = _result_schema(request, agent_run.result_contract)
        validated = validate_structured_result(
            structured,
            schema=schema,
            schema_digest=agent_run.result_contract.schema_digest,
        )
        reject_credentials_in_digest_input(validated)
        reject_canaries_in_payload(validated, canaries=canaries)
    except (TypeError, ValueError) as error:
        return TaskOutcome.failed(
            "invalid_output",
            failure_message(str(error), canaries=canaries),
            retryable=False,
        )
    result_digest = canonical_digest(validated)
    evidence_receipt = receipt.model_copy(update={"stream_session_id": parsed.session_id})
    evidence: dict[str, object] = {
        "process_receipt_digest": canonical_digest(evidence_receipt.model_dump(mode="json")),
        "result_digest": result_digest,
        "terminal": "succeeded",
    }
    if parsed.session_id is not None:
        evidence["stream_session_id"] = parsed.session_id
    reject_credentials_in_digest_input(evidence)
    result = AgentRunResult.model_validate(
        {
            "structured_result": validated,
            "result_digest": result_digest,
            "evidence_digest": canonical_digest(evidence),
            "provider_diff_digest": None,
            "adapter_id": ADAPTER_ID,
            "adapter_version": ADAPTER_VERSION,
            "diagnostics": (),
        }
    )
    return TaskOutcome.succeeded(result.model_dump(mode="json"))


def _structured_result(terminal: Mapping[str, Any]) -> object:
    if "structured_result" in terminal:
        return terminal["structured_result"]
    payload = terminal.get("result")
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str) and payload:
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as error:
            raise ValueError("structured result is missing") from error
        return parsed
    raise ValueError("structured result is missing")


def _result_schema(request: TaskRequest, contract: ResultContract) -> object:
    binding = thaw_json(request.binding_data)
    if not isinstance(binding, dict) or "result_schema" not in binding:
        raise ValueError("result schema is missing")
    schema = binding["result_schema"]
    if canonical_digest(schema) != contract.schema_digest:
        raise ValueError("schema digest is not canonical")
    return schema


def _provider_error_message(terminal: Mapping[str, Any]) -> str:
    for key in ("error", "result"):
        value = terminal.get(key)
        if isinstance(value, str) and value:
            return value
    return "provider error"
