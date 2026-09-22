from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, cast

from agent_runtime_contracts import AgentExecutionContract, AgentPhaseWriteClaims
from agent_runtime_contracts.qa_paths import qa_join, qa_route
from graph_engine.attempts import AttemptRetryPolicy, AttemptTimeoutPolicy, TaskAttemptContract
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import AttemptContractRef, ResourceClaims, ResourceClaimTemplate

from assurance_generation.contracts.agent import CodegenInputV1
from assurance_generation.contracts.codegen import (
    CodegenAuthoringV1,
    CodegenResultV1,
)
from assurance_generation.contracts.reviews import PlanReview, PlanReviewAuthoring
from assurance_generation.contracts.init_runtime import InitTestRuntimeInputV1, InitTestRuntimeResultV1
from assurance_generation.contracts.workflow import (
    CompleteGenerationInputV1,
    GenerationCycleResultV1,
    ResolveGenerationInputV1,
)
from assurance_intake.contracts.workflow import ReviewedCaseV1

_REVIEWER = "assurance-v1-reviewer"
_TEST_AUTHOR = "assurance-v1-test-author"
_AGENT_RETRY = AttemptRetryPolicy(max_attempts=10, interval_seconds=10)
_TASK_RETRY = AttemptRetryPolicy(max_attempts=1)
_TIMEOUT = AttemptTimeoutPolicy(seconds=60)


def _paths(*suffixes: str) -> tuple[str, ...]:
    return qa_route(*suffixes)


_GENERATED_TESTS_ROOT = "qa/tests"


def _review_outputs(family: str) -> tuple[str, ...]:
    return (f"review/{family}-codegen-review.json", f"review/{family}-codegen-review-summary.md")


def _codegen_outputs(family: str) -> tuple[str, ...]:
    return (
        f"codegen/{family}-codegen-summary.md",
        f"codegen/{family}-generated-files.json",
    )


def _job(
    base: str,
    skill_id: str,
    agent_profile: str,
    input_model: type[Any],
    agent_result_model: type[Any],
    output_model: type[Any],
    outputs: tuple[str, ...],
) -> AgentExecutionContract[Any, Any, Any]:
    family, _, stage = base.partition(".")
    runtime_writes = _paths(*outputs)
    if stage == "codegen":
        runtime_writes = tuple(sorted((*runtime_writes, _GENERATED_TESTS_ROOT)))
    finalize_writes = _paths(f"codegen/{family}/reviews") if stage == "codegen-review" else ()
    writes = tuple(sorted((*runtime_writes, *finalize_writes)))
    return AgentExecutionContract(
        contract_id=f"assurance.generation.agent.{base}.v1",
        owner_id="assurance.generation",
        prepare_handler_id=f"assurance.generation.{base}.prepare",
        finalize_handler_id=f"assurance.generation.{base}.finalize",
        skill_id=skill_id,
        agent_profile=agent_profile,
        input_model=input_model,
        agent_result_model=agent_result_model,
        output_model=output_model,
        resources=ResourceClaims(
            reads=("qa",),
            writes=writes,
        ),
        retry=_AGENT_RETRY,
        timeout=_TIMEOUT,
        validators=(),
        phase_write_claims=AgentPhaseWriteClaims(
            prepare=(_GENERATED_TESTS_ROOT,) if stage == "codegen" else (),
            runtime=runtime_writes,
            finalize=finalize_writes,
        ),
    )


_JOBS: tuple[
    tuple[str, str, str, type[Any], type[Any], type[Any], tuple[str, ...]],
    ...,
] = (
    (
        "api.codegen",
        "aa-api-codegen",
        _TEST_AUTHOR,
        CodegenInputV1,
        CodegenAuthoringV1,
        CodegenResultV1,
        _codegen_outputs("api"),
    ),
    (
        "api.codegen-review",
        "aa-api-codegen-reviewer",
        _REVIEWER,
        CodegenInputV1,
        PlanReviewAuthoring,
        PlanReview,
        _review_outputs("api"),
    ),
    (
        "e2e.codegen",
        "aa-e2e-codegen",
        _TEST_AUTHOR,
        CodegenInputV1,
        CodegenAuthoringV1,
        CodegenResultV1,
        _codegen_outputs("e2e"),
    ),
    (
        "e2e.codegen-review",
        "aa-e2e-codegen-reviewer",
        _REVIEWER,
        CodegenInputV1,
        PlanReviewAuthoring,
        PlanReview,
        _review_outputs("e2e"),
    ),
    (
        "fuzz.codegen",
        "aa-fuzz-codegen",
        _TEST_AUTHOR,
        CodegenInputV1,
        CodegenAuthoringV1,
        CodegenResultV1,
        _codegen_outputs("fuzz"),
    ),
    (
        "fuzz.codegen-review",
        "aa-fuzz-codegen-reviewer",
        _REVIEWER,
        CodegenInputV1,
        PlanReviewAuthoring,
        PlanReview,
        _review_outputs("fuzz"),
    ),
    (
        "performance.codegen",
        "aa-performance-codegen",
        _TEST_AUTHOR,
        CodegenInputV1,
        CodegenAuthoringV1,
        CodegenResultV1,
        _codegen_outputs("performance"),
    ),
    (
        "performance.codegen-review",
        "aa-performance-codegen-reviewer",
        _REVIEWER,
        CodegenInputV1,
        PlanReviewAuthoring,
        PlanReview,
        _review_outputs("performance"),
    ),
)

AGENT_JOB_CONTRACTS: Mapping[str, AgentExecutionContract[Any, Any, Any]] = MappingProxyType(
    {
        base: _job(base, skill_id, profile, input_model, agent_result_model, output_model, outputs)
        for base, skill_id, profile, input_model, agent_result_model, output_model, outputs in _JOBS
    }
)
OUTPUT_ROUTE_TEMPLATES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        base: tuple(
            sorted(
                (
                    *_paths(*outputs),
                    *(
                        (
                            qa_join(
                                f"codegen/{base.partition('.')[0]}/reviews/"
                                "epochs/{coverage_epoch}/rounds/{review_round}.json"
                            ),
                            qa_join(
                                f"codegen/{base.partition('.')[0]}/reviews/"
                                "epochs/{coverage_epoch}/finding-scope.json"
                            ),
                        )
                        if base.partition(".")[2] == "codegen-review"
                        else ()
                    ),
                )
            )
        )
        for base, _skill, _profile, _input, _agent_result, _result, outputs in _JOBS
    }
)
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
GENERATION_GRAPH_CONTRACT_IDS: tuple[str, ...] = tuple(
    contract.contract_id for contract in AGENT_JOB_CONTRACTS.values()
) + (_RESOLVE_INPUTS.contract_id, _PUBLISH_CYCLE.contract_id, _INIT_TEST_RUNTIME.contract_id)


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
    "GENERATION_GRAPH_CONTRACT_IDS",
    "OUTPUT_ROUTE_TEMPLATES",
    "TASK_ATTEMPT_CONTRACTS",
    "attempt_contract_refs",
]
