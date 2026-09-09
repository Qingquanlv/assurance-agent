from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.runtime_binding import AgentRuntimeCapabilities
from agent_runtime_contracts.schema import resolve_result_schema, thaw_json
from graph_engine.plugin_api import FrozenModel
from agent_runtime_opencode.protocol import OPENCODE_RUNTIME_CAPABILITIES, canonical_json_text


def advertised_runtime_capabilities() -> AgentRuntimeCapabilities:
    return OPENCODE_RUNTIME_CAPABILITIES


_IDENTITY_BEARING_TYPES = frozenset(
    {
        "session.idle",
        "session.error",
        "session.status",
        "session.updated",
        "session.deleted",
        "message.updated",
        "message.part.updated",
    }
)
ProviderTerminal = Literal["running", "succeeded", "failed", "canceled"]


class OpenCodeTextPart(FrozenModel):
    type: Literal["text"] = "text"
    text: str = Field(min_length=1)


class OpenCodeModelSelection(FrozenModel):
    providerID: str = Field(min_length=1)
    modelID: str = Field(min_length=1)


class OpenCodePromptAdmissionBody(FrozenModel):
    messageID: str = Field(min_length=1)
    parts: tuple[OpenCodeTextPart, ...] = Field(min_length=1)
    agent: str | None = None
    model: OpenCodeModelSelection | None = None
    tools: Mapping[str, bool] | None = None


class OpenCodeSseProperties(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)
    sessionID: str | None = None


class OpenCodeSseEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)
    type: str = Field(min_length=1)
    properties: OpenCodeSseProperties | None = None


@dataclass(frozen=True, slots=True)
class SseFrame:
    event: str | None
    data: str
    identifier: str | None
    comment: str | None


@dataclass(frozen=True, slots=True)
class SseReduction:
    cursor: str | None
    malformed: bool
    heartbeat_count: int


def prompt_admission_body(agent_run: AgentRunRequest, message_id: str) -> dict[str, Any]:
    parts: list[OpenCodeTextPart] = []
    for instruction in agent_run.instructions:
        if instruction.text_content is not None:
            parts.append(OpenCodeTextPart(text=instruction.text_content))
        else:
            parts.append(OpenCodeTextPart(text=canonical_json_text(thaw_json(instruction.json_content))))
    schema_document = resolve_result_schema(
        agent_run.result_contract.schema_digest,
        schema_document=agent_run.result_contract.schema_document,
    )
    schema_text = canonical_json_text(thaw_json(schema_document))
    parts.append(
        OpenCodeTextPart(
            text=(
                "# Runtime result contract\n\n"
                "Your final assistant response MUST be exactly one JSON object with no "
                "Markdown fence, commentary, completion summary, or trailing text. The object "
                "must validate against the following locked JSON Schema. This runtime contract "
                "overrides any user-facing final-output wording in the supplied skill. It governs "
                "only the final assistant text and does not replace required tool calls or file "
                "writes. Complete and verify every required side effect before returning the final "
                "JSON object.\n\n"
                f"delivery_mode: {agent_run.result_contract.delivery_mode}\n"
                f"schema_id: {agent_run.result_contract.schema_id}\n"
                f"schema_digest: {agent_run.result_contract.schema_digest}\n"
                f"schema: {schema_text}"
            )
        )
    )
    model: OpenCodeModelSelection | None = None
    selected = agent_run.execution.provider_model
    if selected != "provider_default":
        provider, separator, model_id = selected.partition("/")
        model = OpenCodeModelSelection(
            providerID=provider,
            modelID=model_id if separator else provider,
        )
    typed = OpenCodePromptAdmissionBody(
        messageID=message_id,
        parts=tuple(parts),
        agent=agent_run.workspace.agent_profile,
        model=model,
    )
    return typed.model_dump(mode="json", exclude_none=True)


