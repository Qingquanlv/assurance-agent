from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel

from agent_runtime_contracts import AgentRunResult, RawAgentRuntimeOutcome, ReadOnlyRawWorkspace
from assurance_generation.contracts.plans import PlanResultV1
from graph_engine.attempts import (
    AttemptExecutionContext,
    AttemptRetryPolicy,
    AttemptTimeoutPolicy,
    AuthorizedAttemptScope,
    BusinessActivation,
    CommittedTaskResult,
    ExecutedAttemptResult,
    TaskAttemptContract,
    derive_attempt_key,
    resolve_contract,
)
from graph_engine.attempts.kernel import AssuranceAttemptKernel
from graph_engine.attempts.resource_arbiter import ResourceArbiter
from graph_engine.attempts.workspace import TaskWorkspaceProvider, TaskWorkspaceStore
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.persistence.attempt_journal import MemoryAttemptJournal
from graph_engine.persistence.resource_authorization import MemoryResourceAuthorizationStore
from graph_engine.plugin_api import ResourceClaims


_LEAF = "entities.item.create"


class _GenerationInput(BaseModel):
    change_id: str
    capability_leafs: tuple[str, ...]


def _plan_payload() -> dict[str, object]:
    return {
        "schema_version": "1",
        "family": "api",
        "change_id": "CH-CONTEXT-001",
        "case_ids": ["TC-001"],
        "required_capabilities": [_LEAF],
        "coverage": [
            {
                "case_id": "TC-001",
                "operation": "create",
                "risk": "high",
                "required_capabilities": [_LEAF],
            }
        ],
        "output_files": ["out.txt"],
    }


class _GenerationExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self,
        validated_input: _GenerationInput,
        scope: AuthorizedAttemptScope,
    ) -> ExecutedAttemptResult[PlanResultV1]:
        self.calls += 1
        (scope.workspace.write_root / "out.txt").write_text("generated\n", encoding="utf-8")
        output = PlanResultV1.model_validate(
            _plan_payload(),
            context={"capability_leafs": frozenset(validated_input.capability_leafs)},
        )
        return ExecutedAttemptResult(output=output)


class _HostBindableGenerationExecutor(_GenerationExecutor):
    def with_host(
        self,
        host: object,
        *,
        graph_revision: str,
        product_lock_digest: str,
    ) -> _HostBindableGenerationExecutor:
        del host, graph_revision, product_lock_digest
        return self


class _Prepared(BaseModel):
    prompt: str = "generate"


class _AgentResult(BaseModel):
    status: str


class _PreparePhase:
    async def execute(
        self,
        validated_input: _GenerationInput,
        scope: AuthorizedAttemptScope,
    ) -> _Prepared:
        del validated_input, scope
        return _Prepared()


class _RuntimePhase:
    handler_id = "runtime.fixture.execute"

    async def execute(
        self,
        prepared: _Prepared,
        scope: AuthorizedAttemptScope,
    ) -> RawAgentRuntimeOutcome:
        del prepared

        payload: JSONValue = {"status": "ok"}
        run_result = AgentRunResult(
            result_payload=payload,
            result_digest=canonical_digest(payload),
            evidence_digest="e" * 64,
            adapter_id="fixture",
            adapter_version="1",
        )
        return RawAgentRuntimeOutcome(
            run_result=run_result,
            raw_workspace=ReadOnlyRawWorkspace(scope.workspace.write_root),
        )


class _FinalizeInput(BaseModel):
    agent_result: dict[str, object]


class _FinalizeHandler:
    input_model = _FinalizeInput

    async def execute(self, request: object, context: object) -> object:
        del request, context
        from graph_engine.plugin_api import TaskOutcome

        return TaskOutcome.succeeded(cast(JSONValue, _plan_payload()))


class _Crash(RuntimeError):
    pass


@dataclass
class _Scenario:
    kernel: AssuranceAttemptKernel
    attempt_key: object
    resolved: object
    validated_input: _GenerationInput
    execution_context: AttemptExecutionContext
    executor: _GenerationExecutor
    store: TaskWorkspaceStore


