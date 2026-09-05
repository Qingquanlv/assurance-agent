from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, cast

from agent_runtime_contracts import AgentExecutionContract, AgentPhaseWriteClaims
from graph_engine.attempts import AttemptRetryPolicy, AttemptTimeoutPolicy, TaskAttemptContract
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.plugin_api import AttemptContractRef, ResourceClaimTemplate

from assurance_quality.contracts.agent import (
    FactBaselineResultV1,
    InspectionResultV1,
    IssueAnalysisResultV1,
    IssueTriageResultV1,
    QualitySkillInputV1,
    ReportResultV1,
)
from assurance_quality.contracts.assessment import (
    AssessmentInputsV1,
    AssessmentSkillInputV1,
    FinalizedFactBaselineV1,
    FinalizedInspectionV1,
    FinalizedReportV1,
    MaterializeAssessmentInputV1,
    ReportSkillInputV1,
)

_DOC_AUTHOR = "assurance-v1-doc-author"
_REPORTER = "assurance-v1-reporter"
_REVIEWER = "assurance-v1-reviewer"
_RETRY = AttemptRetryPolicy(max_attempts=1)
_TIMEOUT = AttemptTimeoutPolicy(seconds=60)


def _paths(*suffixes: str) -> tuple[str, ...]:
    return tuple(sorted(f"qa/changes/{{change_id}}/{suffix}" for suffix in suffixes))


def _job(
    base: str,
    skill_id: str,
    agent_profile: str,
    result_model: type[Any],
    outputs: tuple[str, ...],
    *,
    input_model: type[Any] = QualitySkillInputV1,
    output_model: type[Any] | None = None,
) -> AgentExecutionContract[Any, Any, Any]:
    return AgentExecutionContract(
        contract_id=f"assurance.quality.agent.{base}.v1",
        owner_id="assurance.quality",
        prepare_handler_id=f"assurance.quality.{base}.prepare",
        finalize_handler_id=f"assurance.quality.{base}.finalize",
        skill_id=skill_id,
        agent_profile=agent_profile,
        input_model=input_model,
        agent_result_model=result_model,
        output_model=output_model or result_model,
        resources=ResourceClaimTemplate(
            parameters={"change_id": "/workspace/scope_id"},
            reads=("qa",),
            writes=_paths(*outputs),
        ),
        retry=_RETRY,
        timeout=_TIMEOUT,
        validators=(),
        phase_write_claims=AgentPhaseWriteClaims(prepare=(), runtime=_paths(*outputs), finalize=()),
    )


_JOBS: tuple[tuple[str, str, str, type[Any], tuple[str, ...], type[Any], type[Any] | None], ...] = (
    (
        "fact-baseline",
        "aa-fact-baseline",
        _DOC_AUTHOR,
        FactBaselineResultV1,
        ("facts/fact-baseline.json",),
        AssessmentSkillInputV1,
        FinalizedFactBaselineV1,
    ),
    (
        "inspect",
        "aa-inspect",
        _REVIEWER,
        InspectionResultV1,
        ("inspect/inspection.json",),
        AssessmentSkillInputV1,
        FinalizedInspectionV1,
    ),
    (
        "issue-analysis",
        "aa-issue-analyzer",
        _REPORTER,
        IssueAnalysisResultV1,
        ("inspect/issue-analysis.json",),
        QualitySkillInputV1,
        None,
    ),
    (
        "issue-triage",
        "aa-issue-triage-advisor",
        _REPORTER,
        IssueTriageResultV1,
        ("inspect/issue-triage.json",),
        QualitySkillInputV1,
        None,
    ),
    (
        "report",
        "aa-report-generator",
        _REPORTER,
        ReportResultV1,
        ("report/report.md",),
        ReportSkillInputV1,
        FinalizedReportV1,
    ),
)

AGENT_JOB_CONTRACTS: Mapping[str, AgentExecutionContract[Any, Any, Any]] = MappingProxyType(
    {
        base: _job(
            base,
            skill_id,
            profile,
            result_model,
            outputs,
            input_model=input_model,
            output_model=output_model,
        )
        for base, skill_id, profile, result_model, outputs, input_model, output_model in _JOBS
    }
)
OUTPUT_ROUTE_TEMPLATES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {base: _paths(*outputs) for base, _skill, _profile, _result, outputs, _input, _output in _JOBS}
)
_MATERIALIZE_ASSESSMENT = TaskAttemptContract(
    contract_id="assurance.quality.materialize-assessment-inputs",
    owner_id="assurance.quality",
    handler_id="assurance.quality.materialize-assessment-inputs.execute",
    input_model=MaterializeAssessmentInputV1,
    output_model=AssessmentInputsV1,
    resources=ResourceClaimTemplate(
        parameters={
            "batch_id": "/execution/batch_id",
            "change_id": "/reviewed_case/change_id",
            "coverage_epoch": "/coverage_epoch_token",
        },
        reads=(".aa/policy.yaml", "issues", "qa/changes/{change_id}"),
        writes=(
            "qa/changes/{change_id}/inspect/epochs/{coverage_epoch}/batches/{batch_id}/coverage-gaps.json",
            "qa/changes/{change_id}/inspect/epochs/{coverage_epoch}/batches/{batch_id}/metrics.json",
            "qa/changes/{change_id}/inspect/epochs/{coverage_epoch}/batches/{batch_id}/trace-sufficiency.json",
            "qa/changes/{change_id}/inspect/epochs/{coverage_epoch}/batches/{batch_id}/trace.json",
        ),
    ),
    retry=_RETRY,
    timeout=_TIMEOUT,
    validators=(),
)
TASK_ATTEMPT_CONTRACTS: Mapping[str, TaskAttemptContract[Any, Any]] = MappingProxyType(
    {"materialize-assessment-inputs": _MATERIALIZE_ASSESSMENT}
)
QUALITY_GRAPH_CONTRACT_IDS: tuple[str, ...] = (
    _MATERIALIZE_ASSESSMENT.contract_id,
    "assurance.quality.agent.fact-baseline.v1",
    "assurance.quality.agent.inspect.v1",
    "assurance.quality.agent.issue-triage.v1",
    "assurance.quality.agent.issue-analysis.v1",
    "assurance.quality.agent.report.v1",
)
QUALITY_GRAPH_EXPORTS: tuple[str, ...] = (
    "assess",
    "issue_review",
    "issue_analyze",
    "issue_reconcile",
    "report",
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
    "QUALITY_GRAPH_CONTRACT_IDS",
    "QUALITY_GRAPH_EXPORTS",
    "TASK_ATTEMPT_CONTRACTS",
    "attempt_contract_refs",
]
