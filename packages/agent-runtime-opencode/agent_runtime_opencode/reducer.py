from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent_runtime_contracts import AgentRunRequest, AgentRunResult, ResultContract
from agent_runtime_contracts.schema import (
    canonical_digest,
    reject_credentials_in_digest_input,
    thaw_json,
    validate_structured_result,
)
from graph_engine.plugin_api import TaskOutcome, TaskRequest

from agent_runtime_opencode.discovery import ADAPTER_VERSION
from agent_runtime_opencode.observation import (
    ProviderTerminal,
    provider_error_message,
    structured_result_from_messages,
)
from agent_runtime_opencode.redaction import bound_redacted_messages, failure_message, redact_json


ADAPTER_ID = "runtime.opencode"


def reduce_terminal(
    *,
    kind: ProviderTerminal,
    session: Mapping[str, object],
    messages: Sequence[object],
    agent_run: AgentRunRequest,
    request: TaskRequest,
    diff: object | None,
    canaries: Sequence[str | bytes] = (),
) -> TaskOutcome:
    if kind == "running":
        raise ValueError("running provider state is not terminal")
    if kind == "canceled":
        return TaskOutcome.stopped(
            failure_message(provider_error_message(session, messages), canaries=canaries)
        )
    if kind == "failed":
        return TaskOutcome.failed(
            "external_effect",
            failure_message(provider_error_message(session, messages), canaries=canaries),
            retryable=False,
        )
    return _reduce_success(
        messages=messages,
        agent_run=agent_run,
        request=request,
        diff=diff,
        canaries=canaries,
    )


def _reduce_success(
    *,
    messages: Sequence[object],
    agent_run: AgentRunRequest,
    request: TaskRequest,
    diff: object | None,
    canaries: Sequence[str | bytes],
) -> TaskOutcome:
    structured = structured_result_from_messages(messages)
    if not isinstance(structured, dict):
        return TaskOutcome.failed(
            "invalid_output",
            failure_message("structured result is missing", canaries=canaries),
            retryable=False,
        )
    try:
        schema = _result_schema(request, agent_run.result_contract)
        validated = validate_structured_result(
            structured,
            schema=schema,
            schema_digest=agent_run.result_contract.schema_digest,
        )
        reject_credentials_in_digest_input(validated)
    except (TypeError, ValueError) as error:
        return TaskOutcome.failed(
            "invalid_output",
            failure_message(str(error), canaries=canaries),
            retryable=False,
        )
    result_digest = canonical_digest(validated)
    provider_diff_digest = _diff_digest(diff, canaries=canaries)
    evidence = {
        "history_digest": _history_digest(messages, canaries=canaries),
        "provider_diff_digest": provider_diff_digest,
        "result_digest": result_digest,
        "terminal": "succeeded",
        "tool_digest": _tool_digest(messages, canaries=canaries),
    }
    evidence = redact_json(evidence, canaries=canaries)
    reject_credentials_in_digest_input(evidence)
    result = AgentRunResult.model_validate(
        {
            "structured_result": validated,
            "result_digest": result_digest,
            "evidence_digest": canonical_digest(evidence),
            "provider_diff_digest": provider_diff_digest,
            "adapter_id": ADAPTER_ID,
            "adapter_version": ADAPTER_VERSION,
            "diagnostics": bound_redacted_messages((), canaries=canaries),
        }
    )
    return TaskOutcome.succeeded(result.model_dump(mode="json"))


def _result_schema(request: TaskRequest, contract: ResultContract) -> object:
    binding = thaw_json(request.binding_data)
    if not isinstance(binding, dict) or "result_schema" not in binding:
        raise ValueError("result schema is missing")
    schema = binding["result_schema"]
    if canonical_digest(schema) != contract.schema_digest:
        raise ValueError("schema digest is not canonical")
    return schema


def _diff_digest(diff: object | None, *, canaries: Sequence[str | bytes]) -> str | None:
    if diff is None:
        return None
    redacted = redact_json(diff, canaries=canaries)
    reject_credentials_in_digest_input(redacted)
    return canonical_digest(redacted)


def _history_digest(messages: Sequence[object], *, canaries: Sequence[str | bytes]) -> str:
    projection: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        info = message.get("info")
        if not isinstance(info, dict):
            continue
        item: dict[str, Any] = {}
        identifier = info.get("id")
        role = info.get("role")
        if isinstance(identifier, str):
            item["id"] = identifier
        if isinstance(role, str):
            item["role"] = role
        if item:
            projection.append(item)
    redacted = redact_json(projection, canaries=canaries)
    reject_credentials_in_digest_input(redacted)
    return canonical_digest(redacted)


def _tool_digest(messages: Sequence[object], *, canaries: Sequence[str | bytes]) -> str:
    projection: list[dict[str, Any]] = []
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
            status = state.get("status") if isinstance(state, dict) else None
            projection.append({"status": status, "type": "tool"})
    redacted = redact_json(projection, canaries=canaries)
    reject_credentials_in_digest_input(redacted)
    return canonical_digest(redacted)