def _scenario(tmp_path: Path) -> _Scenario:
    project = tmp_path / "project"
    project.mkdir()
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    executor = _GenerationExecutor()
    resolved = resolve_contract(
        _contract(),
        executor=executor,
        validation_context={"capability_leafs": frozenset({_LEAF})},
    )
    validated_input = _GenerationInput(
        change_id="CH-CONTEXT-001",
        capability_leafs=(_LEAF,),
    )
    attempt_key = derive_attempt_key(
        invocation_id="inv-context",
        graph_revision=_revision(),
        public_entrypoint="generate",
        semantic_node_id="generation.api.codegen",
        business_activation=BusinessActivation.one_shot(),
        contract_id=resolved.contract.contract_id,
        validated_input=validated_input,
    )
    execution_context = AttemptExecutionContext(
        invocation_id="inv-context",
        public_entrypoint="generate",
        semantic_node_id="generation.api.codegen",
        attempt_key=attempt_key,
        fencing_token=1,
    )
    return _Scenario(
        kernel=AssuranceAttemptKernel(
            journal=MemoryAttemptJournal(),
            arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
            workspace=TaskWorkspaceProvider(store),
            graph_revision=_revision(),
        ),
        attempt_key=attempt_key,
        resolved=resolved,
        validated_input=validated_input,
        execution_context=execution_context,
        executor=executor,
        store=store,
    )


def _contract() -> TaskAttemptContract[_GenerationInput, PlanResultV1]:
    return TaskAttemptContract(
        contract_id="assurance.generation.context-test.v1",
        owner_id="assurance.generation",
        handler_id="assurance.generation.context-test",
        input_model=_GenerationInput,
        output_model=PlanResultV1,
        resources=ResourceClaims(writes=("out.txt",)),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=60),
        validators=(),
    )


def _revision() -> str:
    return canonical_digest({"revision": "attempt-model-validation-context"})


