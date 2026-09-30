"""Issue observation, status, reconcile, and review handlers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_runtime_contracts.ops import InputError, failed_input, validate_model
from graph_engine.canonical import canonical_json_bytes
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.issues import (
    ChangeIssueSnapshot,
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
    ReconcileIssuesResultV1,
)
from assurance_quality.operations.common import succeeded
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
    observations: tuple[Observation, ...] = ()
    candidates: tuple[IssueCandidate, ...] = ()
    evidence_bundle_digest: str
    analyzer: str = "assurance.quality"
    prompt_version: str = "1"
    observations_ref: EvidenceArtifactRefV1 | None = None


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
        "batches": [payload.batch_id],
    }


_SNAPSHOT_PATH = "qa/results/issues/snapshot.json"


def _load_observations(context: TaskContext, ref: EvidenceArtifactRefV1) -> tuple[Observation, ...]:
    for root in (context.write_root, context.project_root):
        path = root.joinpath(*PurePosixPath(ref.path).parts)
        if not path.is_file():
            continue
        document = ObservationDocument.model_validate(json.loads(path.read_bytes()))
        return tuple(document.observations)
    raise InputError(f"observations not found: {ref.path}")


def _write_snapshot(write_root: Path, snapshot: ChangeIssueSnapshot) -> EvidenceArtifactRefV1:
    data = canonical_json_bytes(snapshot.model_dump(mode="json"))
    destination = write_root.joinpath(*PurePosixPath(_SNAPSHOT_PATH).parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return EvidenceArtifactRefV1(path=_SNAPSHOT_PATH, digest=hashlib.sha256(data).hexdigest())


def _snapshot_from_reconcile(result: dict[str, object]) -> ChangeIssueSnapshot:
    return ChangeIssueSnapshot.model_validate(
        {
            "schema_version": result["schema_version"],
            "change_id": result["change_id"],
            "authoritative_batch_id": result["authoritative_batch_id"],
            "observations": result["observations"],
            "occurrences": result["occurrences"],
            "analysis_status": result["analysis_status"],
            "project_sync_status": result["project_sync_status"],
            "batches": result["batches"],
        }
    )


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
            payload = validate_model(self.input_model, request.input)
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

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        return await super().execute(request, context)


class RecordEmptyIssueAnalysisHandler(_Handler):
    input_model = AnalysisStatusInput
    builder = staticmethod(empty_analysis)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        return await super().execute(request, context)


class RecordIssueAnalysisFailureHandler(_Handler):
    input_model = AnalysisStatusInput
    builder = staticmethod(failed_analysis)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        return await super().execute(request, context)


class RecordProjectSyncPendingHandler(_Handler):
    input_model = SyncPendingInput
    builder = staticmethod(sync_pending)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        return await super().execute(request, context)


class ReconcileIssuesHandler(_Handler):
    input_model = ReconcileInput
    builder = staticmethod(reconcile_issues)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            raw = dict(request.input) if isinstance(request.input, dict) else request.input
            if isinstance(raw, dict) and not raw.get("observations") and raw.get("observations_ref"):
                ref = EvidenceArtifactRefV1.model_validate(raw["observations_ref"])
                loaded = _load_observations(context, ref)
                raw = {
                    **raw,
                    "observations": [item.model_dump(mode="json") for item in loaded],
                }
            payload = validate_model(ReconcileInput, raw)
            result = reconcile_issues(payload)
            snapshot = _snapshot_from_reconcile(result)
            ref = _write_snapshot(context.write_root, snapshot)
            sealed = ReconcileIssuesResultV1.model_validate(
                {**result, "issue_snapshot_ref": ref.model_dump(mode="json")}
            )
            return succeeded(cast(dict[str, object], sealed.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except (ValidationError, OSError, json.JSONDecodeError) as error:
            return failed_input(error)


class LoadProblemReviewContextHandler(_Handler):
    input_model = ReviewContextInput
    builder = staticmethod(load_review_context)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        return await super().execute(request, context)


class ApplyProblemReviewHandler(_Handler):
    input_model = ApplyReviewInput
    builder = staticmethod(apply_problem_review)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        return await super().execute(request, context)


# Re-export proposed types so tests can build candidates without hunting.
IssueCandidateProposed = IssueCandidateProposed
Field = Field
