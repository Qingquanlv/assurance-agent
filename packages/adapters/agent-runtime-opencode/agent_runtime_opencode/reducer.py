from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent_runtime_contracts import AgentRunRequest, AgentRunResult, ResultContract
from agent_runtime_contracts.schema import (
    canonical_digest,
    reject_credentials_in_digest_input,
    resolve_result_schema,
    validate_structured_result,
)
from graph_engine.plugin_api import TaskOutcome, TaskRequest

from agent_runtime_opencode.discovery import ADAPTER_VERSION
from agent_runtime_opencode.observation import (
    ProviderTerminal,
    _has_open_tool_work,
    _terminal_error_kind,
    parse_closed_terminal_result,
    provider_error_is_transient,
    provider_error_message,
)
from agent_runtime_opencode.redaction import (
    bound_redacted_messages,
    failure_message,
    redact_json,
    reject_service_canaries_in_result,
)


ADAPTER_ID = "runtime.opencode"
_NON_RETRYABLE_INVALID_OUTPUT_MARKERS = ("canary", "credential")


def _invalid_output_outcome(error: BaseException, *, canaries: Sequence[str | bytes]) -> TaskOutcome:
    message = failure_message(str(error), canaries=canaries)
    folded = message.casefold()
    retryable = not any(marker in folded for marker in _NON_RETRYABLE_INVALID_OUTPUT_MARKERS)
    return TaskOutcome.failed(
        "invalid_output",
        message,
        retryable=retryable,
    )


def reduce_terminal(
    *,
    kind: ProviderTerminal,
    session: Mapping[str, object],
    messages: Sequence[object],
    agent_run: AgentRunRequest,
    request: TaskRequest,
    diff: object | None,
    canaries: Sequence[str | bytes] = (),
    selected_result: Mapping[str, object] | None = None,
) -> TaskOutcome:
    if kind == "running":
        raise ValueError("running provider state is not terminal")
    if kind == "canceled":
        return TaskOutcome.stopped(
            failure_message(provider_error_message(session, messages), canaries=canaries)
        )
    if kind == "failed":
        if _terminal_error_kind(session, messages) is None:
            try:
                parse_closed_terminal_result(messages)
            except ValueError as error:
                return _invalid_output_outcome(error, canaries=canaries)
        transient = provider_error_is_transient(session, messages)
        return TaskOutcome.failed(
            "transient" if transient else "external_effect",
            failure_message(provider_error_message(session, messages), canaries=canaries),
            retryable=True,
        )
    return _reduce_success(
        messages=messages,
        agent_run=agent_run,
        request=request,
        diff=diff,
        canaries=canaries,
        selected_result=selected_result,
    )


def _reduce_success(
    *,
    messages: Sequence[object],
    agent_run: AgentRunRequest,
    request: TaskRequest,
    diff: object | None,
    canaries: Sequence[str | bytes],
    selected_result: Mapping[str, object] | None,
) -> TaskOutcome:
    try:
        structured = (
            dict(selected_result) if selected_result is not None else parse_closed_terminal_result(messages)
        )
        validated = _validate_result_candidate(
            structured,
            agent_run=agent_run,
            request=request,
            canaries=canaries,
        )
    except (TypeError, ValueError) as error:
        return _invalid_output_outcome(error, canaries=canaries)
    result_digest = canonical_digest(validated)
    provider_diff_digest = _diff_digest(diff, canaries=canaries)
    provider, model = _provider_and_model(agent_run)
    evidence = {
        "adapter_id": ADAPTER_ID,
        "history_digest": _history_digest(messages, canaries=canaries),
        "model": model,
        "provider": provider,
        "provider_diff_digest": provider_diff_digest,
        "result_digest": result_digest,
        "terminal": "succeeded",
        "tool_digest": _tool_digest(messages, canaries=canaries),
    }
    evidence = redact_json(evidence, canaries=canaries)
    reject_credentials_in_digest_input(evidence)
    result = AgentRunResult.model_validate(
        {
            "result_payload": validated,
            "result_digest": result_digest,
            "evidence_digest": canonical_digest(evidence),
            "provider_diff_digest": provider_diff_digest,
            "adapter_id": ADAPTER_ID,
            "adapter_version": ADAPTER_VERSION,
            "diagnostics": bound_redacted_messages((), canaries=canaries),
        }
    )
    return TaskOutcome.succeeded(result.model_dump(mode="json"))


def result_candidate_satisfies_contract(
    candidate: Mapping[str, object],
    *,
    agent_run: AgentRunRequest,
    request: TaskRequest,
    canaries: Sequence[str | bytes],
) -> bool:
    try:
        _validate_result_candidate(
            candidate,
            agent_run=agent_run,
            request=request,
            canaries=canaries,
        )
    except (TypeError, ValueError):
        return False
    return True


def select_unique_contract_valid_result(
    messages: Sequence[object],
    *,
    agent_run: AgentRunRequest,
    request: TaskRequest,
    canaries: Sequence[str | bytes],
) -> dict[str, object] | None:
    """Select the one schema-valid closed JSON retry while OpenCode is still busy."""
    if _has_open_tool_work(messages):
        return None
    candidates: dict[str, dict[str, object]] = {}
    for message in messages:
        try:
            candidate = parse_closed_terminal_result((message,))
            validated = _validate_result_candidate(
                candidate,
                agent_run=agent_run,
                request=request,
                canaries=canaries,
            )
        except (TypeError, ValueError):
            continue
        candidates.setdefault(canonical_digest(validated), candidate)
        if len(candidates) > 1:
            return None
    return next(iter(candidates.values()), None)


def _provider_and_model(agent_run: AgentRunRequest) -> tuple[str, str]:
    selected = agent_run.execution.provider_model
    if selected == "provider_default":
        return "provider_default", "provider_default"
    provider, separator, model_id = selected.partition("/")
    return provider, model_id if separator else provider


def _validate_result_candidate(
    candidate: Mapping[str, object],
    *,
    agent_run: AgentRunRequest,
    request: TaskRequest,
    canaries: Sequence[str | bytes],
) -> object:
    schema = _result_schema(request, agent_run.result_contract)
    validated = validate_structured_result(
        candidate,
        schema=schema,
        schema_digest=agent_run.result_contract.schema_digest,
    )
    reject_service_canaries_in_result(validated, canaries=canaries)
    return validated


def _result_schema(request: TaskRequest, contract: ResultContract) -> object:
    del request
    return resolve_result_schema(contract.schema_digest, schema_document=contract.schema_document)


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
