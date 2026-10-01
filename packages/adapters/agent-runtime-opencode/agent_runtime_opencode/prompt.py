from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

import httpx
from pydantic import Field

from agent_runtime_contracts import AgentRunRequest
from agent_runtime_contracts.wire.schema import resolve_result_schema, thaw_json
from graph_engine.plugin_api import FrozenModel, TaskActivityReconcileResult

from agent_runtime_opencode.session.discovery import OpenCodeActivityReference
from agent_runtime_opencode.transport.http import OpenCodeHttpClient, canonical_json_text


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


async def _admit_prompt(
    client: OpenCodeHttpClient,
    agent_run: AgentRunRequest,
    reference: OpenCodeActivityReference,
) -> TaskActivityReconcileResult | None:
    session_id = reference.session_id
    if not session_id:
        return TaskActivityReconcileResult(
            status="indeterminate",
            reason="bound session identity is unknown",
        )
    expected_body = prompt_admission_body(agent_run, reference.expected_message_id)
    try:
        record = await client.get_message(session_id, reference.expected_message_id)
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 404:
            raise
        messages = await client.list_messages(session_id)
        if user_prompt_already_admitted(messages, expected_body):
            return None
    else:
        if classify_admission(record, expected_body) == "conflict":
            return TaskActivityReconcileResult(
                status="indeterminate",
                reason="prompt identity conflict",
            )
        return None
    try:
        await client.admit_message(session_id, expected_body)
    except httpx.HTTPStatusError as error:
        if error.response.status_code in {400, 409}:
            return TaskActivityReconcileResult(
                status="indeterminate",
                reason="prompt identity conflict",
            )
        raise
    return None
