from __future__ import annotations

from agent_runtime_contracts import AgentRunRequest, AgentRunResult, ResultContract
from agent_runtime_contracts.schema import canonical_digest, thaw_json
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
    assert result.structured_result == {"ok": True}
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


def _intake_messages(payload: object) -> list[dict[str, object]]:
    import json

    return [
        {
            "info": {"id": "asst-1", "role": "assistant"},
            "parts": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"))}],
        }
    ]


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
                extraction_mode="structured",
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
    assert thaw_json(result.structured_result) == {"output_files": ["qa/changes/CH-1/proposal.md"]}
