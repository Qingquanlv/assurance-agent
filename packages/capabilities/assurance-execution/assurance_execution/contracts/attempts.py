from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, cast

from agent_runtime_contracts import AgentExecutionContract, AgentPhaseWriteClaims
from graph_engine.attempts import AttemptRetryPolicy, AttemptTimeoutPolicy, TaskAttemptContract
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import AttemptContractRef, ResourceClaimTemplate

from assurance_execution.contracts.agent import ExecutionPrepareInputV1
from assurance_execution.contracts.evidence import ExecutionAgentResultV1, ExecutionEvidenceV1
from assurance_execution.contracts.verification import ExecutionDispatchResultV1

_EXECUTOR = "assurance-v1-executor"
_AGENT_RETRY = AttemptRetryPolicy(max_attempts=10, interval_seconds=10)
_TIMEOUT = AttemptTimeoutPolicy(seconds=60)


def _paths(*suffixes: str) -> tuple[str, ...]:
    return tuple(sorted(f"qa/changes/{{change_id}}/{suffix}" for suffix in suffixes))


def _job(
    base: str,
    skill_id: str,
    input_model: type[Any],
    outputs: tuple[str, ...],
) -> AgentExecutionContract[Any, Any, Any]:
    writes = _paths(*outputs)
    view_root = "qa/changes/{change_id}/.staging/execution"
    attempt_writes = tuple(sorted((*writes, view_root)))
    return AgentExecutionContract(
        contract_id=f"assurance.execution.agent.{base}.v1",
        owner_id="assurance.execution",
        prepare_handler_id=f"assurance.execution.{base}.prepare",
        finalize_handler_id=f"assurance.execution.{base}.finalize",
        skill_id=skill_id,
        agent_profile=_EXECUTOR,
        input_model=input_model,
        agent_result_model=ExecutionAgentResultV1,
        output_model=ExecutionEvidenceV1,
        resources=ResourceClaimTemplate(
            parameters={"change_id": "/change_id"},
            reads=("qa",),
            writes=attempt_writes,
        ),
        retry=_AGENT_RETRY,
        timeout=_TIMEOUT,
        validators=(),
        phase_write_claims=AgentPhaseWriteClaims(
            prepare=(view_root,),
            runtime=(),
            finalize=writes,
        ),
    )


_JOBS: tuple[tuple[str, str, type[Any], tuple[str, ...]], ...] = (
    ("execute", "aa-execute", ExecutionPrepareInputV1, ("execution/execute-result.json",)),
    ("run", "aa-run", ExecutionPrepareInputV1, ("execution/run-result.json",)),
)

AGENT_JOB_CONTRACTS: Mapping[str, AgentExecutionContract[Any, Any, Any]] = MappingProxyType(
    {base: _job(base, skill_id, input_model, outputs) for base, skill_id, input_model, outputs in _JOBS}
)
OUTPUT_ROUTE_TEMPLATES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {base: _paths(*outputs) for base, _skill, _input, outputs in _JOBS}
)
TASK_ATTEMPT_CONTRACTS: Mapping[str, TaskAttemptContract[Any, Any]] = MappingProxyType(
    {
        f"assurance.execution.task.{base}.v1": TaskAttemptContract(
            contract_id=f"assurance.execution.task.{base}.v1",
            owner_id="assurance.execution",
            handler_id="assurance.execution.verified-attempt",
            input_model=ExecutionPrepareInputV1,
            output_model=ExecutionDispatchResultV1,
            resources=ResourceClaimTemplate(
                parameters={"change_id": "/change_id"},
                reads=("qa",),
                # The explicit result leaf preserves the legacy finalize phase claim.
                writes=_paths(".staging/execution", "execution", f"execution/{base}-result.json"),
            ),
            retry=AttemptRetryPolicy(max_attempts=1),
            # Covers all three legacy host phases, each bounded at 3600 seconds.
            timeout=AttemptTimeoutPolicy(seconds=10800),
            validators=(),
        )
        for base in ("execute", "run")
    }
)


def attempt_contract_refs() -> tuple[AttemptContractRef, ...]:
    return tuple(
        sorted(
            (
                AttemptContractRef(
                    contract_id=contract.contract_id,
                    digest=canonical_digest(cast(JSONValue, contract.canonical_projection())),
                )
                for contract in (*AGENT_JOB_CONTRACTS.values(), *TASK_ATTEMPT_CONTRACTS.values())
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
