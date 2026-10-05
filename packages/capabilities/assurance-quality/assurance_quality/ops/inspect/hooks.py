"""Authenticate the assessment, then seal the inspection and its failure facts."""

from __future__ import annotations

from agent_runtime_contracts.ops import FinalizeContext, InputError, OutputError, PrepareContext

from assurance_execution.contracts.evidence import ExecutionEvidenceV1
from assurance_execution.contracts.workflow import ExecutionCycleDocumentV1, ExecutionCycleResultV1
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_intake.contracts.workflow import ReviewedCaseV1
from assurance_quality.contracts.agent import InspectionResultV1
from assurance_intake.contracts.coverage_rework import (
    COVERAGE_REWORK_HANDOFF_PATH,
    CoverageReworkHandoffV1,
)
from assurance_quality.contracts.assessment import (
    INSPECTION_OUTCOME_PATH,
    AssessmentInputsV1,
    AssessmentSkillInputV1,
    FinalizedInspectionV1,
    InspectBoundInputV1,
    InspectPublishedV1,
    InspectionDocumentV1,
)
from assurance_quality.contracts.coverage import classify_coverage_state
from assurance_quality.contracts.decisions import (
    classify_inspection_disposition,
    merge_obligation_disposition,
)
from assurance_quality.contracts.metrics import MetricsDocument
from assurance_quality.contracts.obligations import obligation_gate
from assurance_quality.contracts.sufficiency import TraceSufficiencyFacts
from assurance_quality.operations.agent_skills import (
    authenticate_assessment_input,
    captured_agent_document,
    load_json_ref,
    open_quality_artifact,
)
from assurance_quality.operations.inspect import build_failure_classification_facts

PATH = "qa/results/inspect/inspection.json"


def _skill(
    ctx: PrepareContext | FinalizeContext,
    business: InspectBoundInputV1,
    *,
    error: type[InputError] | type[OutputError],
) -> AssessmentSkillInputV1:
    reviewed = open_quality_artifact(ctx.project_root, business.reviewed_case_ref, ReviewedCaseV1, error)
    generation = open_quality_artifact(
        ctx.project_root, business.generation_ref, GenerationCycleResultV1, error
    )
    document = open_quality_artifact(
        ctx.project_root, business.execution_ref, ExecutionCycleDocumentV1, error
    )
    assessment = open_quality_artifact(ctx.project_root, business.assessment_ref, AssessmentInputsV1, error)
    if document.repair_round != business.repair_round or document.coverage_epoch != business.coverage_epoch:
        raise error("execution repair_round does not match")
    execution = ExecutionCycleResultV1.model_validate(
        {**document.model_dump(mode="json"), "receipt": business.execution_receipt.model_dump(mode="json")}
    )
    return AssessmentSkillInputV1(
        change_id=business.change_id,
        coverage_epoch=business.coverage_epoch,
        repair_round=execution.repair_round,
        batch_id=execution.batch_id,
        plan_digest=business.plan_digest,
        plan_ref=business.plan_ref,
        capability_leafs=business.capability_leafs,
        artifact_paths=business.artifact_paths,
        policy_sha256=business.product_policy.sha256,
        assessment_ref=business.assessment_ref,
        assessment=assessment,
        reviewed_case=reviewed,
        mapping_ref=generation.mapping_ref,
        fact_baseline_ref=business.fact_baseline_ref,
        validation_error=business.validation_error,
    )


def before(ctx: PrepareContext, business: InspectBoundInputV1) -> AssessmentSkillInputV1:
    skill = _skill(ctx, business, error=InputError)
    if skill.assessment is None:
        if business.assessment_ref is None:
            raise InputError("assessment is missing")
        loaded = load_json_ref(ctx.project_root, skill.assessment_ref, AssessmentInputsV1)
        skill = skill.model_copy(update={"assessment": loaded})
    authenticate_assessment_input(skill, ctx.project_root)
    return skill


