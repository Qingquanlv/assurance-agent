"""Issue observation, status, reconcile, and review handlers."""

from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_quality.contracts.issues import (
    IssueAnalysisStatus,
    IssueCandidate,
    IssueCandidateDocument,
    IssueCandidateProposed,
    IssueOccurrence,
    Observation,
    ObservationDocument,
    ObservationSource,
    OccurrenceAnalysis,
    Problem,
    ProblemAssessment,
    ProblemSeenRef,
    ProvisionalAssessment,
)
from assurance_quality.operations.common import InputError, failed_input, succeeded, validate_input
from assurance_quality.operations.identity import (
    ObservationIdentityInput,
    candidate_document_digest,
    event_id,
    observation_id,
    occurrence_id,
    per_candidate_digest,
    problem_fingerprint,
    problem_id,
    review_id,
)

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_FAILURE_REASONS: dict[str, Literal["timeout", "transport", "invalid_output", "unavailable"]] = {
    "timeout": "timeout",
    "transport": "unavailable",
    "rate_limit": "transport",
    "invalid_output": "invalid_output",
    "unavailable": "unavailable",
}


class CollectObservationsInput(BaseModel):
    model_config = _FROZEN

    change_id: str
    batch_id: str
    kind: Literal[
        "test_failure",
        "warning",
        "anomaly",
        "workaround",
        "coverage_gap",
        "performance_signal",
        "environment_signal",
        "review_finding",
    ] = "test_failure"
    target: Literal["api", "e2e", "fuzz", "performance", "coverage"] = "api"
    case_id: str | None = None
    source_artifact: str
    source_json_pointer: str
    evidence_refs: tuple[str, ...]
    signature: str
    message: str | None = None
    observed_at: str


class AnalysisStatusInput(BaseModel):
    model_config = _FROZEN

    change_id: str
    batch_id: str
    evidence_bundle_digest: str
    error_kind: str | None = None
    candidate_digest: str | None = None


class SyncPendingInput(BaseModel):
    model_config = _FROZEN

    change_id: str
    batch_id: str
    evidence_bundle_digest: str
    candidate_digest: str


class ReconcileInput(BaseModel):
    model_config = _FROZEN

    change_id: str
    batch_id: str
    observations: tuple[Observation, ...]
    candidates: tuple[IssueCandidate, ...]
    evidence_bundle_digest: str
    analyzer: str = "assurance.quality"
    prompt_version: str = "1"


class ReviewContextInput(BaseModel):
    model_config = _FROZEN

    change_id: str
    problem: Problem
    occurrence_id: str
    candidate_id: str | None = None


class ApplyReviewInput(BaseModel):
    model_config = _FROZEN

    change_id: str
    problem: Problem
    review_id: str
    action: Literal[
        "confirm_assessment",
        "mark_not_an_issue",
        "accept_risk",
        "start_work",
        "reopen",
        "submit_resolution",
    ]
    evidence_digest: str
    expected_problem_version: int | None = None


def collect_observations(payload: CollectObservationsInput) -> ObservationDocument:
    signature = payload.message if payload.message is not None else payload.signature
    identity = ObservationIdentityInput(
        change_id=payload.change_id,
        batch_id=payload.batch_id,
        kind=payload.kind,
        target=payload.target,
        case_id=payload.case_id,
        source_artifact=payload.source_artifact,
        source_json_pointer=payload.source_json_pointer,
        signature=signature,
    )
    observation = Observation(
        observation_id=observation_id(identity),
        change_id=payload.change_id,
        batch_id=payload.batch_id,
        kind=payload.kind,
        target=payload.target,
        case_id=payload.case_id,
        source=ObservationSource(artifact=payload.source_artifact, json_pointer=payload.source_json_pointer),
        evidence_refs=list(payload.evidence_refs),
        signature=signature,
        observed_at=payload.observed_at,
    )
    return ObservationDocument(
        schema_version="1.0",
        change_id=payload.change_id,
        batch_id=payload.batch_id,
        observations=[observation],
    )


def empty_analysis(payload: AnalysisStatusInput) -> IssueAnalysisStatus:
    empty = IssueCandidateDocument(
        schema_version="1.0",
        change_id=payload.change_id,
        batch_id=payload.batch_id,
        evidence_bundle_digest=payload.evidence_bundle_digest,
        candidates=[],
    )
    return IssueAnalysisStatus(
        schema_version="1.0",
        change_id=payload.change_id,
        batch_id=payload.batch_id,
        status="completed",
        evidence_bundle_digest=payload.evidence_bundle_digest,
        candidate_count=0,
        candidate_digest=candidate_document_digest(empty),
    )


def failed_analysis(payload: AnalysisStatusInput) -> IssueAnalysisStatus:
    kind = payload.error_kind or "unavailable"
    reason = _FAILURE_REASONS.get(kind)
    if reason is None:
        raise InputError(f"unknown analysis error kind: {kind}")
    return IssueAnalysisStatus(
        schema_version="1.0",
        change_id=payload.change_id,
        batch_id=payload.batch_id,
        status="failed",
        evidence_bundle_digest=payload.evidence_bundle_digest,
        candidate_count=0,
        reason=reason,
        retryable=reason != "invalid_output",
    )


def sync_pending(payload: SyncPendingInput) -> dict[str, object]:
    key = f"project_sync_pending:{payload.change_id}:{payload.batch_id}:{payload.candidate_digest}"
    return {
        "project_sync_status": "pending",
        "change_id": payload.change_id,
        "batch_id": payload.batch_id,
        "candidate_digest": payload.candidate_digest,
        "evidence_bundle_digest": payload.evidence_bundle_digest,
        "event_id": event_id(key),
    }


