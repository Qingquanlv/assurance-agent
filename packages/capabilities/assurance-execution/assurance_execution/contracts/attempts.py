from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, cast

from agent_runtime_contracts import AgentExecutionContract
from graph_engine.attempts import AttemptRetryPolicy, AttemptTimeoutPolicy, TaskAttemptContract
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import AttemptContractRef, ResourceClaimTemplate

from assurance_execution.contracts.agent import ExecuteInputV1, RunSkillInputV1
from assurance_execution.contracts.execution import ExecutionManifest

_EXECUTOR = "assurance-v1-executor"
_RETRY = AttemptRetryPolicy(max_attempts=1)
_TIMEOUT = AttemptTimeoutPolicy(seconds=60)


def _paths(*suffixes: str) -> tuple[str, ...]:
    return tuple(sorted(f"qa/changes/{{change_id}}/{suffix}" for suffix in suffixes))


def _job(
    base: str,
    skill_id: str,
    input_model: type[Any],
    outputs: tuple[str, ...],
) -> AgentExecutionContract[Any, Any, Any]:
    return AgentExecutionContract(
        contract_id=f"assurance.execution.agent.{base}.v1",
        owner_id="assurance.execution",
        prepare_handler_id=f"assurance.execution.{base}.prepare",
        finalize_handler_id=f"assurance.execution.{base}.finalize",
        skill_id=skill_id,
        agent_profile=_EXECUTOR,
        input_model=input_model,
        agent_result_model=ExecutionManifest,
        output_model=ExecutionManifest,
        requires_provider_schema=True,
        resources=ResourceClaimTemplate(
            parameters={"change_id": "/workspace/scope_id"},
            reads=("qa",),
            writes=_paths(*outputs),
        ),
        retry=_RETRY,
        timeout=_TIMEOUT,
        validators=(),
    )


_JOBS: tuple[tuple[str, str, type[Any], tuple[str, ...]], ...] = (
    ("execute", "aa-execute", ExecuteInputV1, ("execution/execute-result.json",)),
    ("run", "aa-run", RunSkillInputV1, ("execution/run-result.json",)),
)

AGENT_JOB_CONTRACTS: Mapping[str, AgentExecutionContract[Any, Any, Any]] = MappingProxyType(
    {base: _job(base, skill_id, input_model, outputs) for base, skill_id, input_model, outputs in _JOBS}
)
OUTPUT_ROUTE_TEMPLATES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {base: _paths(*outputs) for base, _skill, _input, outputs in _JOBS}
)
TASK_ATTEMPT_CONTRACTS: Mapping[str, TaskAttemptContract[Any, Any]] = MappingProxyType({})


def attempt_contract_refs() -> tuple[AttemptContractRef, ...]:
    return tuple(
        sorted(
            (
                AttemptContractRef(
                    contract_id=contract.contract_id,
                    digest=canonical_digest(cast(JSONValue, contract.canonical_projection())),
                )
                for contract in AGENT_JOB_CONTRACTS.values()
            ),
            key=lambda item: item.contract_id,
        )
    )


__all__ = [
    "AGENT_JOB_CONTRACTS",
    "OUTPUT_ROUTE_TEMPLATES",
    "TASK_ATTEMPT_CONTRACTS",
    "attempt_contract_refs",
]
