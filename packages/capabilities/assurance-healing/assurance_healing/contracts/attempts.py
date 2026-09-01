from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, cast

from agent_runtime_contracts import AgentExecutionContract
from graph_engine.attempts import AttemptRetryPolicy, AttemptTimeoutPolicy, TaskAttemptContract
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import AttemptContractRef, ResourceClaimTemplate

from assurance_healing.contracts.agent import CoverageRepairInputV1, FixProposalInputV1, FixProposalResultV1
from assurance_healing.contracts.coverage_repair import CoverageRepairStatus

_DOC_AUTHOR = "assurance-v1-doc-author"
_TEST_AUTHOR = "assurance-v1-test-author"
_RETRY = AttemptRetryPolicy(max_attempts=1)
_TIMEOUT = AttemptTimeoutPolicy(seconds=60)


def _paths(*suffixes: str) -> tuple[str, ...]:
    return tuple(sorted(f"qa/changes/{{change_id}}/{suffix}" for suffix in suffixes))


def _job(
    base: str,
    skill_id: str,
    agent_profile: str,
    input_model: type[Any],
    result_model: type[Any],
    outputs: tuple[str, ...],
) -> AgentExecutionContract[Any, Any, Any]:
    return AgentExecutionContract(
        contract_id=f"assurance.healing.agent.{base}.v1",
        owner_id="assurance.healing",
        prepare_handler_id=f"assurance.healing.{base}.prepare",
        finalize_handler_id=f"assurance.healing.{base}.finalize",
        skill_id=skill_id,
        agent_profile=agent_profile,
        input_model=input_model,
        agent_result_model=result_model,
        output_model=result_model,
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


_JOBS: tuple[tuple[str, str, str, type[Any], type[Any], tuple[str, ...]], ...] = (
    (
        "coverage-repair",
        "aa-coverage-repair",
        _TEST_AUTHOR,
        CoverageRepairInputV1,
        CoverageRepairStatus,
        ("healing/coverage-repair.json",),
    ),
    (
        "fix-proposal",
        "aa-fix-proposal",
        _DOC_AUTHOR,
        FixProposalInputV1,
        FixProposalResultV1,
        ("healing/fix-proposal.json",),
    ),
)

AGENT_JOB_CONTRACTS: Mapping[str, AgentExecutionContract[Any, Any, Any]] = MappingProxyType(
    {
        base: _job(base, skill_id, profile, input_model, result_model, outputs)
        for base, skill_id, profile, input_model, result_model, outputs in _JOBS
    }
)
OUTPUT_ROUTE_TEMPLATES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {base: _paths(*outputs) for base, _skill, _profile, _input, _result, outputs in _JOBS}
)
TASK_ATTEMPT_CONTRACTS: Mapping[str, TaskAttemptContract[Any, Any]] = MappingProxyType({})
HEALING_GRAPH_CONTRACT_IDS: tuple[str, ...] = (
    "assurance.healing.agent.coverage-repair.v1",
    "assurance.healing.agent.fix-proposal.v1",
)
HEALING_GRAPH_EXPORTS: tuple[str, ...] = (
    "repair_failure",
    "repair_coverage",
)
HEALING_EFFECT_IDS: tuple[str, ...] = (
    "assurance.healing.effect.allocation.v2",
    "assurance.healing.effect.heal-apply.v2",
    "assurance.healing.effect.proposal-approved.v1",
)


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
    "HEALING_EFFECT_IDS",
    "HEALING_GRAPH_CONTRACT_IDS",
    "HEALING_GRAPH_EXPORTS",
    "OUTPUT_ROUTE_TEMPLATES",
    "TASK_ATTEMPT_CONTRACTS",
    "attempt_contract_refs",
]
