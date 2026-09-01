from __future__ import annotations

import asyncio
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ValidationError

from graph_engine.attempts import (
    AttemptExecutionContext,
    AttemptKey,
    AttemptRetryPolicy,
    AttemptTimeoutPolicy,
    resolve_contract,
)
from graph_engine.plugin_api import ResourceClaims

from agent_runtime_contracts import AgentExecutionContract
from agent_runtime_contracts.attempt_executor import (
    CompositeAttemptExecutor,
    StructuredOutputCapabilityError,
    TypedPhaseBundle,
)
from agent_runtime_contracts.runtime_binding import AgentRuntimeCapabilities


class CaseDesignInput(BaseModel):
    change_id: str
    path: Literal["primary", "repair"] = "primary"


class CaseDesignPrepared(BaseModel):
    change_id: str
    path: Literal["primary", "repair"]
    prompt: str


class CaseDesignAgentResult(BaseModel):
    output_files: tuple[str, ...]


class CaseDesignOutput(BaseModel):
    status: str
    output_files: tuple[str, ...]
    path: Literal["primary", "repair"]


class RecordingPrepare:
    def __init__(self, prepared: CaseDesignPrepared) -> None:
        self.prepared = prepared
        self.seen_input: CaseDesignInput | None = None

    async def execute(
        self,
        validated_input: CaseDesignInput,
        context: AttemptExecutionContext,
    ) -> CaseDesignPrepared:
        del context
        self.seen_input = validated_input
        return self.prepared


class RecordingRuntime:
    def __init__(self, result: object) -> None:
        self.result = result
        self.seen_prepared: CaseDesignPrepared | None = None
        self.seen_schema: dict[str, Any] | None = None

    async def execute(
        self,
        prepared: CaseDesignPrepared,
        context: AttemptExecutionContext,
        *,
        schema: dict[str, Any],
    ) -> object:
        del context
        self.seen_prepared = prepared
        self.seen_schema = schema
        return self.result


class RecordingFinalize:
    def __init__(self, output: CaseDesignOutput) -> None:
        self.output = output
        self.seen: TypedPhaseBundle[CaseDesignPrepared, CaseDesignAgentResult] | None = None

    async def execute(
        self,
        bundle: TypedPhaseBundle[CaseDesignPrepared, CaseDesignAgentResult],
        context: AttemptExecutionContext,
    ) -> CaseDesignOutput:
        del context
        self.seen = bundle
        return self.output


def _context() -> AttemptExecutionContext:
    return AttemptExecutionContext(
        invocation_id="inv-1",
        public_entrypoint="intake",
        semantic_node_id="case-design",
        attempt_key=AttemptKey(digest="a" * 64),
        fencing_token=1,
    )


def _contract(
    *, requires_provider_schema: bool
) -> AgentExecutionContract[CaseDesignInput, CaseDesignAgentResult, CaseDesignOutput]:
    return AgentExecutionContract(
        contract_id="assurance.intake.agent.case-design.v1",
        owner_id="assurance.intake",
        prepare_handler_id="assurance.intake.case-design.prepare",
        finalize_handler_id="assurance.intake.case-design.finalize",
        skill_id="aa-case-design",
        agent_profile="assurance-v1-doc-author",
        input_model=CaseDesignInput,
        agent_result_model=CaseDesignAgentResult,
        output_model=CaseDesignOutput,
        requires_provider_schema=requires_provider_schema,
        resources=ResourceClaims(),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=60),
        validators=(),
    )


def _executor(
    *,
    requires_provider_schema: bool,
    provider_schema: bool,
    prepare: RecordingPrepare,
    runtime: RecordingRuntime,
    finalize: RecordingFinalize,
) -> CompositeAttemptExecutor[CaseDesignInput, CaseDesignPrepared, CaseDesignAgentResult, CaseDesignOutput]:
    return CompositeAttemptExecutor(
        contract=_contract(requires_provider_schema=requires_provider_schema),
        prepare=prepare,
        runtime=runtime,
        finalize=finalize,
        capabilities=AgentRuntimeCapabilities(provider_schema=provider_schema),
    )


def test_composite_executor_passes_typed_phase_bundle_to_finalize() -> None:
    validated_input = CaseDesignInput(change_id="CH-1", path="primary")
    prepared_value = CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases")
    expected_agent_result = CaseDesignAgentResult(output_files=("qa/changes/CH-1/proposal.md",))
    expected_output = CaseDesignOutput(
        status="committed",
        output_files=expected_agent_result.output_files,
        path="primary",
    )
    prepare = RecordingPrepare(prepared_value)
    runtime = RecordingRuntime(expected_agent_result)
    finalize = RecordingFinalize(expected_output)
    executor = _executor(
        requires_provider_schema=False,
        provider_schema=False,
        prepare=prepare,
        runtime=runtime,
        finalize=finalize,
    )
    context = _context()

    output = asyncio.run(executor.execute(validated_input, context))

    assert prepare.seen_input == validated_input
    assert runtime.seen_schema == CaseDesignAgentResult.model_json_schema()
    assert finalize.seen is not None
    assert finalize.seen.prepared == prepared_value
    assert finalize.seen.agent_result == expected_agent_result
    assert output == expected_output


