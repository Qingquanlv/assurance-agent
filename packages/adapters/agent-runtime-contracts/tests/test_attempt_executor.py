from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

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

from agent_runtime_contracts import AgentExecutionContract, AgentRunResult, canonical_digest
from agent_runtime_contracts.attempt_executor import (
    RawAgentRuntimeOutcome,
    RawFinalizeBundle,
    ReadOnlyRawWorkspace,
    ResolvedRawAgentExecutor,
)
from agent_runtime_contracts.schema import thaw_json


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


_EVIDENCE = "b" * 64


def _run_result(payload: object) -> AgentRunResult:
    thawed = thaw_json(payload)
    return AgentRunResult.model_validate(
        {
            "result_payload": thawed,
            "result_digest": canonical_digest(thawed),
            "evidence_digest": _EVIDENCE,
            "adapter_id": "agent-runtime-fixture",
            "adapter_version": "1.0.0",
        }
    )


class RecordingPrepare:
    def __init__(self, prepared: CaseDesignPrepared, order: list[str]) -> None:
        self.prepared = prepared
        self.order = order
        self.seen_input: CaseDesignInput | None = None

    async def execute(
        self,
        validated_input: CaseDesignInput,
        context: AttemptExecutionContext,
    ) -> CaseDesignPrepared:
        del context
        self.order.append("prepare")
        self.seen_input = validated_input
        return self.prepared


class RecordingRuntime:
    def __init__(
        self,
        result: object,
        workspace: ReadOnlyRawWorkspace,
        order: list[str],
    ) -> None:
        self.result = result
        self.workspace = workspace
        self.order = order
        self.seen_prepared: CaseDesignPrepared | None = None
        self.seen_schema: object = None

    async def execute(
        self,
        prepared: CaseDesignPrepared,
        context: AttemptExecutionContext,
    ) -> RawAgentRuntimeOutcome:
        del context
        self.order.append("runtime")
        self.seen_prepared = prepared
        run_result = self.result if isinstance(self.result, AgentRunResult) else _run_result(self.result)
        return RawAgentRuntimeOutcome(run_result=run_result, raw_workspace=self.workspace)


class RecordingFinalize:
    def __init__(self, output: object, order: list[str]) -> None:
        self.output = output
        self.order = order
        self.seen: RawFinalizeBundle[CaseDesignInput, CaseDesignPrepared, CaseDesignAgentResult] | None = None

    async def execute(
        self,
        bundle: RawFinalizeBundle[CaseDesignInput, CaseDesignPrepared, CaseDesignAgentResult],
        context: AttemptExecutionContext,
    ) -> object:
        del context
        self.order.append("finalize")
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


def _contract() -> AgentExecutionContract[CaseDesignInput, CaseDesignAgentResult, CaseDesignOutput]:
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
        resources=ResourceClaims(),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=60),
        validators=(),
    )


def _workspace(tmp_path: Path) -> ReadOnlyRawWorkspace:
    root = tmp_path / "raw-workspace"
    root.mkdir()
    (root / "qa").mkdir()
    (root / "qa" / "proposal.md").write_text("design", encoding="utf-8")
    return ReadOnlyRawWorkspace(root)


def _executor(
    *,
    prepare: RecordingPrepare,
    runtime: RecordingRuntime,
    finalize: RecordingFinalize,
) -> ResolvedRawAgentExecutor[CaseDesignInput, CaseDesignPrepared, CaseDesignAgentResult, CaseDesignOutput]:
    return ResolvedRawAgentExecutor(
        _contract(),
        prepare=prepare,
        runtime=runtime,
        finalize=finalize,
    )


def test_raw_executor_runs_prepare_runtime_result_finalize_output_in_order(tmp_path: Path) -> None:
    validated_input = CaseDesignInput(change_id="CH-1", path="primary")
    prepared_value = CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases")
    expected_agent_result = CaseDesignAgentResult(output_files=("qa/changes/CH-1/proposal.md",))
    expected_output = CaseDesignOutput(
        status="committed",
        output_files=expected_agent_result.output_files,
        path="primary",
    )
    order: list[str] = []
    prepare = RecordingPrepare(prepared_value, order)
    runtime = RecordingRuntime(expected_agent_result.model_dump(), _workspace(tmp_path), order)
    finalize = RecordingFinalize(expected_output, order)

    output = asyncio.run(
        _executor(prepare=prepare, runtime=runtime, finalize=finalize).execute(validated_input, _context())
    )

    assert order == ["prepare", "runtime", "finalize"]
    assert prepare.seen_input == validated_input
    assert runtime.seen_prepared == prepared_value
    assert runtime.seen_schema is None
    assert finalize.seen is not None
    assert finalize.seen.validated_input == validated_input
    assert finalize.seen.prepared == prepared_value
    assert finalize.seen.agent_result == expected_agent_result
    assert finalize.seen.run_evidence.result_digest == canonical_digest(expected_agent_result.model_dump())
    assert finalize.seen.raw_workspace.read_text("qa/proposal.md") == "design"
    assert output == expected_output