def after(
    ctx: FinalizeContext,
    business: InspectBoundInputV1,
    result: InspectionResultV1,
) -> InspectPublishedV1:
    skill = _skill(ctx, business, error=OutputError)
    if result.batch_id != skill.batch_id:
        raise OutputError("agent result batch_id does not match")
    authenticate_assessment_input(skill, ctx.project_root)
    assessment = skill.assessment
    if assessment is None:
        raise InputError("Inspect requires the loaded assessment")
    if business.fact_baseline_ref is None:
        raise InputError("Inspect requires an authenticated fact baseline")
    captured_agent_document(ctx, PATH, result)
    expected = {
        "execution_digest": assessment.execution_ref.digest,
        "healing_digest": assessment.healing_ref.digest if assessment.healing_ref is not None else None,
        "trace_digest": assessment.trace_ref.digest,
        "coverage_digest": assessment.gaps_ref.digest,
        "metrics_digest": assessment.metrics_ref.digest,
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
    metrics = load_json_ref(ctx.project_root, assessment.metrics_ref, MetricsDocument)
    sufficiency = load_json_ref(ctx.project_root, assessment.sufficiency_ref, TraceSufficiencyFacts)
    execution = load_json_ref(ctx.project_root, assessment.execution_ref, ExecutionEvidenceV1)
    failure_facts, reason_codes = build_failure_classification_facts(execution, metrics)
    has_failures = any(item.status == "failed" for item in execution.results)
    if not result.classification_performed:
        raise OutputError("inspection did not complete authenticated classification")
    expected_status = "analyzed" if has_failures else "no_failures"
    if result.status != expected_status:
        raise OutputError(f"inspection status must be {expected_status} for the authenticated execution")
    finalized = FinalizedInspectionV1(
        agent_result=result,
        assessment=assessment,
        reviewed_case=skill.reviewed_case,
        mapping_ref=skill.mapping_ref,
        metrics=metrics,
        sufficiency=sufficiency,
        failure_facts=failure_facts,
        fact_baseline_ref=business.fact_baseline_ref,
        reason_codes=reason_codes,
    )
    published = seal_inspection(skill, finalized)
    ctx.stage(INSPECTION_OUTCOME_PATH, published.inspection_outcome)
    outcome = published.inspection_outcome
    ctx.stage(
        COVERAGE_REWORK_HANDOFF_PATH,
        CoverageReworkHandoffV1(
            source_epoch=outcome.coverage_epoch,
            assessment_refs=outcome.assessment_refs,
        ),
    )
    return published


def seal_inspection(
    business: AssessmentSkillInputV1,
    finalized: FinalizedInspectionV1,
) -> InspectPublishedV1:
    """Classify against the op input. The commit receipt stays outside this document."""

    assessment = business.assessment
    if assessment is None or finalized.assessment != assessment:
        raise ValueError("inspection was finalized against stale assessment inputs")
    if finalized.reviewed_case != business.reviewed_case:
        raise ValueError("inspection was finalized against a stale Reviewed Case")
    if finalized.mapping_ref != business.mapping_ref:
        raise ValueError("inspection was finalized against a stale test mapping")
    if business.fact_baseline_ref is None or finalized.fact_baseline_ref != business.fact_baseline_ref:
        raise ValueError("inspection was finalized against a stale fact baseline")
    if (
        business.change_id != assessment.change_id
        or business.coverage_epoch != assessment.coverage_epoch
        or business.batch_id != assessment.batch_id
        or business.policy_sha256 != assessment.scope.policy_digest
    ):
        raise ValueError("inspection identity no longer matches the active assessment cycle")
    facts = finalized.failure_facts
    has_execution_problem = (
        not facts.identity_valid
        or facts.blocking_failure
        or facts.analysis_required
        or facts.needs_human
        or facts.repairable_failure
    )
    coverage_state = None
    if not has_execution_problem:
        coverage_state = classify_coverage_state(
            metrics=finalized.metrics,
            sufficiency=finalized.sufficiency,
            scope=assessment.scope,
            policy=assessment.policy,
        )
    disposition = classify_inspection_disposition(facts=facts, coverage_state=coverage_state)
    obligation_decision = obligation_gate(assessment.obligation_gate_facts)
    disposition = merge_obligation_disposition(disposition, obligation_decision)
    if disposition == "coverage_insufficient":
        coverage_state = "repair_required"
    elif disposition != "satisfied":
        coverage_state = None
    reason_codes = set(finalized.reason_codes)
    if coverage_state is not None:
        reason_codes.add(f"coverage.{coverage_state}")
    reason_codes.add(f"obligation.{obligation_decision}")
    if assessment.obligation_gate_facts.required_count == 0:
        reason_codes.add("obligation.no_verifiable_scope")
    assessment_refs = tuple(
        sorted(
            (
                assessment.trace_ref,
                assessment.gaps_ref,
                assessment.metrics_ref,
                assessment.sufficiency_ref,
                assessment.execution_ref,
                assessment.observations_ref,
                assessment.obligation_assessment_ref,
                assessment.issue_evidence_manifest_ref,
                *(() if assessment.healing_ref is None else (assessment.healing_ref,)),
                *(() if assessment.issue_ref is None else (assessment.issue_ref,)),
                finalized.fact_baseline_ref,
            ),
            key=lambda item: (item.path, item.digest),
        )
    )
    outcome = InspectionDocumentV1(
        change_id=assessment.change_id,
        coverage_epoch=assessment.coverage_epoch,
        batch_id=assessment.batch_id,
        plan_digest=assessment.plan_digest,
        plan_ref=assessment.plan_ref,
        disposition=disposition,
        reviewed_case=finalized.reviewed_case,
        mapping_ref=finalized.mapping_ref,
        assessment_refs=assessment_refs,
        reason_codes=tuple(sorted(reason_codes)),
        coverage_state=coverage_state,
    )
    return InspectPublishedV1(
        disposition=disposition,
        coverage_state=coverage_state,
        inspection_outcome=outcome,
        evidence_refs=assessment_refs,
        observations_ref=assessment.observations_ref,
        issue_evidence_manifest_ref=assessment.issue_evidence_manifest_ref,
        owned_evidence_ids=assessment.owned_evidence_ids,
        evidence_bundle_digest=assessment.evidence_bundle_digest,
        assessment=assessment,
        finalized=finalized,
    )