def test_generation_output_commit_uses_locked_capability_context(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = TaskWorkspaceStore(project, tmp_path / "attempts", tmp_path / "receipts")
    executor = _GenerationExecutor()
    validation_context = {"capability_leafs": frozenset({_LEAF})}
    resolved = resolve_contract(
        _contract(),
        executor=executor,
        validation_context=validation_context,
    )
    validated_input = _GenerationInput(
        change_id="CH-CONTEXT-001",
        capability_leafs=(_LEAF,),
    )
    attempt_key = derive_attempt_key(
        invocation_id="inv-context",
        graph_revision=_revision(),
        public_entrypoint="generate",
        semantic_node_id="generation.api.codegen",
        business_activation=BusinessActivation.one_shot(),
        contract_id=resolved.contract.contract_id,
        validated_input=validated_input,
    )
    execution_context = AttemptExecutionContext(
        invocation_id="inv-context",
        public_entrypoint="generate",
        semantic_node_id="generation.api.codegen",
        attempt_key=attempt_key,
        fencing_token=1,
    )
    kernel = AssuranceAttemptKernel(
        journal=MemoryAttemptJournal(),
        arbiter=ResourceArbiter(MemoryResourceAuthorizationStore()),
        workspace=TaskWorkspaceProvider(store),
        graph_revision=_revision(),
    )
    validation_context["capability_leafs"] = frozenset({"entities.forged"})

    try:
        result = asyncio.run(
            kernel.execute_or_recover(
                attempt_key,
                resolved,
                validated_input,
                execution_context,
            )
        )
    finally:
        store.close()

    assert isinstance(result, CommittedTaskResult)
    assert result.output.required_capabilities == (_LEAF,)
    assert resolved.validation_context == {"capability_leafs": frozenset({_LEAF})}
    with pytest.raises(TypeError):
        resolved.validation_context["capability_leafs"] = frozenset()  # type: ignore[index]
    assert executor.calls == 1
    assert (project / "out.txt").read_text(encoding="utf-8") == "generated\n"


def test_production_host_binding_preserves_locked_capability_context() -> None:
    from assurance_product.runtime_ports import _bind_executor_host

    expected = {"capability_leafs": frozenset({_LEAF})}
    resolved = resolve_contract(
        _contract(),
        executor=_HostBindableGenerationExecutor(),
        validation_context=expected,
    )

    bound = _bind_executor_host(
        resolved,
        host=object(),
        graph_revision=_revision(),
        product_lock_digest="f" * 64,
    )

    assert bound.validation_context == expected


def test_generation_output_observed_replay_uses_locked_capability_context(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)

    def crash_after_observation(name: str) -> None:
        if name == "after_observed_result":
            raise _Crash(name)

    try:
        with pytest.raises(_Crash, match="after_observed_result"):
            asyncio.run(
                scenario.kernel.execute_or_recover(
                    scenario.attempt_key,  # type: ignore[arg-type]
                    scenario.resolved,  # type: ignore[arg-type]
                    scenario.validated_input,
                    scenario.execution_context,
                    transaction_cut=crash_after_observation,
                )
            )
        replay = asyncio.run(
            scenario.kernel.execute_or_recover(
                scenario.attempt_key,  # type: ignore[arg-type]
                scenario.resolved,  # type: ignore[arg-type]
                scenario.validated_input,
                scenario.execution_context,
                transaction_cut=None,
            )
        )
    finally:
        scenario.store.close()

    assert isinstance(replay, CommittedTaskResult)
    assert replay.output.required_capabilities == (_LEAF,)
    assert scenario.executor.calls == 1


def test_generation_output_terminal_replay_uses_locked_capability_context(tmp_path: Path) -> None:
    scenario = _scenario(tmp_path)

    try:
        committed = asyncio.run(
            scenario.kernel.execute_or_recover(
                scenario.attempt_key,  # type: ignore[arg-type]
                scenario.resolved,  # type: ignore[arg-type]
                scenario.validated_input,
                scenario.execution_context,
            )
        )
        replay = asyncio.run(
            scenario.kernel.execute_or_recover(
                scenario.attempt_key,  # type: ignore[arg-type]
                scenario.resolved,  # type: ignore[arg-type]
                scenario.validated_input,
                scenario.execution_context,
            )
        )
    finally:
        scenario.store.close()

    assert isinstance(committed, CommittedTaskResult)
    assert isinstance(replay, CommittedTaskResult)
    assert replay.output.required_capabilities == (_LEAF,)
    assert replay.receipt == committed.receipt
    assert scenario.executor.calls == 1


def test_product_raw_agent_validates_final_generation_output_once_with_the_locked_context(
    tmp_path: Path,
) -> None:
    from agent_runtime_contracts import (
        AgentExecutionContract,
        AgentPhaseWriteClaims,
        ResolvedRawAgentExecutor,
    )
    from assurance_product.runtime_bindings import InstalledFinalizePhase

    contract = AgentExecutionContract(
        contract_id="assurance.generation.agent.context-test.v1",
        owner_id="assurance.generation",
        prepare_handler_id="assurance.generation.context-test.prepare",
        finalize_handler_id="assurance.generation.context-test.finalize",
        skill_id="context-test",
        agent_profile="context-test",
        input_model=_GenerationInput,
        agent_result_model=_AgentResult,
        output_model=PlanResultV1,
        resources=ResourceClaims(),
        retry=AttemptRetryPolicy(max_attempts=1),
        timeout=AttemptTimeoutPolicy(seconds=60),
        validators=(),
        phase_write_claims=AgentPhaseWriteClaims(prepare=(), runtime=(), finalize=()),
    )
    validation_context = {"capability_leafs": frozenset({_LEAF})}
    executor = ResolvedRawAgentExecutor(
        contract,
        prepare=_PreparePhase(),
        runtime=_RuntimePhase(),
        finalize=InstalledFinalizePhase(
            contract.finalize_handler_id,
            _FinalizeHandler(),
            contract.output_model,
        ),
        result_context=validation_context,
    )
    project = tmp_path / "raw-agent-project"
    project.mkdir()
    store = TaskWorkspaceStore(
        project,
        tmp_path / "raw-agent-attempts",
        tmp_path / "raw-agent-receipts",
    )
    scope = AuthorizedAttemptScope(
        execution=AttemptExecutionContext(
            invocation_id="inv-raw-context",
            public_entrypoint="generate",
            semantic_node_id="generation.api.codegen",
            attempt_key=derive_attempt_key(
                invocation_id="inv-raw-context",
                graph_revision=_revision(),
                public_entrypoint="generate",
                semantic_node_id="generation.api.codegen",
                business_activation=BusinessActivation.one_shot(),
                contract_id=contract.contract_id,
                validated_input=_GenerationInput(
                    change_id="CH-CONTEXT-001",
                    capability_leafs=(_LEAF,),
                ),
            ),
            fencing_token=1,
        ),
        workspace=store.begin(task_id="context-test", attempt=1, output_paths=()),
    )

    try:
        result = asyncio.run(
            executor.execute(
                _GenerationInput(
                    change_id="CH-CONTEXT-001",
                    capability_leafs=(_LEAF,),
                ),
                scope,
            )
        )
    finally:
        store.close()

    assert isinstance(result, ExecutedAttemptResult)
    assert isinstance(result.output, PlanResultV1)
    assert result.output.required_capabilities == (_LEAF,)
