from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, cast

from agent_runtime_contracts import AgentExecutionContract, AgentPhaseWriteClaims
from agent_runtime_contracts.qa_paths import qa_join, qa_route
from graph_engine.attempts import AttemptRetryPolicy, AttemptTimeoutPolicy, TaskAttemptContract
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import AttemptContractRef, ResourceClaims

from assurance_intake.contracts.agent import (
    ArtifactListResultV1,
    CaseDesignOutputV1,
    CaseDesignInputV1,
    CaseReviewInputV1,
    ExploreInputV1,
    FinalizedArtifactsV1,
    IntakeInputV1,
)
from assurance_intake.contracts.review import CaseReviewResultV1
from assurance_intake.contracts.plan import LoadPlanInputV1, ResolvePlanInputV1, ResolvePlanOutputV1

_DOC_AUTHOR = "assurance-v1-doc-author"
_EXPLORER = "assurance-v1-explorer"
_REVIEWER = "assurance-v1-reviewer"
_AGENT_RETRY = AttemptRetryPolicy(max_attempts=10, interval_seconds=10)
_TASK_RETRY = AttemptRetryPolicy(max_attempts=1)
_TIMEOUT = AttemptTimeoutPolicy(seconds=60)


def _paths(*suffixes: str) -> tuple[str, ...]:
    return qa_route(*suffixes)


_CASES_ROOT = "qa/cases"


def _job(
    base: str,
    skill_id: str,
    agent_profile: str,
    input_model: type[Any],
    result_model: type[Any],
    output_model: type[Any],
    outputs: tuple[str, ...],
    extra_claims: tuple[str, ...] = (),
) -> AgentExecutionContract[Any, Any, Any]:
    prepare_suffixes = {
        "explore": ("explore/context.json",),
        "intake": ("requirement.md", "intake/sources/run-spec.effective.yaml"),
    }.get(base, ())
    prepare_paths = _paths(*prepare_suffixes) if prepare_suffixes else ()
    finalize_suffixes = {
        "case-review": ("cases/reviewed-case.json", "cases/reviews"),
        "explore": ("explore/exploration.json",),
    }.get(base, ())
    finalize_paths = _paths(*finalize_suffixes) if finalize_suffixes else ()
    literal_claims = (_CASES_ROOT,) if base == "case-design" else ()
    writes = tuple(
        sorted(
            set(_paths(*outputs, *extra_claims))
            | set(literal_claims)
            | set(prepare_paths)
            | set(finalize_paths)
        )
    )
    return AgentExecutionContract(
        contract_id=f"assurance.intake.agent.{base}.v1",
        owner_id="assurance.intake",
        prepare_handler_id=f"assurance.intake.{base}.prepare",
        finalize_handler_id=f"assurance.intake.{base}.finalize",
        skill_id=skill_id,
        agent_profile=agent_profile,
        input_model=input_model,
        agent_result_model=result_model,
        output_model=output_model,
        resources=ResourceClaims(
            reads=("qa",),
            writes=writes,
        ),
        retry=_AGENT_RETRY,
        timeout=_TIMEOUT,
        validators=(),
        phase_write_claims=AgentPhaseWriteClaims(
            prepare=prepare_paths,
            runtime=tuple(
                path for path in writes if path not in set(prepare_paths) and path not in set(finalize_paths)
            ),
            finalize=finalize_paths,
        ),
    )


_JOBS: tuple[
    tuple[str, str, str, type[Any], type[Any], type[Any], tuple[str, ...], tuple[str, ...]],
    ...,
] = (
    (
        "case-design",
        "aa-case-design",
        _DOC_AUTHOR,
        CaseDesignInputV1,
        ArtifactListResultV1,
        CaseDesignOutputV1,
        (".qa.yaml", "proposal.md", "trace/minimum-coverage-matrix.json"),
        (),
    ),
    (
        "case-review",
        "aa-case-reviewer",
        _REVIEWER,
        CaseReviewInputV1,
        CaseReviewResultV1,
        CaseReviewResultV1,
        ("review/case-review.json", "review/case-review-summary.md"),
        ("cases/reviewed-case.json",),
    ),
    (
        "explore",
        "aa-explore",
        _EXPLORER,
        ExploreInputV1,
        ArtifactListResultV1,
        FinalizedArtifactsV1,
        ("explore/exploration-draft.json", "explore/impact-inventory.json"),
        (),
    ),
    (
        "intake",
        "aa-intake",
        _DOC_AUTHOR,
        IntakeInputV1,
        ArtifactListResultV1,
        FinalizedArtifactsV1,
        (".qa.yaml",),
        (),
    ),
)

AGENT_JOB_CONTRACTS: Mapping[str, AgentExecutionContract[Any, Any, Any]] = MappingProxyType(
    {
        base: _job(base, skill_id, profile, input_model, result_model, output_model, outputs, extra)
        for base, skill_id, profile, input_model, result_model, output_model, outputs, extra in _JOBS
    }
)
OUTPUT_ROUTE_TEMPLATES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        base: tuple(
            sorted(
                (
                    *_paths(*outputs),
                    *(
                        (qa_join("cases/reviews/epochs/{coverage_epoch}/rounds/{review_round}.json"),)
                        if base == "case-review"
                        else ()
                    ),
                )
            )
        )
        for base, _skill, _profile, _input, _result, _output, outputs, _extra in _JOBS
    }
)
_RESOLVE_PLAN = TaskAttemptContract(
    contract_id="assurance.intake.task.resolve-plan",
    owner_id="assurance.intake",
    handler_id="assurance.intake.resolve-plan",
    input_model=ResolvePlanInputV1,
    output_model=ResolvePlanOutputV1,
    resources=ResourceClaims(
        reads=(".aa", "qa"),
        writes=_paths("plan"),
    ),
    retry=_TASK_RETRY,
    timeout=_TIMEOUT,
    validators=(),
)
_LOAD_PLAN = TaskAttemptContract(
    contract_id="assurance.intake.task.load-plan",
    owner_id="assurance.intake",
    handler_id="assurance.intake.load-plan",
    input_model=LoadPlanInputV1,
    output_model=ResolvePlanOutputV1,
    resources=ResourceClaims(
        reads=(".aa", "qa"),
    ),
    retry=_TASK_RETRY,
    timeout=_TIMEOUT,
    validators=(),
)
TASK_ATTEMPT_CONTRACTS: Mapping[str, TaskAttemptContract[Any, Any]] = MappingProxyType(
    {"resolve-plan": _RESOLVE_PLAN, "load-plan": _LOAD_PLAN}
)
INTAKE_GRAPH_CONTRACT_IDS: tuple[str, ...] = (
    "assurance.intake.agent.intake.v1",
    "assurance.intake.agent.explore.v1",
    "assurance.intake.agent.case-design.v1",
    "assurance.intake.agent.case-review.v1",
)
INTAKE_GRAPH_SEMANTIC_OCCURRENCES: tuple[tuple[str, str], ...] = (
    ("intake.intake", "assurance.intake.agent.intake.v1"),
    ("intake.explore", "assurance.intake.agent.explore.v1"),
    ("intake.case-design", "assurance.intake.agent.case-design.v1"),
    ("intake.case-design-repair", "assurance.intake.agent.case-design.v1"),
    ("intake.case-review", "assurance.intake.agent.case-review.v1"),
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
    "INTAKE_GRAPH_CONTRACT_IDS",
    "INTAKE_GRAPH_SEMANTIC_OCCURRENCES",
    "OUTPUT_ROUTE_TEMPLATES",
    "TASK_ATTEMPT_CONTRACTS",
    "attempt_contract_refs",
]
