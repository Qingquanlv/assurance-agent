from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from agent_runtime_opencode.transport.http import canonical_json_text


ProviderTerminal = Literal["running", "succeeded", "failed", "canceled"]


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
    "request timeout",
    "service unavailable",
    "temporarily unavailable",
    "temporary failure",
    "timed out",
    "tls handshake",
)


def provider_error_is_transient(
    session: Mapping[str, object],
    messages: Sequence[object],
) -> bool:
    message = provider_error_message(session, messages).casefold()
    return any(marker in message for marker in _TRANSIENT_PROVIDER_ERROR_MARKERS)


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
            if info.get("finish") == "tool-calls":
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
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        candidate = _sole_trailing_json_object(text)
        if candidate is None:
            raise ValueError("terminal text is not one JSON object") from error
        return candidate
    if not isinstance(parsed, dict):
        raise ValueError("terminal text is not one JSON object")
    return parsed


def _sole_trailing_json_object(text: str) -> dict[str, Any] | None:
    """Allow prose before exactly one complete object, never multiple objects."""
    decoder = json.JSONDecoder()
    trailing: dict[str, Any] | None = None
    found = False
    start = text.find("{")
    while start != -1:
        try:
            candidate, end = decoder.raw_decode(text, start)
        except (json.JSONDecodeError, ValueError):
            start = text.find("{", start + 1)
            continue
        if found:
            return None
        found = True
        if not text[end:].strip():
            trailing = candidate
        # Skip the entire decoded object, including nested objects and strings.
        start = text.find("{", end)
    return trailing