def reconcile_issues(payload: ReconcileInput) -> dict[str, object]:
    document = IssueCandidateDocument(
        schema_version="1.0",
        change_id=payload.change_id,
        batch_id=payload.batch_id,
        evidence_bundle_digest=payload.evidence_bundle_digest,
        candidates=list(payload.candidates),
    )
    digest = candidate_document_digest(document)
    occurrences: list[IssueOccurrence] = []
    problems: list[Problem] = []
    for candidate in payload.candidates:
        fingerprint = problem_fingerprint(
            affected_surface=candidate.affected_surface,
            fingerprint_inputs=candidate.fingerprint_inputs,
        )
        identified = problem_id(fingerprint)
        occ = occurrence_id(payload.change_id, payload.batch_id, per_candidate_digest(candidate))
        seen = ProblemSeenRef(change_id=payload.change_id, occurrence_id=occ)
        occurrences.append(
            IssueOccurrence(
                occurrence_id=occ,
                change_id=payload.change_id,
                batch_id=payload.batch_id,
                observation_ids=list(candidate.observation_ids),
                problem_id=identified,
                provisional_assessment=ProvisionalAssessment(
                    classification=candidate.proposed.classification,
                    severity=candidate.proposed.severity,
                    authority="llm_provisional",
                    root_cause_hypothesis=candidate.proposed.root_cause_hypothesis,
                ),
                analysis=OccurrenceAnalysis(
                    evidence_bundle_digest=payload.evidence_bundle_digest,
                    analyzer=payload.analyzer,
                    prompt_version=payload.prompt_version,
                    candidate_digest=per_candidate_digest(candidate),
                ),
            )
        )
        problems.append(
            Problem(
                problem_id=identified,
                fingerprint=fingerprint,
                title=candidate.proposed.title,
                assessment=ProblemAssessment(
                    classification=candidate.proposed.classification,
                    severity=candidate.proposed.severity,
                    authority="llm_provisional",
                    root_cause_hypothesis=candidate.proposed.root_cause_hypothesis,
                ),
                status="detected",
                first_seen=seen,
                last_seen=seen,
                occurrences=[occ],
                version=1,
            )
        )
    return {
        "schema_version": "1.0",
        "change_id": payload.change_id,
        "authoritative_batch_id": payload.batch_id,
        "observations": [item.model_dump(mode="json") for item in payload.observations],
        "occurrences": [item.model_dump(mode="json") for item in occurrences],
        "problems": [item.model_dump(mode="json") for item in problems],
        "analysis_status": IssueAnalysisStatus(
            schema_version="1.0",
            change_id=payload.change_id,
            batch_id=payload.batch_id,
            status="completed",
            evidence_bundle_digest=payload.evidence_bundle_digest,
            candidate_count=len(payload.candidates),
            candidate_digest=digest,
        ).model_dump(mode="json"),
        "candidate_digest": digest,
        "project_sync_status": "completed",
    }


def load_review_context(payload: ReviewContextInput) -> dict[str, object]:
    identified = review_id(payload.problem.problem_id, payload.problem.version)
    return {
        "review_id": identified,
        "problem_id": payload.problem.problem_id,
        "expected_problem_version": payload.problem.version,
        "change_id": payload.change_id,
        "occurrence_id": payload.occurrence_id,
        "candidate_id": payload.candidate_id,
    }


def apply_problem_review(payload: ApplyReviewInput) -> dict[str, object]:
    expected = payload.expected_problem_version or payload.problem.version
    expected_id = review_id(payload.problem.problem_id, expected)
    if payload.review_id != expected_id:
        raise InputError("review_id does not authenticate the reviewed problem version")
    return {
        "problem_id": payload.problem.problem_id,
        "review_id": payload.review_id,
        "action": payload.action,
        "expected_problem_version": expected,
        "evidence_digest": payload.evidence_digest,
        "change_id": payload.change_id,
    }


class _Handler:
    input_model: type[BaseModel]
    builder: object

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        del context
        try:
            payload = validate_input(self.input_model, request.input)
            result = self.builder(payload)  # type: ignore[operator]
            if hasattr(result, "model_dump"):
                return succeeded(cast(dict[str, object], result.model_dump(mode="json")))
            return succeeded(cast(dict[str, object], result))
        except InputError as error:
            return failed_input(error)
        except ValidationError as error:
            return failed_input(error)


class CollectObservationsHandler(_Handler):
    input_model = CollectObservationsInput
    builder = staticmethod(collect_observations)


class RecordEmptyIssueAnalysisHandler(_Handler):
    input_model = AnalysisStatusInput
    builder = staticmethod(empty_analysis)


class RecordIssueAnalysisFailureHandler(_Handler):
    input_model = AnalysisStatusInput
    builder = staticmethod(failed_analysis)


class RecordProjectSyncPendingHandler(_Handler):
    input_model = SyncPendingInput
    builder = staticmethod(sync_pending)


class ReconcileIssuesHandler(_Handler):
    input_model = ReconcileInput
    builder = staticmethod(reconcile_issues)


class LoadProblemReviewContextHandler(_Handler):
    input_model = ReviewContextInput
    builder = staticmethod(load_review_context)


class ApplyProblemReviewHandler(_Handler):
    input_model = ApplyReviewInput
    builder = staticmethod(apply_problem_review)


# Re-export proposed types so tests can build candidates without hunting.
IssueCandidateProposed = IssueCandidateProposed
Field = Field