def parse_sse_frames(payload: bytes) -> tuple[SseFrame, ...]:
    text = payload.decode("utf-8")
    frames: list[SseFrame] = []
    event: str | None = None
    identifier: str | None = None
    comment: str | None = None
    data_lines: list[str] = []

    def _flush() -> None:
        nonlocal event, identifier, comment, data_lines
        if event is None and identifier is None and comment is None and not data_lines:
            return
        frames.append(
            SseFrame(
                event=event,
                data="\n".join(data_lines),
                identifier=identifier,
                comment=comment,
            )
        )
        event = None
        identifier = None
        comment = None
        data_lines = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if line == "":
            _flush()
            continue
        if line.startswith(":"):
            comment = line[1:].lstrip()
            continue
        if line.startswith("event:"):
            event = line[6:].lstrip()
            continue
        if line.startswith("id:"):
            identifier = line[3:].lstrip()
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
    _flush()
    return tuple(frames)


def reduce_sse_frames(
    frames: Sequence[SseFrame],
    *,
    session_id: str,
    heartbeat: Callable[[], None],
) -> SseReduction:
    cursor: str | None = None
    heartbeat_count = 0
    for frame in frames:
        if frame.identifier:
            cursor = frame.identifier
        if frame.comment:
            heartbeat()
            heartbeat_count += 1
        event_name = frame.event
        if not frame.data:
            if event_name == "server.heartbeat":
                heartbeat()
                heartbeat_count += 1
            continue
        try:
            parsed = json.loads(frame.data)
        except json.JSONDecodeError:
            if _is_identity_bearing_name(event_name):
                return SseReduction(cursor=cursor, malformed=True, heartbeat_count=heartbeat_count)
            continue
        if not isinstance(parsed, dict):
            if _is_identity_bearing_name(event_name):
                return SseReduction(cursor=cursor, malformed=True, heartbeat_count=heartbeat_count)
            continue
        payload = dict(parsed)
        if "type" not in payload and event_name:
            payload["type"] = event_name
        envelope_type = payload.get("type")
        if envelope_type == "server.heartbeat":
            heartbeat()
            heartbeat_count += 1
            continue
        named = envelope_type if isinstance(envelope_type, str) else event_name
        if not _is_identity_bearing_name(named):
            continue
        try:
            envelope = OpenCodeSseEnvelope.model_validate(payload)
        except ValidationError:
            return SseReduction(cursor=cursor, malformed=True, heartbeat_count=heartbeat_count)
        properties = envelope.properties
        if properties is None or properties.sessionID is None:
            return SseReduction(cursor=cursor, malformed=True, heartbeat_count=heartbeat_count)
        if properties.sessionID != session_id:
            continue
        heartbeat()
        heartbeat_count += 1
    return SseReduction(cursor=cursor, malformed=False, heartbeat_count=heartbeat_count)


def classify_admission(
    record: object, expected: Mapping[str, object]
) -> Literal["exact", "pending", "conflict"]:
    if not isinstance(record, dict):
        return "conflict"
    candidate: object = record.get("admission") if isinstance(record.get("admission"), dict) else record
    if not isinstance(candidate, dict):
        return "conflict"
    expected_id = expected.get("messageID")
    info = candidate.get("info")
    candidate_id = candidate.get("messageID")
    if candidate_id != expected_id and isinstance(info, dict):
        candidate_id = info.get("id")
    if candidate_id == expected_id:
        if _admission_identity_matches(candidate, expected):
            return "exact"
        role = info.get("role") if isinstance(info, dict) else candidate.get("role")
        expected_texts = _text_parts(expected.get("parts"))
        observed_texts = _text_parts(candidate.get("parts"))
        if (
            role == "user"
            and not _admission_model_conflicts(candidate, expected)
            and _texts_partially_admitted(expected_texts, observed_texts)
        ):
            return "pending"
        return "conflict"
    role = info.get("role") if isinstance(info, dict) else candidate.get("role")
    if role != "user":
        return "conflict"
    expected_texts = _text_parts(expected.get("parts"))
    if expected_texts and _text_parts(candidate.get("parts")) == expected_texts:
        return "exact"
    return "conflict"


