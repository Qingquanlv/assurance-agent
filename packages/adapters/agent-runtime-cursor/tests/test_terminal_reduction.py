from __future__ import annotations

from pathlib import Path

from agent_runtime_contracts import AgentRunResult, ResultContract
from agent_runtime_contracts.schema import canonical_digest, thaw_json
from agent_runtime_cursor.process import CursorProcessReceipt
from fake_process_host import FakeConfinedProcessHost  # pyright: ignore[reportMissingImports]
from cursor_harness import (  # pyright: ignore[reportMissingImports]
    agent_run,
    complete_stream,
    error_stream,
    execute_fixture,
)


_FORBIDDEN_RESULT_FIELDS = (
    "assistant",
    "tool_call",
    "working",
    "transient-stderr",
    "raw_stream",
    "stderr",
)


async def test_terminal_reduction_returns_schema_valid_agent_run_result(tmp_path: Path) -> None:
    fixture = execute_fixture(tmp_path)
    fixture.host.stderr = b"transient-stderr"
    outcome = await fixture.handler.execute(fixture.request, fixture.context)
    result = AgentRunResult.model_validate(outcome.output)
    encoded = result.model_dump_json()
    for forbidden in _FORBIDDEN_RESULT_FIELDS:
        assert forbidden not in encoded
    assert "dispatch" not in encoded
    assert "sess-1" not in encoded
    assert result.result_payload == {"ok": True}
    assert result.result_digest == canonical_digest({"ok": True})
    assert result.adapter_id == "runtime.cursor"
    assert result.adapter_version == "0.1.0"
    assert result.provider_diff_digest is None
    assert len(result.evidence_digest) == 64
    receipt = CursorProcessReceipt.model_validate(thaw_json(fixture.port.snapshot.reference))
    assert receipt.stream_session_id is None
    assert "--resume" not in fixture.host.launches[0].argv


async def test_product_result_schema_on_contract_validates(tmp_path: Path) -> None:
    schema = {
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
    cwd = str(tmp_path.resolve())
    host = FakeConfinedProcessHost(
        stdout=complete_stream(cwd, result={"output_files": ["qa/changes/CH-1/proposal.md"]}),
    )
    run = agent_run(
        result_contract=ResultContract(
            schema_id="assurance.intake.result.intake.v1",
            schema_digest=canonical_digest(schema),
            delivery_mode="assistant_json_local_v1",
            schema_document=schema,
        )
    )
    fixture = execute_fixture(tmp_path, host, agent_run=run)
    outcome = await fixture.handler.execute(fixture.request, fixture.context)
    result = AgentRunResult.model_validate(outcome.output)
    assert thaw_json(result.result_payload) == {"output_files": ["qa/changes/CH-1/proposal.md"]}


async def test_terminal_reduction_rejects_result_schema_failure(tmp_path: Path) -> None:
    cwd = str(tmp_path.resolve())
    host = FakeConfinedProcessHost(
        stdout=complete_stream(cwd, result={"ok": True, "tokens": 9}),
    )
    fixture = execute_fixture(tmp_path, host)
    outcome = await fixture.handler.execute(fixture.request, fixture.context)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "invalid_output"
    assert outcome.failure.retryable is False


async def test_terminal_reduction_maps_complete_provider_error(tmp_path: Path) -> None:
    cwd = str(tmp_path.resolve())
    host = FakeConfinedProcessHost(stdout=error_stream(cwd), exit_code=1)
    fixture = execute_fixture(tmp_path, host)
    outcome = await fixture.handler.execute(fixture.request, fixture.context)
    assert outcome.status == "failed"
    assert outcome.failure is not None
    assert outcome.failure.kind == "external_effect"
    assert outcome.failure.retryable is False
    assert "exploded" in outcome.failure.message
    encoded = outcome.model_dump_json()
    assert "working" not in encoded
