"""Authenticate the inspection chain, then bind the staged report bytes."""

from __future__ import annotations

from typing import Literal

from agent_runtime_contracts.ops import FinalizeContext, InputError, OutputError, PrepareContext

from assurance_execution.contracts.workflow import ExecutionCycleDocumentV1
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.agent import ReportResultV1
from assurance_quality.contracts.assessment import (
    REPORT_OUTCOME_PATH,
    AssessmentInputsV1,
    FinalizedReportV1,
    InspectionDocumentV1,
    InspectionOutcomeV1,
    ReportBoundInputV1,
    ReportOutcomeDocumentV1,
    ReportPublishedV1,
    ReportSkillInputV1,
)
from assurance_quality.derived import derive_report_input
from assurance_quality.operations.agent_skills import authenticate_report_input, open_quality_artifact

PATH = "qa/results/report/report.md"


def _skill(
    ctx: PrepareContext | FinalizeContext,
    business: ReportBoundInputV1,
    *,
    error: type[InputError] | type[OutputError],
) -> ReportSkillInputV1:
    document = open_quality_artifact(ctx.project_root, business.inspection_ref, InspectionDocumentV1, error)
    inspection = InspectionOutcomeV1.model_validate(
        {
            **document.model_dump(mode="json"),
            "inspection_receipt": business.inspection_receipt.model_dump(mode="json"),
        }
    )
    assessment = open_quality_artifact(ctx.project_root, business.assessment_ref, AssessmentInputsV1, error)
    generation = open_quality_artifact(
        ctx.project_root, business.generation_ref, GenerationCycleResultV1, error
    )
    execution = open_quality_artifact(
        ctx.project_root, business.execution_ref, ExecutionCycleDocumentV1, error
    )
    if execution.coverage_epoch != business.coverage_epoch:
        raise error("execution coverage_epoch does not match")
    filled = derive_report_input(
        {
            "change_id": business.change_id,
            "coverage_epoch": business.coverage_epoch,
            "batch_id": execution.batch_id,
            "purpose": business.purpose,
            "fact_baseline_ref": business.fact_baseline_ref.model_dump(mode="json"),
            "issue_analysis_ref": None
            if business.issue_analysis_ref is None
            else business.issue_analysis_ref.model_dump(mode="json"),
            "capability_leafs": list(business.capability_leafs),
            "allowed_artifact_paths": list(business.artifact_paths),
            "inspection_outcome": inspection.model_dump(mode="json"),
            "inspection_receipt": business.inspection_receipt.model_dump(mode="json"),
            "assessment_inputs": assessment.model_dump(mode="json"),
            "generation_result": generation.model_dump(mode="json"),
        }
    )
    filled["inspection"] = filled.pop("inspection_outcome")
    filled["assessment"] = filled.pop("assessment_inputs")
    filled["generation"] = filled.pop("generation_result")
    if "execution_evidence_digest" in filled:
        filled["execution_digest"] = filled.pop("execution_evidence_digest")
    filled.pop("allowed_artifact_paths", None)
    filled.pop("inspection_receipt", None)
    return ReportSkillInputV1.model_validate(filled)


def before(ctx: PrepareContext, business: ReportBoundInputV1) -> ReportSkillInputV1:
    skill = _skill(ctx, business, error=InputError)
    authenticate_report_input(skill, ctx.project_root)
    return skill


def seal_report(business: ReportSkillInputV1, finalized: FinalizedReportV1) -> ReportPublishedV1:
    """Bind publication from the locked request. The commit receipt stays outside."""

    identity = (finalized.change_id, finalized.coverage_epoch, finalized.batch_id)
    expected = (business.change_id, business.coverage_epoch, business.batch_id)
    stale = (
        identity != expected
        or finalized.inspection_receipt != business.inspection.inspection_receipt
        or finalized.purpose != business.purpose
    )
    if stale:
        return ReportPublishedV1(publication="failed", coverage_state=business.inspection.coverage_state)
    publication: Literal["reported", "diagnostic", "failed"] = (
        "reported" if finalized.purpose == "normal" else "diagnostic"
    )
    outcome = None
    if publication == "reported":
        outcome = {
            "change_id": finalized.change_id,
            "coverage_epoch": finalized.coverage_epoch,
            "batch_id": finalized.batch_id,
            "inspection_receipt": finalized.inspection_receipt.model_dump(mode="json"),
            "plan_digest": finalized.plan_digest,
            "plan_ref": finalized.plan_ref.model_dump(mode="json"),
            "report_refs": [ref.model_dump(mode="json") for ref in finalized.report_refs],
        }
    return ReportPublishedV1(
        publication=publication,
        report_refs=finalized.report_refs,
        coverage_state=business.inspection.coverage_state,
        report_outcome=outcome,
        finalized=finalized,
    )


def after(ctx: FinalizeContext, business: ReportBoundInputV1, result: ReportResultV1) -> ReportPublishedV1:
    if result.report_files != (PATH,):
        raise OutputError("report_files does not match")
    skill = _skill(ctx, business, error=OutputError)
    if result.batch_id != skill.batch_id:
        raise OutputError("agent result batch_id does not match")
    authenticate_report_input(skill, ctx.project_root)
    expected = {
        "case_digest": skill.case_digest,
        "plan_digest": skill.plan_digest,
        "mapping_digest": skill.mapping_digest,
        "execution_digest": skill.execution_digest,
        "healing_digest": skill.healing_digest,
        "trace_digest": skill.trace_digest,
        "coverage_digest": skill.coverage_digest,
        "issue_digest": skill.issue_digest,
        "metrics_digest": skill.metrics_digest,
    }
    actual = {
        "case_digest": result.case_digest,
        "plan_digest": result.plan_digest,
        "mapping_digest": result.mapping_digest,
        "execution_digest": result.execution_digest,
        "healing_digest": result.healing_digest,
        "trace_digest": result.trace_digest,
        "coverage_digest": result.coverage_digest,
        "issue_digest": result.issue_digest,
        "metrics_digest": result.metrics_digest,
    }
    for key, locked in expected.items():
        if actual[key] != locked:
            raise OutputError(f"report {key} is not closed against the locked projection")
    staged = {
        relative
        for path in ctx.write_root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and (relative := path.relative_to(ctx.write_root).as_posix()) != REPORT_OUTCOME_PATH
    }
    if staged != set(result.report_files):
        raise OutputError("report result does not match the actual candidate write set")
    refs = tuple(
        EvidenceArtifactRefV1(path=relative, digest=ctx.ref(relative).digest)
        for relative in result.report_files
    )
    published = _publish(ctx, skill, result, refs)
    return published


def _publish(
    ctx: FinalizeContext,
    business: ReportSkillInputV1,
    result: ReportResultV1,
    refs: tuple[EvidenceArtifactRefV1, ...],
) -> ReportPublishedV1:
    del result
    finalized = FinalizedReportV1(
        change_id=business.change_id,
        coverage_epoch=business.coverage_epoch,
        batch_id=business.batch_id,
        purpose=business.purpose,
        plan_digest=business.plan_digest,
        plan_ref=business.plan_ref,
        inspection_receipt=business.inspection.inspection_receipt,
        report_refs=refs,
    )
    published = seal_report(business, finalized)
    if published.report_outcome is not None:
        ctx.stage(
            REPORT_OUTCOME_PATH,
            ReportOutcomeDocumentV1.model_validate(published.report_outcome),
        )
    return published