def _admission_identity_matches(candidate: Mapping[str, object], expected: Mapping[str, object]) -> bool:
    expected_texts = _text_parts(expected.get("parts"))
    if not expected_texts or not _texts_admitted(expected_texts, _text_parts(candidate.get("parts"))):
        return False
    return not _admission_model_conflicts(candidate, expected)


def _admission_model_conflicts(candidate: Mapping[str, object], expected: Mapping[str, object]) -> bool:
    expected_model = _model_identity(expected.get("model"))
    if expected_model is None:
        return False
    observed_model = _model_identity(candidate.get("model"))
    if observed_model is None:
        info = candidate.get("info")
        if isinstance(info, dict):
            observed_model = _model_identity(info.get("model"))
    return observed_model is not None and observed_model != expected_model


def _texts_admitted(expected: tuple[str, ...], observed: tuple[str, ...]) -> bool:
    if expected == observed:
        return True
    haystack = "\n".join(observed)
    cursor = 0
    for piece in expected:
        found = haystack.find(piece, cursor)
        if found < 0:
            return False
        cursor = found + len(piece)
    return True


def _texts_partially_admitted(expected: tuple[str, ...], observed: tuple[str, ...]) -> bool:
    if not expected:
        return False
    if not observed:
        return True
    if len(observed) >= len(expected):
        return False
    for expected_piece, observed_piece in zip(expected, observed, strict=False):
        if expected_piece not in observed_piece and not expected_piece.startswith(observed_piece):
            return False
    return True


def _model_identity(model: object) -> tuple[str, str] | None:
    if not isinstance(model, dict):
        return None
    provider = model.get("providerID")
    model_id = model.get("modelID")
    if isinstance(provider, str) and isinstance(model_id, str) and provider and model_id:
        return (provider, model_id)
    return None


def _text_parts(parts: object) -> tuple[str, ...]:
    if not isinstance(parts, list | tuple):
        return ()
    texts: list[str] = []
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
    return tuple(texts)


def user_prompt_already_admitted(messages: Sequence[object], expected: Mapping[str, object]) -> bool:
    expected_texts = _text_parts(expected.get("parts"))
    if not expected_texts:
        return False
    for message in messages:
        if not isinstance(message, dict):
            continue
        if classify_admission(message, expected) in {"exact", "pending"}:
            return True
    return False


def classify_provider_state(
    *,
    session_id: str,
    status_map: object,
    session: Mapping[str, object],
    messages: Sequence[object],
) -> ProviderTerminal:
    error_kind = _terminal_error_kind(session, messages)
    idle = _idle_from_status_map(status_map, session_id)
    try:
        parse_closed_terminal_result(messages)
        has_result = True
    except ValueError:
        has_result = False
    open_tools = _has_open_tool_work(messages)
    if has_result and not open_tools and error_kind != "failed":
        return "succeeded"
    if error_kind == "canceled":
        return "canceled"
    if error_kind == "failed":
        return "failed"
    if idle and not open_tools and not has_result and _has_result_bearing_assistant(messages):
        return "failed"
    return "running"


_SUCCESS_FINISH_REASONS = frozenset({"stop", "end-turn"})
# `reasoning` carries model deliberation rather than response content, and providers
# emit it alongside the final text of the same message. Content-bearing part types such
# as `file` stay forbidden: they would mean the terminal response is not a lone JSON object.
_RESULT_PART_TYPES = frozenset({"text", "step-start", "step-finish", "patch", "reasoning"})


