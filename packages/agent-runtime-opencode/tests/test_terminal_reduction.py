from __future__ import annotations

from agent_runtime_contracts import AgentRunResult
from agent_runtime_contracts.schema import canonical_digest
from harness import (  # pyright: ignore[reportMissingImports]
    _completed_engine_invocation,
    _terminal_success_fixture,
    _terminal_success_outcome,
)


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
