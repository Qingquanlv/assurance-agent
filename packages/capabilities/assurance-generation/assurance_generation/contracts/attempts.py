from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, cast

from agent_runtime_contracts import AgentExecutionContract, AgentPhaseWriteClaims
from graph_engine.attempts import AttemptRetryPolicy, AttemptTimeoutPolicy, TaskAttemptContract
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import AttemptContractRef, ResourceClaimTemplate

from assurance_generation.contracts.agent import CodegenFixInputV1, CodegenInputV1, PlanInputV1
from assurance_generation.contracts.codegen import CodegenResultV1
from assurance_generation.contracts.plans import PlanResultV1
from assurance_generation.contracts.reviews import PlanReview

_DOC_AUTHOR = "assurance-v1-doc-author"
_REVIEWER = "assurance-v1-reviewer"
_TEST_AUTHOR = "assurance-v1-test-author"
_RETRY = AttemptRetryPolicy(max_attempts=1)
_TIMEOUT = AttemptTimeoutPolicy(seconds=60)

_PLAN_FILES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "api": (
            "plans/api-plan.md",
            "plans/api-test-data-plan.md",
            "plans/api-codegen-plan.md",
            "plans/api-codegen-mapping.json",
            "plans/m3-review-summary.md",
        ),
        "e2e": (
            "plans/e2e-plan.md",
            "plans/e2e-test-data-plan.md",
            "plans/e2e-codegen-plan.md",
            "plans/e2e-codegen-mapping.json",
            "plans/m4-review-summary.md",
        ),
        "fuzz": (
            "plans/fuzz-plan.md",
            "plans/fuzz-codegen-plan.md",
            "plans/fuzz-codegen-mapping.json",
            "plans/fuzz-review-summary.md",
        ),
        "performance": (
            "plans/performance-plan.md",
            "plans/performance-codegen-plan.md",
            "plans/performance-codegen-mapping.json",
            "plans/performance-review-summary.md",
        ),
    }
)


def _paths(*suffixes: str) -> tuple[str, ...]:
    return tuple(sorted(f"qa/changes/{{change_id}}/{suffix}" for suffix in suffixes))


def _review_outputs(family: str) -> tuple[str, ...]:
    return (f"review/{family}-plan-review.json", f"review/{family}-plan-review-summary.md")


def _codegen_outputs(family: str, *, fix: bool = False) -> tuple[str, ...]:
    suffix = "-fix" if fix else ""
    return (
        f"codegen/{family}-codegen{suffix}-summary.md",
        f"codegen/{family}-generated-files.json",
    )


def _job(
    base: str,
    skill_id: str,
    agent_profile: str,
    input_model: type[Any],
    result_model: type[Any],
    outputs: tuple[str, ...],
) -> AgentExecutionContract[Any, Any, Any]:
    family, _, stage = base.partition(".")
    claim_outputs = outputs
    if stage in {"codegen", "codegen-fix"}:
        claim_outputs = (*outputs, f"generated/{family}/files")
    writes = _paths(*claim_outputs)
    return AgentExecutionContract(
        contract_id=f"assurance.generation.agent.{base}.v1",
        owner_id="assurance.generation",
        prepare_handler_id=f"assurance.generation.{base}.prepare",
        finalize_handler_id=f"assurance.generation.{base}.finalize",
        skill_id=skill_id,
        agent_profile=agent_profile,
        input_model=input_model,
        agent_result_model=result_model,
        output_model=result_model,
        resources=ResourceClaimTemplate(
            parameters={"change_id": "/workspace/scope_id"},
            reads=("qa",),
            writes=writes,
        ),
        retry=_RETRY,
        timeout=_TIMEOUT,
        validators=(),
        phase_write_claims=AgentPhaseWriteClaims(prepare=(), runtime=writes, finalize=()),
    )


_JOBS: tuple[tuple[str, str, str, type[Any], type[Any], tuple[str, ...]], ...] = (
    (
        "api.codegen-fix",
        "aa-api-codegen-fixer",
        _TEST_AUTHOR,
        CodegenFixInputV1,
        CodegenResultV1,
        _codegen_outputs("api", fix=True),
    ),
    ("api.codegen", "aa-api-codegen", _TEST_AUTHOR, CodegenInputV1, CodegenResultV1, _codegen_outputs("api")),
    ("api.plan-review", "aa-api-plan-reviewer", _REVIEWER, PlanInputV1, PlanReview, _review_outputs("api")),
    ("api.plan", "aa-api-plan", _DOC_AUTHOR, PlanInputV1, PlanResultV1, _PLAN_FILES["api"]),
    (
        "e2e.codegen-fix",
        "aa-e2e-codegen-fixer",
        _TEST_AUTHOR,
        CodegenFixInputV1,
        CodegenResultV1,
        _codegen_outputs("e2e", fix=True),
    ),
    ("e2e.codegen", "aa-e2e-codegen", _TEST_AUTHOR, CodegenInputV1, CodegenResultV1, _codegen_outputs("e2e")),
    ("e2e.plan-review", "aa-e2e-plan-reviewer", _REVIEWER, PlanInputV1, PlanReview, _review_outputs("e2e")),
    ("e2e.plan", "aa-e2e-plan", _DOC_AUTHOR, PlanInputV1, PlanResultV1, _PLAN_FILES["e2e"]),
    (
        "fuzz.codegen",
        "aa-fuzz-codegen",
        _TEST_AUTHOR,
        CodegenInputV1,
        CodegenResultV1,
        _codegen_outputs("fuzz"),
    ),
    (
        "fuzz.plan-review",
        "aa-fuzz-plan-reviewer",
        _REVIEWER,
        PlanInputV1,
        PlanReview,
        _review_outputs("fuzz"),
    ),
    ("fuzz.plan", "aa-fuzz-plan", _DOC_AUTHOR, PlanInputV1, PlanResultV1, _PLAN_FILES["fuzz"]),
    (
        "performance.codegen",
        "aa-performance-codegen",
        _TEST_AUTHOR,
        CodegenInputV1,
        CodegenResultV1,
        _codegen_outputs("performance"),
    ),
    (
        "performance.plan-review",
        "aa-performance-plan-reviewer",
        _REVIEWER,
        PlanInputV1,
        PlanReview,
        _review_outputs("performance"),
    ),
    (
        "performance.plan",
        "aa-performance-plan",
        _DOC_AUTHOR,
        PlanInputV1,
        PlanResultV1,
        _PLAN_FILES["performance"],
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
GENERATION_GRAPH_CONTRACT_IDS: tuple[str, ...] = tuple(
    contract.contract_id for contract in AGENT_JOB_CONTRACTS.values()
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
    "GENERATION_GRAPH_CONTRACT_IDS",
    "OUTPUT_ROUTE_TEMPLATES",
    "TASK_ATTEMPT_CONTRACTS",
    "attempt_contract_refs",
]
