"""Finalize the issue-analysis Agent result."""

from __future__ import annotations

from typing import cast

from agent_runtime_contracts.ops import InputError, OutputError, run_finalize, validate_output
from graph_engine.canonical import JSONValue
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_quality.contracts.agent import (
    AgentFinalizeInputV1,
    FinalizedIssueAnalysisV1,
    IssueAnalysisResultV1,
)
from assurance_quality.contracts.issues import IssueCandidateDocument
from assurance_quality.operations.agent_skills import (
    authenticate_issue_analysis_input,
    staged_agent_document,
    structured,
)
from assurance_quality.operations.identity import candidate_document_digest, problem_fingerprint, problem_id

input_model = AgentFinalizeInputV1


def _commit(payload: AgentFinalizeInputV1, context: TaskContext) -> TaskOutcome:
    document = validate_output(IssueAnalysisResultV1, structured(payload))
    if document.change_id != payload.change_id or document.batch_id != payload.batch_id:
        raise OutputError("issue analysis identity does not match the locked change")
    if not payload.evidence_bundle_digest:
        raise InputError("evidence_bundle_digest must authenticate issue analysis")
    if document.evidence_bundle_digest != payload.evidence_bundle_digest:
        raise OutputError("issue analysis evidence bundle is not authenticated")
    owned = frozenset(payload.owned_evidence_ids)
    try:
        document.require_complete_coverage(owned)
    except ValueError as error:
        raise OutputError(str(error)) from error
    for candidate in document.candidates:
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
    authenticate_issue_analysis_input(payload, context.project_root)
    candidates_doc = IssueCandidateDocument(
        schema_version="1.0",
        change_id=document.change_id,
        batch_id=document.batch_id,
        evidence_bundle_digest=document.evidence_bundle_digest,
        candidates=list(document.candidates),
    )
    relative = "qa/results/inspect/issue-analysis.json"
    _, issue_analysis_ref = staged_agent_document(
        context=context,
        relative=relative,
        result=document,
        model=IssueAnalysisResultV1,
    )
    finalized = FinalizedIssueAnalysisV1(
        agent_result=document,
        candidate_digest=(
            candidate_document_digest(candidates_doc) if document.status == "completed" else None
        ),
        issue_analysis_ref=issue_analysis_ref,
    )
    return TaskOutcome.succeeded(cast(JSONValue, finalized.model_dump(mode="json")))


async def execute(request: TaskRequest, context: TaskContext) -> TaskOutcome:
    return run_finalize(request, context, input_model=AgentFinalizeInputV1, commit=_commit)
