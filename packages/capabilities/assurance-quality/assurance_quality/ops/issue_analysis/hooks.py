"""Authenticate the evidence bundle, then seal the issue-analysis document."""

from __future__ import annotations

from agent_runtime_contracts.ops import (
    FinalizeContext,
    InputError,
    OutputError,
    PrepareContext,
)

from assurance_healing.contracts.issue_handoff import (
    ISSUE_ANALYSIS_HANDOFF_PATH,
    IssueAnalysisHandoffV1,
)
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_quality.contracts.agent import (
    FinalizedIssueAnalysisV1,
    IssueAnalysisBoundInputV1,
    IssueAnalysisResultV1,
    IssueAnalysisSkillInputV1,
    QualitySkillInputV1,
)
from assurance_quality.contracts.assessment import (
    AssessmentInputsV1,
    InspectionDocumentV1,
    InspectionOutcomeV1,
)
from assurance_quality.contracts.decisions import (
    AnalysisOutcome,
    IssueAnalysisPublishedV1,
    analysis_route,
    classify_issue_candidates,
)
from assurance_quality.contracts.issues import IssueCandidateDocument
from assurance_quality.derived import derive_issue_analysis_input
from assurance_quality.operations.agent_skills import (
    authenticate_issue_analysis_input,
    open_quality_artifact,
    staged_agent_document,
)
from assurance_quality.operations.identity import candidate_document_digest, problem_fingerprint, problem_id

PATH = "qa/results/inspect/issue-analysis.json"


def require_issue_analysis_ready(
    business: QualitySkillInputV1,
    inspection: InspectionOutcomeV1 | None,
    assessment: AssessmentInputsV1 | None,
) -> None:
    """Diagnostic readiness when both ledger documents are present.

    Neither document means the caller already admitted the input, which is the
    thin entry and the direct agent call. A present pair that disagrees is an
    input failure.
    """
    if inspection is None and assessment is None:
        return
    if inspection is None or assessment is None:
        raise InputError("inspection and assessment must be read together")
    if inspection.disposition not in {"blocked", "analysis_required"}:
        raise InputError("issue analysis requires a failed inspection")
    if not assessment.owned_evidence_ids:
        raise InputError("blocked inspection has no owned observations")
    if (
        assessment.change_id != inspection.change_id
        or assessment.batch_id != inspection.batch_id
        or assessment.coverage_epoch != inspection.coverage_epoch
        or tuple(business.owned_evidence_ids) != tuple(assessment.owned_evidence_ids)
        or business.evidence_bundle_digest != assessment.evidence_bundle_digest
    ):
        raise InputError("inspection is not diagnostic-ready")


def _opened(
    ctx: PrepareContext | FinalizeContext,
    business: IssueAnalysisBoundInputV1,
    error: type[InputError] | type[OutputError],
) -> tuple[IssueAnalysisSkillInputV1, InspectionOutcomeV1 | None, AssessmentInputsV1 | None]:
    inspection = None
    assessment = None
    payload = business.model_dump(mode="json")
    if business.inspection_ref is not None:
        document = open_quality_artifact(
            ctx.project_root, business.inspection_ref, InspectionDocumentV1, error
        )
        data = document.model_dump(mode="json")
        if business.inspection_receipt is not None:
            data["inspection_receipt"] = business.inspection_receipt.model_dump(mode="json")
        inspection = InspectionOutcomeV1.model_validate(data)
        payload["inspection_outcome"] = inspection.model_dump(mode="json")
    if business.assessment_ref is not None:
        assessment = open_quality_artifact(
            ctx.project_root, business.assessment_ref, AssessmentInputsV1, error
        )
        payload["assessment_inputs"] = assessment.model_dump(mode="json")
    if business.generation_ref is not None:
        generation = open_quality_artifact(
            ctx.project_root, business.generation_ref, GenerationCycleResultV1, error
        )
        payload["generation_result"] = generation.model_dump(mode="json")
    filled = derive_issue_analysis_input(payload)
    if "execution_evidence_digest" in filled:
        filled["execution_digest"] = filled.pop("execution_evidence_digest")
    skill_fields = set(IssueAnalysisSkillInputV1.model_fields)
    skill = IssueAnalysisSkillInputV1.model_validate(
        {key: value for key, value in filled.items() if key in skill_fields}
    )
    return skill, inspection, assessment


def before(ctx: PrepareContext, business: IssueAnalysisBoundInputV1) -> IssueAnalysisSkillInputV1:
    skill, inspection, assessment = _opened(ctx, business, InputError)
    require_issue_analysis_ready(skill, inspection, assessment)
    authenticate_issue_analysis_input(skill, ctx.project_root)
    return skill


def _route(business: QualitySkillInputV1, result: IssueAnalysisResultV1, *, path: str) -> AnalysisOutcome:
    if path != PATH:
        return "failed"
    return analysis_route(result, inspection_disposition=business.inspection_disposition)


def _closed(business: QualitySkillInputV1, result: IssueAnalysisResultV1) -> bool:
    identity = (result.change_id, result.batch_id, result.evidence_bundle_digest)
    expected = (business.change_id, business.batch_id, business.evidence_bundle_digest)
    if identity != expected:
        return False
    try:
        result.require_complete_coverage(frozenset(business.owned_evidence_ids))
    except (TypeError, ValueError):
        return False
    return True


def after(
    ctx: FinalizeContext, business: IssueAnalysisBoundInputV1, result: IssueAnalysisResultV1
) -> IssueAnalysisPublishedV1:
    skill, _inspection, _assessment = _opened(ctx, business, OutputError)
    if not skill.evidence_bundle_digest:
        raise InputError("evidence_bundle_digest must authenticate issue analysis")
    if not _closed(skill, result):
        summary = classify_issue_candidates(result)
        return IssueAnalysisPublishedV1(
            route="failed",
            classification=summary.classification,
            fix_eligible=summary.fix_eligible,
        )
    for candidate in result.candidates:
        fingerprint = problem_fingerprint(
            affected_surface=candidate.affected_surface,
            fingerprint_inputs=candidate.fingerprint_inputs,
        )
        expected_id = problem_id(fingerprint)
        if candidate.possible_problem_ids and expected_id not in candidate.possible_problem_ids:
            raise OutputError(f"issue candidate possible_problem_ids does not contain {expected_id}")
    authenticate_issue_analysis_input(skill, ctx.project_root)
    candidates_doc = IssueCandidateDocument(
        schema_version="1.0",
        change_id=result.change_id,
        batch_id=result.batch_id,
        evidence_bundle_digest=result.evidence_bundle_digest,
        candidates=list(result.candidates),
    )
    _, issue_analysis_ref = staged_agent_document(
        context=ctx,
        relative=PATH,
        result=result,
        model=IssueAnalysisResultV1,
    )
    ctx.stage(
        ISSUE_ANALYSIS_HANDOFF_PATH,
        IssueAnalysisHandoffV1(
            coverage_epoch=skill.coverage_epoch,
            issue_analysis_ref=issue_analysis_ref,
        ),
    )
    finalized = FinalizedIssueAnalysisV1(
        agent_result=result,
        candidate_digest=(
            candidate_document_digest(candidates_doc) if result.status == "completed" else None
        ),
        issue_analysis_ref=issue_analysis_ref,
    )
    summary = classify_issue_candidates(result)
    return IssueAnalysisPublishedV1(
        route=_route(skill, result, path=issue_analysis_ref.path),
        classification=summary.classification,
        fix_eligible=summary.fix_eligible,
        evidence_refs=(issue_analysis_ref,),
        issue_analysis=finalized,
        issue_analysis_ref=issue_analysis_ref,
    )
