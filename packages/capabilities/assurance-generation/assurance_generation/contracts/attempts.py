from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, cast

from agent_runtime_contracts.qa_paths import qa_join
from graph_engine.attempts import AttemptRetryPolicy, AttemptTimeoutPolicy, TaskAttemptContract
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import AttemptContractRef, ResourceClaims, ResourceClaimTemplate

from assurance_generation.contracts.init_runtime import InitTestRuntimeInputV1, InitTestRuntimeResultV1
from assurance_generation.contracts.workflow import (
    CompleteGenerationInputV1,
    GenerationCycleResultV1,
    ResolveGenerationInputV1,
)
from assurance_intake.contracts.workflow import ReviewedCaseV1

_TASK_RETRY = AttemptRetryPolicy(max_attempts=1)
_TIMEOUT = AttemptTimeoutPolicy(seconds=60)


def _agent_catalog() -> tuple[Mapping[str, Any], Mapping[str, tuple[str, ...]]]:
    from assurance_generation.ops import router

    return router.agent_contracts(), router.output_routes()


AGENT_JOB_CONTRACTS, OUTPUT_ROUTE_TEMPLATES = _agent_catalog()
_RESOLVE_INPUTS = TaskAttemptContract(
    contract_id="assurance.generation.resolve-inputs",
    owner_id="assurance.generation",
    handler_id="assurance.generation.resolve-inputs.execute",
    input_model=ResolveGenerationInputV1,
    output_model=ReviewedCaseV1,
    resources=ResourceClaims(reads=("qa",)),
    retry=_TASK_RETRY,
    timeout=_TIMEOUT,
    validators=(),
)
_PUBLISH_CYCLE = TaskAttemptContract(
    contract_id="assurance.generation.publish-cycle",
    owner_id="assurance.generation",
    handler_id="assurance.generation.publish-cycle.execute",
    input_model=CompleteGenerationInputV1,
    output_model=GenerationCycleResultV1,
    resources=ResourceClaimTemplate(
        parameters={"coverage_epoch": "/coverage_epoch_token"},
        reads=("qa",),
        writes=(
            qa_join("generation/epochs/{coverage_epoch}/mapping.json"),
            qa_join("generation/epochs/{coverage_epoch}/obligation-methods.json"),
        ),
    ),
    retry=_TASK_RETRY,
    timeout=_TIMEOUT,
    validators=(),
)
_INIT_TEST_RUNTIME = TaskAttemptContract(
    contract_id="assurance.generation.init-test-runtime",
    owner_id="assurance.generation",
    handler_id="assurance.generation.init-test-runtime.execute",
    input_model=InitTestRuntimeInputV1,
    output_model=InitTestRuntimeResultV1,
    resources=ResourceClaims(
        reads=("qa",),
        writes=("qa/tests", qa_join("init/test-runtime.json")),
    ),
    retry=_TASK_RETRY,
    timeout=_TIMEOUT,
    validators=(),
)
TASK_ATTEMPT_CONTRACTS: Mapping[str, TaskAttemptContract[Any, Any]] = MappingProxyType(
    {
        "resolve-inputs": _RESOLVE_INPUTS,
        "publish-cycle": _PUBLISH_CYCLE,
        "init-test-runtime": _INIT_TEST_RUNTIME,
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
