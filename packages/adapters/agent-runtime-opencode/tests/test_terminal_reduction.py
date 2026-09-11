from __future__ import annotations

import json

import pytest

from agent_runtime_contracts import AgentRunRequest, AgentRunResult, ResultContract
from agent_runtime_contracts.schema import canonical_digest, thaw_json
from agent_runtime_opencode.observation import classify_provider_state, parse_closed_terminal_result
from agent_runtime_opencode.reducer import reduce_terminal
from harness import (  # pyright: ignore[reportMissingImports]
    _completed_engine_invocation,
    _terminal_success_fixture,
    _terminal_success_outcome,
    agent_run_request,
    task_request,
)


_INTAKE_RESULT_SCHEMA = {
    "additionalProperties": False,
    "properties": {
        "output_files": {
            "items": {"title": "Output Files", "type": "string"},
            "type": "array",
        }
    },
    "required": ["output_files"],
    "title": "ArtifactListResultV1",
    "type": "object",
}


_FORBIDDEN_RESULT_FIELDS = (
    "messages",
    "reasoning",
    "tool_calls",
    "model_history",
    "token_count",
    "cost",
)


async def test_terminal_reduction_keeps_full_session_state_out_of_result() -> None:
    outcome = await _terminal_success_outcome()
    result = AgentRunResult.model_validate(outcome.output)
    encoded = result.model_dump_json()
    for forbidden in _FORBIDDEN_RESULT_FIELDS:
        assert forbidden not in encoded
    assert thaw_json(result.result_payload) == {"ok": True}
    assert result.result_digest == canonical_digest({"ok": True})
    assert result.adapter_id == "runtime.opencode"
    assert result.adapter_version == "0.1.0"
    assert result.provider_diff_digest is None
    assert len(result.evidence_digest) == 64


async def test_supported_diff_digest_is_included_when_available() -> None:
    fixture = _terminal_success_fixture()
    fixture.fake.diff_payload = {"files": ["result.json"]}
    try:
        outcome = await fixture.handler.execute(fixture.request, fixture.context)
        result = AgentRunResult.model_validate(outcome.output)
        assert result.provider_diff_digest == canonical_digest({"files": ["result.json"]})
        encoded = result.model_dump_json()
        assert "result.json" not in encoded
        for forbidden in _FORBIDDEN_RESULT_FIELDS:
            assert forbidden not in encoded
    finally:
        fixture.close()


def test_post_success_replay_needs_no_opencode_access() -> None:
    fixture = _completed_engine_invocation()
    try:
        fixture.fake.reject_all_requests()
        replayed = fixture.reopen_and_run()
        assert replayed.terminal == "succeeded"
        AgentRunResult.model_validate(replayed.outcome.output)
    finally:
        fixture.close()


def _closed_assistant(
    payload: object,
    *,
    extra_parts: tuple[dict[str, object], ...] = (),
    text: str | None = None,
    finish: str = "stop",
    error: dict[str, object] | None = None,
    truncated: bool = False,
) -> list[dict[str, object]]:
    body = json.dumps(payload, separators=(",", ":")) if text is None else text
    finish_reason = "length" if truncated else finish
    parts: list[dict[str, object]] = [
        {"type": "step-start"},
        {"type": "text", "text": body},
        {"type": "step-finish", "reason": finish_reason},
        *extra_parts,
    ]
    info: dict[str, object] = {
        "id": "asst-1",
        "role": "assistant",
        "time": {"created": 1, "completed": None if truncated else 2},
        "finish": finish_reason,
    }
    if error is not None:
        info["error"] = error
    return [{"info": info, "parts": parts}]


def _intake_messages(payload: object) -> list[dict[str, object]]:
    return _closed_assistant(payload)