def parse_closed_terminal_result(messages: Sequence[object]) -> dict[str, Any]:
    result_bearing: list[Mapping[str, object]] = []
    for message in messages:
        if not isinstance(message, Mapping):
            raise ValueError("terminal message is not an object")
        info = message.get("info")
        if not isinstance(info, Mapping):
            raise ValueError("terminal message is missing info")
        role = info.get("role")
        if role == "user":
            continue
        if role != "assistant":
            raise ValueError("terminal message role is not assistant")
        parts = message.get("parts")
        if not isinstance(parts, list):
            raise ValueError("terminal message parts are missing")
        if _nonempty_text_parts(parts):
            finish = info.get("finish")
            if finish is not None and finish not in _SUCCESS_FINISH_REASONS:
                continue
            result_bearing.append(message)
            continue
        for part in parts:
            if not isinstance(part, Mapping):
                raise ValueError("terminal part is not an object")
    if not result_bearing:
        raise ValueError("expected one result-bearing assistant message")
    parsed = tuple(_parse_result_bearing_message(message) for message in result_bearing)
    expected = canonical_json_text(parsed[0])
    if any(canonical_json_text(candidate) != expected for candidate in parsed[1:]):
        raise ValueError("multiple distinct result-bearing assistant messages")
    return parsed[0]


def _has_result_bearing_assistant(messages: Sequence[object]) -> bool:
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        info = message.get("info")
        if not isinstance(info, Mapping) or info.get("role") != "assistant":
            continue
        parts = message.get("parts")
        if isinstance(parts, list) and _nonempty_text_parts(parts):
            return True
    return False


def _nonempty_text_parts(parts: Sequence[object]) -> tuple[Mapping[str, object], ...]:
    found: list[Mapping[str, object]] = []
    for part in parts:
        if not isinstance(part, Mapping) or part.get("type") != "text":
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            found.append(part)
    return tuple(found)


def _parse_result_bearing_message(message: Mapping[str, object]) -> dict[str, Any]:
    info = message.get("info")
    if not isinstance(info, Mapping):
        raise ValueError("terminal message is missing info")
    error = info.get("error")
    if isinstance(error, dict):
        raise ValueError("terminal assistant message has an error")
    finish = info.get("finish")
    timestamp = info.get("time")
    completed = timestamp.get("completed") if isinstance(timestamp, Mapping) else None
    if finish is not None and finish not in _SUCCESS_FINISH_REASONS:
        raise ValueError("terminal assistant message is truncated or incomplete")
    if completed is None and finish not in _SUCCESS_FINISH_REASONS:
        raise ValueError("terminal assistant message is truncated or incomplete")
    parts = message.get("parts")
    if not isinstance(parts, list):
        raise ValueError("terminal message parts are missing")
    step_starts = 0
    step_finishes = 0
    text_parts: list[str] = []
    for part in parts:
        if not isinstance(part, Mapping):
            raise ValueError("terminal part is not an object")
        kind = part.get("type")
        if kind not in _RESULT_PART_TYPES:
            raise ValueError(f"forbidden terminal part type {kind!r}")
        if kind == "step-start":
            step_starts += 1
            continue
        if kind == "step-finish":
            step_finishes += 1
            if part.get("reason") not in _SUCCESS_FINISH_REASONS:
                raise ValueError("step-finish is not successful")
            continue
        if kind in {"patch", "reasoning"}:
            continue
        text = part.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text part must be non-empty")
        text_parts.append(text)
    if step_starts != 1 or step_finishes != 1 or len(text_parts) != 1:
        raise ValueError("closed terminal response must have one text, one step-start, and one step-finish")
    return _exact_json_object(text_parts[0])


