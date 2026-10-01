"""Authenticate the assessment, then seal the inspection and its failure facts."""

from __future__ import annotations

from agent_runtime_contracts.ops import FinalizeContext, InputError, OutputError, PrepareContext

from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_quality.contracts.agent import InspectionResultV1
from assurance_quality.contracts.assessment import AssessmentSkillInputV1, FinalizedInspectionV1
from assurance_quality.contracts.metrics import MetricsDocument
from assurance_quality.contracts.sufficiency import TraceSufficiencyFacts
from assurance_quality.operations.agent_skills import (
    authenticate_assessment_input,
    load_json_ref,
    staged_agent_document,
)
from assurance_quality.operations.inspect import build_failure_classification_facts

PATH = "qa/results/inspect/inspection.json"


def before(ctx: PrepareContext, business: AssessmentSkillInputV1) -> AssessmentSkillInputV1:
    authenticate_assessment_input(business, ctx.project_root)
    return business


def after(
    ctx: FinalizeContext, business: AssessmentSkillInputV1, result: InspectionResultV1
) -> FinalizedInspectionV1:
    authenticate_assessment_input(business, ctx.project_root)
    if business.fact_baseline_ref is None:
        raise InputError("Inspect requires an authenticated fact baseline")
    staged_agent_document(context=ctx, relative=PATH, result=result, model=InspectionResultV1)
    expected = {
        "execution_digest": business.assessment.execution_ref.digest,
        "healing_digest": (
            business.assessment.healing_ref.digest if business.assessment.healing_ref is not None else None
        ),
        "trace_digest": business.assessment.trace_ref.digest,
        "coverage_digest": business.assessment.gaps_ref.digest,
        "metrics_digest": business.assessment.metrics_ref.digest,
    }
    actual = {
        "execution_digest": result.execution_digest,
        "healing_digest": result.healing_digest,
        "trace_digest": result.trace_digest,
        "coverage_digest": result.coverage_digest,
        "metrics_digest": result.metrics_digest,
    }
    for key, locked in expected.items():
        if actual[key] != locked:
            raise OutputError(f"inspection {key} is not closed against the locked projection")
    if result.change_id != business.change_id or result.batch_id != business.batch_id:
        raise OutputError("inspection identity does not match the locked change")
    metrics = load_json_ref(ctx.project_root, business.assessment.metrics_ref, MetricsDocument)
    sufficiency = load_json_ref(
        ctx.project_root,
        business.assessment.sufficiency_ref,
        TraceSufficiencyFacts,
    )
    execution = load_json_ref(ctx.project_root, business.assessment.execution_ref, ExecutionEvidenceV1)
    failure_facts, reason_codes = build_failure_classification_facts(execution, metrics)
    has_failures = any(item.status == "failed" for item in execution.results)
    if not result.classification_performed:
        raise OutputError("inspection did not complete authenticated classification")
    expected_status = "analyzed" if has_failures else "no_failures"
    if result.status != expected_status:
        raise OutputError(f"inspection status must be {expected_status} for the authenticated execution")
    return FinalizedInspectionV1(
        agent_result=result,
        assessment=business.assessment,
        reviewed_case=business.reviewed_case,
        mapping_ref=business.mapping_ref,
        metrics=metrics,
        sufficiency=sufficiency,
        failure_facts=failure_facts,
        fact_baseline_ref=business.fact_baseline_ref,
        reason_codes=reason_codes,
    )