def test_invalid_raw_result_fails_before_finalize(tmp_path: Path) -> None:
    validated_input = CaseDesignInput(change_id="CH-1")
    order: list[str] = []
    prepare = RecordingPrepare(
        CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases"), order
    )
    runtime = RecordingRuntime({"not": "an-agent-result"}, _workspace(tmp_path), order)
    finalize = RecordingFinalize(
        CaseDesignOutput(status="committed", output_files=("qa/changes/CH-1/proposal.md",), path="primary"),
        order,
    )

    with pytest.raises((ValidationError, ValueError)):
        asyncio.run(
            _executor(prepare=prepare, runtime=runtime, finalize=finalize).execute(
                validated_input, _context()
            )
        )

    assert order == ["prepare", "runtime"]
    assert finalize.seen is None


def test_invalid_output_fails_after_finalize(tmp_path: Path) -> None:
    validated_input = CaseDesignInput(change_id="CH-1")
    prepared_value = CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases")
    expected_agent_result = CaseDesignAgentResult(output_files=("qa/changes/CH-1/proposal.md",))
    order: list[str] = []
    prepare = RecordingPrepare(prepared_value, order)
    runtime = RecordingRuntime(expected_agent_result.model_dump(), _workspace(tmp_path), order)
    finalize = RecordingFinalize({"status": "missing-required-fields"}, order)

    with pytest.raises(ValidationError):
        asyncio.run(
            _executor(prepare=prepare, runtime=runtime, finalize=finalize).execute(
                validated_input, _context()
            )
        )

    assert order == ["prepare", "runtime", "finalize"]
    assert finalize.seen is not None


def test_raw_workspace_is_read_only(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    assert workspace.read_bytes("qa/proposal.md") == b"design"
    with pytest.raises(AttributeError):
        workspace.write_text("qa/proposal.md", "mutated")  # type: ignore[attr-defined]
    assert not hasattr(workspace, "write_bytes")
    assert workspace.read_text("qa/proposal.md") == "design"


def test_raw_executor_resolves_single_graph_facing_task_contract(tmp_path: Path) -> None:
    order: list[str] = []
    prepare = RecordingPrepare(
        CaseDesignPrepared(change_id="CH-1", path="primary", prompt="design cases"), order
    )
    runtime = RecordingRuntime(
        CaseDesignAgentResult(output_files=("qa/changes/CH-1/proposal.md",)).model_dump(),
        _workspace(tmp_path),
        order,
    )
    finalize = RecordingFinalize(
        CaseDesignOutput(status="committed", output_files=("qa/changes/CH-1/proposal.md",), path="primary"),
        order,
    )
    executor = _executor(prepare=prepare, runtime=runtime, finalize=finalize)

    resolved = executor.resolve()
    again = resolve_contract(executor.to_task_contract(), executor=executor)

    assert resolved.contract_digest == again.contract_digest
    assert "executor" not in resolved.canonical_projection()
    assert resolved.executor is executor
    assert resolved.contract.input_model is CaseDesignInput
    assert resolved.contract.output_model is CaseDesignOutput
    assert resolved.contract.handler_id == "assurance.intake.case-design.prepare"
    assert not hasattr(executor, "negotiate_provider_schema")


def test_case_design_raw_attempt_preserves_both_legacy_prepare_consumers(tmp_path: Path) -> None:
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
    order: list[str] = []
    prepare = RecordingPrepare(prepared_value, order)
    runtime = RecordingRuntime(expected_agent_result.model_dump(), _workspace(tmp_path), order)
    finalize = RecordingFinalize(expected_output, order)

    output = asyncio.run(
        _executor(prepare=prepare, runtime=runtime, finalize=finalize).execute(validated_input, _context())
    )

    assert runtime.seen_prepared == prepared_value
    assert finalize.seen is not None
    assert finalize.seen.prepared == prepared_value
    assert finalize.seen.agent_result == expected_agent_result
    assert output == expected_output


def test_case_design_repair_raw_attempt_preserves_both_legacy_prepare_consumers(tmp_path: Path) -> None:
    validated_input = CaseDesignInput(change_id="CH-1", path="repair")
    prepared_value = CaseDesignPrepared(change_id="CH-1", path="repair", prompt="repair case-design")
    expected_agent_result = CaseDesignAgentResult(output_files=("qa/changes/CH-1/cases/api/case.yaml",))
    expected_output = CaseDesignOutput(
        status="repaired",
        output_files=expected_agent_result.output_files,
        path="repair",
    )
    order: list[str] = []
    prepare = RecordingPrepare(prepared_value, order)
    runtime = RecordingRuntime(expected_agent_result.model_dump(), _workspace(tmp_path), order)
    finalize = RecordingFinalize(expected_output, order)

    output = asyncio.run(
        _executor(prepare=prepare, runtime=runtime, finalize=finalize).execute(validated_input, _context())
    )

    assert runtime.seen_prepared == prepared_value
    assert finalize.seen is not None
    assert finalize.seen.prepared == prepared_value
    assert finalize.seen.agent_result == expected_agent_result
    assert finalize.seen.validated_input.path == "repair"
    assert output == expected_output