def _exact_json_object(text: str) -> dict[str, Any]:
    # Try direct parsing first
    try:
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("terminal text is not one JSON object")
        return parsed
    except json.JSONDecodeError:
        pass
    
    # Try extracting JSON from markdown code block
    stripped = text.strip()
    if stripped.startswith("```"):
        # Find the first ```json or ``` and extract content until closing ```
        lines = stripped.split("\n")
        if lines[0].startswith("```"):
            # Remove first line (```json or ```)
            lines = lines[1:]
            # Find closing ```
            for i, line in enumerate(lines):
                if line.strip() == "```":
                    json_text = "\n".join(lines[:i])
                    # Check that there's no content after the closing ```
                    remaining = "\n".join(lines[i+1:]).strip()
                    if remaining:
                        # There's content after the code block, reject it
                        raise ValueError(f"terminal text is not one JSON object (text preview: {text[:200]!r})")
                    try:
                        parsed = json.loads(json_text)
                        if not isinstance(parsed, dict):
                            raise ValueError("terminal text is not one JSON object")
                        return parsed
                    except json.JSONDecodeError:
                        pass
                    break
    
    # All parsing attempts failed
    raise ValueError(f"terminal text is not one JSON object (text preview: {text[:200]!r})")


def provider_error_message(
    session: Mapping[str, object],
    messages: Sequence[object],
) -> str:
    message = _provider_error_message(session.get("error"))
    if message is not None:
        return message
    for record in messages:
        if not isinstance(record, dict):
            continue
        info = record.get("info")
        if not isinstance(info, dict):
            continue
        message = _provider_error_message(info.get("error"))
        if message is not None:
            return message
    return "provider error"


def _provider_error_message(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    message = payload.get("message")
    if isinstance(message, str) and message:
        return message
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    message = data.get("message")
    if isinstance(message, str) and message:
        return message
    return None


_TRANSIENT_PROVIDER_ERROR_MARKERS = (
    '"code":"cyber_policy"',
    "certificate verification",
    "certificate verify failed",
    "connection closed",
    "connection refused",
    "connection reset",
    "econnrefused",
    "econnreset",
    "enotfound",
    "gateway timeout",
    "network error",
    "request burst",
    "requestbursttoofast",
    "request timeout",
    "service unavailable",
    "temporarily unavailable",
    "temporary failure",
    "timed out",
    "tls handshake",
    "too many requests",
    "toomanyrequests",
)


def provider_error_is_transient(
    session: Mapping[str, object],
    messages: Sequence[object],
) -> bool:
    message = provider_error_message(session, messages).casefold()
    return any(marker in message for marker in _TRANSIENT_PROVIDER_ERROR_MARKERS)


def _is_identity_bearing_name(name: str | None) -> bool:
    return name in _IDENTITY_BEARING_TYPES


def _idle_from_status_map(status_map: object, session_id: str) -> bool:
    """A session is idle unless the provider is actively reporting it busy.

    OpenCode drops a session from the status map once its loop finishes rather than
    reporting an explicit idle record, so absence has to count as idle.
    """
    if not isinstance(status_map, dict):
        return False
    record = status_map.get(session_id)
    if not isinstance(record, dict):
        return True
    return record.get("type") != "busy"


def _has_open_tool_work(messages: Sequence[object]) -> bool:
    for message in messages:
        if not isinstance(message, dict):
            continue
        parts = message.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "tool":
                continue
            state = part.get("state")
            if isinstance(state, dict) and state.get("status") == "running":
                return True
    return False


def _terminal_error_kind(
    session: Mapping[str, object],
    messages: Sequence[object],
) -> Literal["failed", "canceled"] | None:
    if session.get("aborted") is True:
        return "canceled"
    error = session.get("error")
    if isinstance(error, dict):
        if _is_abort_error(error):
            return "canceled"
        return "failed"
    for message in messages:
        if not isinstance(message, dict):
            continue
        info = message.get("info")
        if not isinstance(info, dict):
            continue
        payload = info.get("error")
        if not isinstance(payload, dict):
            continue
        if _is_abort_error(payload):
            return "canceled"
        return "failed"
    return None


def _is_abort_error(payload: Mapping[str, object]) -> bool:
    return payload.get("name") in {"Aborted", "MessageAbortedError"}