def test_required_provider_schema_fails_closed_when_adapter_lacks_it() -> None:
    validated_input = CaseDesignInput(change_id="CH-1")
    context = _context()
    prepare = RecordingPrepare(CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases"))
    runtime = RecordingRuntime(CaseDesignAgentResult(output_files=("qa/changes/CH-1/proposal.md",)))
    finalize = RecordingFinalize(
        CaseDesignOutput(status="committed", output_files=("qa/changes/CH-1/proposal.md",), path="primary")
    )
    executor_without_schema = _executor(
        requires_provider_schema=True,
        provider_schema=False,
        prepare=prepare,
        runtime=runtime,
        finalize=finalize,
    )

    with pytest.raises(StructuredOutputCapabilityError):
        asyncio.run(executor_without_schema.execute(validated_input, context))

    assert prepare.seen_input is None
    assert runtime.seen_schema is None
    assert finalize.seen is None


def test_composite_executor_validates_agent_result_locally_even_when_provider_schema_is_advertised() -> None:
    validated_input = CaseDesignInput(change_id="CH-1")
    prepare = RecordingPrepare(CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases"))
    runtime = RecordingRuntime({"not": "an-agent-result"})
    finalize = RecordingFinalize(
        CaseDesignOutput(status="committed", output_files=("qa/changes/CH-1/proposal.md",), path="primary")
    )
    executor = _executor(
        requires_provider_schema=True,
        provider_schema=True,
        prepare=prepare,
        runtime=runtime,
        finalize=finalize,
    )

    with pytest.raises(ValidationError):
        asyncio.run(executor.execute(validated_input, context=_context()))

    assert runtime.seen_schema == CaseDesignAgentResult.model_json_schema()
    assert finalize.seen is None


def test_adapter_constructs_core_resolved_attempt_contract() -> None:
    prepare = RecordingPrepare(CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases"))
    runtime = RecordingRuntime(CaseDesignAgentResult(output_files=("qa/changes/CH-1/proposal.md",)))
    finalize = RecordingFinalize(
        CaseDesignOutput(status="committed", output_files=("qa/changes/CH-1/proposal.md",), path="primary")
    )
    executor = _executor(
        requires_provider_schema=False,
        provider_schema=False,
        prepare=prepare,
        runtime=runtime,
        finalize=finalize,
    )

    resolved = executor.resolve()
    again = resolve_contract(executor.to_task_contract(), executor=executor)

    assert resolved.contract_digest == again.contract_digest
    assert "executor" not in resolved.canonical_projection()
    assert resolved.executor is executor
    assert resolved.contract.input_model is CaseDesignInput
    assert resolved.contract.output_model is CaseDesignOutput
    assert resolved.contract.handler_id == "assurance.intake.case-design.prepare"


def test_case_design_composite_attempt_preserves_both_legacy_prepare_consumers() -> None:
    validated_input = CaseDesignInput(change_id="CH-1", path="primary")
    prepared_value = CaseDesignPrepared(change_id="CH-1", path="primary", prompt="primary case-design")
    expected_agent_result = CaseDesignAgentResult(
        output_files=("qa/changes/CH-1/proposal.md", "qa/changes/CH-1/.qa.yaml")
    )
    expected_output = CaseDesignOutput(
        status="committed",
        output_files=expected_agent_result.output_files,
        path="primary",
    )
    prepare = RecordingPrepare(prepared_value)
    runtime = RecordingRuntime(expected_agent_result.model_dump())
    finalize = RecordingFinalize(expected_output)
    executor = _executor(
        requires_provider_schema=False,
        provider_schema=False,
        prepare=prepare,
        runtime=runtime,
        finalize=finalize,
    )

    output = asyncio.run(executor.execute(validated_input, _context()))

    assert runtime.seen_prepared == prepared_value
    assert finalize.seen is not None
    assert finalize.seen.prepared == prepared_value
    assert finalize.seen.agent_result == expected_agent_result
    assert output == expected_output


def test_case_design_repair_composite_attempt_preserves_both_legacy_prepare_consumers() -> None:
    validated_input = CaseDesignInput(change_id="CH-1", path="repair")
    prepared_value = CaseDesignPrepared(change_id="CH-1", path="repair", prompt="repair case-design")
    expected_agent_result = CaseDesignAgentResult(output_files=("qa/changes/CH-1/cases/api/case.yaml",))
    expected_output = CaseDesignOutput(
        status="repaired",
        output_files=expected_agent_result.output_files,
        path="repair",
    )
    prepare = RecordingPrepare(prepared_value)
    runtime = RecordingRuntime(expected_agent_result.model_dump())
    finalize = RecordingFinalize(expected_output)
    executor = _executor(
        requires_provider_schema=False,
        provider_schema=False,
        prepare=prepare,
        runtime=runtime,
        finalize=finalize,
    )

    output = asyncio.run(executor.execute(validated_input, _context()))

    assert runtime.seen_prepared == prepared_value
    assert finalize.seen is not None
    assert finalize.seen.prepared == prepared_value
    assert finalize.seen.agent_result == expected_agent_result
    assert output == expected_output
