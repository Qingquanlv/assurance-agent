"""Provider-neutral prepare → fake adapter → finalize harness."""

from __future__ import annotations

from dataclasses import dataclass

from agent_runtime_contracts import AgentRunRequest, AgentRunResult
from agent_runtime_contracts.wire.schema import canonical_digest
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskHandler, TaskOutcome
from tests.capabilities.conformance import execute_task


class FakeAgentAdapter:
    EVIDENCE_DIGEST = canonical_digest({"kind": "capabilities.fake-evidence", "version": 1})

    def __init__(
        self,
        structured_result: JSONValue,
        *,
        adapter_id: str = "test.fake",
        adapter_version: str = "1.0.0",
    ) -> None:
        self.structured_result = structured_result
        self.adapter_id = adapter_id
        self.adapter_version = adapter_version
        self.recorded_request_bytes: bytes | None = None

    def execute_request(self, request: AgentRunRequest) -> AgentRunResult:
        self.recorded_request_bytes = request.canonical_bytes()
        return AgentRunResult(
            result_payload=self.structured_result,
            result_digest=canonical_digest(self.structured_result),
            evidence_digest=self.EVIDENCE_DIGEST,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
        )


@dataclass(frozen=True, slots=True)
class AgentSkillHarness:
    prepare: TaskHandler
    finalize: TaskHandler

    async def run(
        self,
        business_input: JSONValue,
        binding_data: JSONValue,
        structured_result: JSONValue,
    ) -> TaskOutcome:
        prepared = await _execute(self.prepare, business_input, binding_data=binding_data)
        if prepared.status != "succeeded":
            return prepared
        request = AgentRunRequest.model_validate(prepared.output)
        adapter_result = FakeAgentAdapter(structured_result).execute_request(request)
        return await _execute(
            self.finalize,
            {"agent_result": adapter_result.model_dump(mode="json")},
        )


async def _execute(
    handler: TaskHandler,
    business_input: JSONValue,
    *,
    binding_data: JSONValue = None,
) -> TaskOutcome:
    executed = await execute_task(handler, business_input, binding_data=binding_data)
    return executed.outcome


__all__ = [
    "AgentSkillHarness",
    "FakeAgentAdapter",
]
