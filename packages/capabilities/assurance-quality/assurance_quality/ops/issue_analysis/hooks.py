"""Authenticate the evidence bundle, then seal the issue-analysis document."""

from __future__ import annotations

from agent_runtime_contracts.ops import FinalizeContext, InputError, OutputError, PrepareContext

from assurance_quality.contracts.agent import (
    FinalizedIssueAnalysisV1,
    IssueAnalysisResultV1,
    QualitySkillInputV1,
)
from assurance_quality.contracts.issues import IssueCandidateDocument
from assurance_quality.operations.agent_skills import authenticate_issue_analysis_input, staged_agent_document
from assurance_quality.operations.identity import candidate_document_digest, problem_fingerprint, problem_id

PATH = "qa/results/inspect/issue-analysis.json"


def before(ctx: PrepareContext, business: QualitySkillInputV1) -> QualitySkillInputV1:
    authenticate_issue_analysis_input(business, ctx.project_root)
    return business


def after(
    ctx: FinalizeContext, business: QualitySkillInputV1, result: IssueAnalysisResultV1
) -> FinalizedIssueAnalysisV1:
    if result.change_id != business.change_id or result.batch_id != business.batch_id:
        raise OutputError("issue analysis identity does not match the locked change")
    if not business.evidence_bundle_digest:
        raise InputError("evidence_bundle_digest must authenticate issue analysis")
    if result.evidence_bundle_digest != business.evidence_bundle_digest:
        raise OutputError("issue analysis evidence bundle is not authenticated")
    owned = frozenset(business.owned_evidence_ids)
    try:
        result.require_complete_coverage(owned)
    except ValueError as error:
        raise OutputError(str(error)) from error
    for candidate in result.candidates:
        try:
            fingerprint = problem_fingerprint(
                affected_surface=candidate.affected_surface,
                fingerprint_inputs=candidate.fingerprint_inputs,
            )
        except ValueError as error:
            raise OutputError(f"issue candidate {candidate.candidate_id}: {error}") from error
        expected_id = problem_id(fingerprint)
        if candidate.possible_problem_ids and expected_id not in candidate.possible_problem_ids:
            raise OutputError(f"issue candidate possible_problem_ids does not contain {expected_id}")
    authenticate_issue_analysis_input(business, ctx.project_root)
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
    return FinalizedIssueAnalysisV1(
        agent_result=result,
        candidate_digest=(
            candidate_document_digest(candidates_doc) if result.status == "completed" else None
        ),
        issue_analysis_ref=issue_analysis_ref,
    )