def test_product_result_schema_missing_document_is_invalid_output() -> None:
    agent_run = agent_run_request(result_schema=_INTAKE_RESULT_SCHEMA)
    outcome = reduce_terminal(
        kind="succeeded",
        session={},
        messages=_intake_messages({"output_files": ["qa/changes/CH-1/proposal.md"]}),
        agent_run=agent_run,
        request=task_request(agent_run),
        diff=None,
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is False
    assert outcome.failure.message == "result schema is missing"


def test_product_result_schema_on_contract_validates() -> None:
    digest = canonical_digest(_INTAKE_RESULT_SCHEMA)
    base = agent_run_request(result_schema=_INTAKE_RESULT_SCHEMA)
    agent_run = AgentRunRequest.model_validate(
        {
            **base.model_dump(mode="json"),
            "result_contract": ResultContract(
                schema_id="assurance.intake.result.intake.v1",
                schema_digest=digest,
                delivery_mode="assistant_json_local_v1",
                schema_document=_INTAKE_RESULT_SCHEMA,
            ).model_dump(mode="json"),
        }
    )
    outcome = reduce_terminal(
        kind="succeeded",
        session={},
        messages=_intake_messages({"output_files": ["qa/changes/CH-1/proposal.md"]}),
        agent_run=agent_run,
        request=task_request(agent_run),
        diff=None,
    )
    assert outcome.status == "succeeded"
    result = AgentRunResult.model_validate(outcome.output)
    assert thaw_json(result.result_payload) == {"output_files": ["qa/changes/CH-1/proposal.md"]}


def test_closed_terminal_accepts_one_json_text_step_pair_and_optional_patch() -> None:
    messages = _closed_assistant(
        {"ok": True},
        extra_parts=({"type": "patch", "hash": "abc", "files": ["hint.json"]},),
    )
    assert parse_closed_terminal_result(messages) == {"ok": True}


def test_closed_terminal_recovers_one_json_object_after_leading_commentary() -> None:
    messages = _closed_assistant(
        {"ok": True},
        text='Review artifacts are written. Final result:\n\n{"ok":true}',
    )

    assert parse_closed_terminal_result(messages) == {"ok": True}


def test_repeated_identical_closed_results_are_one_logical_terminal_result() -> None:
    first = _closed_assistant({"ok": True})
    second = _closed_assistant({"ok": True})
    second[0]["info"]["id"] = "asst-2"  # type: ignore[index]
    messages = first + second

    assert parse_closed_terminal_result(messages) == {"ok": True}
    assert (
        classify_provider_state(
            session_id="ses_busy",
            status_map={"ses_busy": {"type": "busy"}},
            session={"id": "ses_busy"},
            messages=messages,
        )
        == "succeeded"
    )


def test_idle_history_ignores_intermediate_tool_call_text_before_closed_result() -> None:
    messages = [
        {
            "info": {
                "id": "asst-tool",
                "role": "assistant",
                "time": {"created": 1, "completed": 2},
                "finish": "tool-calls",
            },
            "parts": [
                {"type": "step-start"},
                {"type": "text", "text": "I will verify the output before returning it."},
                {"type": "tool", "state": {"status": "completed"}},
                {"type": "step-finish", "reason": "tool-calls"},
            ],
        },
        *_closed_assistant({"ok": True}),
    ]

    kind = classify_provider_state(
        session_id="ses_idle",
        status_map={"ses_idle": {"type": "idle"}},
        session={"id": "ses_idle"},
        messages=messages,
    )
    outcome = reduce_terminal(
        kind=kind,
        session={"id": "ses_idle"},
        messages=messages,
        agent_run=agent_run_request(),
        request=task_request(agent_run_request()),
        diff=None,
    )

    assert kind == "succeeded"
    assert outcome.status == "succeeded"
    result = AgentRunResult.model_validate(outcome.output)
    assert thaw_json(result.result_payload) == {"ok": True}


def test_closed_terminal_rejects_distinct_result_messages() -> None:
    messages = _closed_assistant({"ok": True}) + _closed_assistant({"ok": False})

    with pytest.raises(ValueError, match="distinct result-bearing"):
        parse_closed_terminal_result(messages)


@pytest.mark.parametrize(
    "messages",
    [
        [
            {
                "info": {"id": "asst-1", "role": "assistant", "time": {"created": 1, "completed": 2}},
                "parts": [
                    {"type": "step-start"},
                    {"type": "text", "text": '{"ok":true}'},
                    {"type": "text", "text": "extra commentary"},
                    {"type": "step-finish", "reason": "stop"},
                ],
            }
        ],
        [
            {
                "info": {"id": "asst-1", "role": "assistant", "time": {"created": 1, "completed": 2}},
                "parts": [
                    {"type": "step-start"},
                    {"type": "text", "text": '{"ok":true}{"ok":true}'},
                    {"type": "step-finish", "reason": "stop"},
                ],
            }
        ],
        _closed_assistant({"ok": True}, extra_parts=({"type": "tool", "state": {"status": "completed"}},)),
        _closed_assistant({"ok": True}, extra_parts=({"type": "file", "filename": "notes.md"},)),
        _closed_assistant({"ok": True}, extra_parts=({"type": "unknown", "text": "x"},)),
        _closed_assistant({"ok": True}, error={"name": "ProviderError", "message": "failed"}),
        _closed_assistant({"ok": True}, truncated=True),
    ],
)
def test_closed_terminal_rejects_ambiguous_or_unsafe_parts(messages: list[dict[str, object]]) -> None:
    with pytest.raises(ValueError):
        parse_closed_terminal_result(messages)


def test_idle_non_closed_history_reduces_to_invalid_output() -> None:
    messages = [
        {
            "info": {
                "id": "asst-1",
                "role": "assistant",
                "time": {"created": 1, "completed": 2},
                "finish": "stop",
            },
            "parts": [
                {"type": "step-start"},
                {"type": "text", "text": '{"ok": true}'},
                {"type": "text", "text": "extra commentary"},
            ],
        }
    ]
    kind = classify_provider_state(
        session_id="ses_idle",
        status_map={"ses_idle": {"type": "idle"}},
        session={"id": "ses_idle"},
        messages=messages,
    )
    assert kind != "running"
    outcome = reduce_terminal(
        kind=kind,
        session={"id": "ses_idle"},
        messages=messages,
        agent_run=agent_run_request(),
        request=task_request(agent_run_request()),
        diff=None,
    )
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is False


def test_intermediate_reasoning_then_closed_terminal_reduces_success() -> None:
    messages = [
        {
            "info": {"id": "msg_tool", "role": "assistant"},
            "parts": [
                {"type": "reasoning", "text": "planning the write"},
                {"type": "file", "filename": "scratch.md"},
                {"type": "unknown", "text": "provider noise"},
                {"type": "tool", "state": {"status": "completed"}},
            ],
        },
        *_closed_assistant({"ok": True}),
    ]
    assert parse_closed_terminal_result(messages) == {"ok": True}
    kind = classify_provider_state(
        session_id="ses_idle",
        status_map={"ses_idle": {"type": "idle"}},
        session={"id": "ses_idle"},
        messages=messages,
    )
    assert kind == "succeeded"
    outcome = reduce_terminal(
        kind=kind,
        session={"id": "ses_idle"},
        messages=messages,
        agent_run=agent_run_request(),
        request=task_request(agent_run_request()),
        diff=None,
    )
    assert outcome.status == "succeeded"
    result = AgentRunResult.model_validate(outcome.output)
    assert thaw_json(result.result_payload) == {"ok": True}


def test_reduce_terminal_validates_local_result_and_keeps_bounded_identity_evidence() -> None:
    selected = agent_run_request().model_copy(
        update={
            "execution": agent_run_request().execution.model_copy(
                update={"provider_model": "openai/gpt-5.6-terra"}
            )
        }
    )
    outcome = reduce_terminal(
        kind="succeeded",
        session={},
        messages=_closed_assistant(
            {"ok": True},
            extra_parts=({"type": "patch", "hash": "abc", "files": ["hint.json"]},),
        ),
        agent_run=selected,
        request=task_request(selected),
        diff=None,
    )
    assert outcome.status == "succeeded"
    result = AgentRunResult.model_validate(outcome.output)
    assert thaw_json(result.result_payload) == {"ok": True}
    assert result.adapter_id == "runtime.opencode"
    encoded = result.model_dump_json()
    assert "hint.json" not in encoded
    expected_evidence = {
        "adapter_id": "runtime.opencode",
        "history_digest": canonical_digest([{"id": "asst-1", "role": "assistant"}]),
        "model": "gpt-5.6-terra",
        "provider": "openai",
        "provider_diff_digest": None,
        "result_digest": canonical_digest({"ok": True}),
        "terminal": "succeeded",
        "tool_digest": canonical_digest([]),
    }
    assert result.evidence_digest == canonical_digest(expected_evidence)


def test_model_reported_file_manifest_is_not_workspace_proof() -> None:
    digest = canonical_digest(_INTAKE_RESULT_SCHEMA)
    base = agent_run_request(result_schema=_INTAKE_RESULT_SCHEMA)
    agent_run = AgentRunRequest.model_validate(
        {
            **base.model_dump(mode="json"),
            "result_contract": ResultContract(
                schema_id="assurance.intake.result.intake.v1",
                schema_digest=digest,
                delivery_mode="assistant_json_local_v1",
                schema_document=_INTAKE_RESULT_SCHEMA,
            ).model_dump(mode="json"),
        }
    )
    outcome = reduce_terminal(
        kind="succeeded",
        session={},
        messages=_closed_assistant({"output_files": ["qa/changes/CH-1/proposal.md"]}),
        agent_run=agent_run,
        request=task_request(agent_run),
        diff=None,
    )
    assert outcome.status == "succeeded"
    result = AgentRunResult.model_validate(outcome.output)
    assert thaw_json(result.result_payload) == {"output_files": ["qa/changes/CH-1/proposal.md"]}
    expected_evidence = {
        "adapter_id": "runtime.opencode",
        "history_digest": canonical_digest([{"id": "asst-1", "role": "assistant"}]),
        "model": "provider_default",
        "provider": "provider_default",
        "provider_diff_digest": None,
        "result_digest": canonical_digest({"output_files": ["qa/changes/CH-1/proposal.md"]}),
        "terminal": "succeeded",
        "tool_digest": canonical_digest([]),
    }
    assert result.evidence_digest == canonical_digest(expected_evidence)
    assert "files" not in expected_evidence
    assert "output_files" not in expected_evidence
