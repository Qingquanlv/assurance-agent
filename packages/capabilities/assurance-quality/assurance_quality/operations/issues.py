"""Issue observation, status, reconcile, and review handlers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from collections.abc import Mapping
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_runtime_contracts.ops import InputError, failed_input, validate_model
from graph_engine.artifacts import ArtifactReadError, open_artifact
from graph_engine.canonical import canonical_json_bytes
from graph_engine.plugin_api import TaskContext, TaskOutcome, TaskRequest

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1
from assurance_quality.contracts.agent import FinalizedIssueAnalysisV1
from assurance_quality.contracts.decisions import classify_issue_candidates
from assurance_quality.contracts.issues import (
    ChangeIssueSnapshot,
    IssueAnalysisStatus,
    IssueCandidate,
    IssueCandidateDocument,
    IssueCandidateProposed,
    IssueOccurrence,
    Observation,
    ObservationDocument,
    OccurrenceAnalysis,
    Problem,
    ProblemAssessment,
    ProblemSeenRef,
    ProvisionalAssessment,
    ReconcileIssuesResultV1,
)
from assurance_generation.contracts.workflow import GenerationCycleResultV1
from assurance_quality.contracts.assessment import (
    AssessmentInputsV1,
    InspectionDocumentV1,
    InspectionOutcomeV1,
)
from assurance_quality.derived import derive_reconcile_input
from assurance_quality.operations.common import succeeded
from assurance_quality.operations.identity import (
    candidate_document_digest,
    occurrence_id,
    per_candidate_digest,
    problem_fingerprint,
    problem_id,
)

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_FAILURE_REASONS: dict[str, Literal["timeout", "transport", "invalid_output", "unavailable"]] = {
    "timeout": "timeout",
    "transport": "unavailable",
    "rate_limit": "transport",
    "invalid_output": "invalid_output",
    "unavailable": "unavailable",
}


class AnalysisStatusInput(BaseModel):
    model_config = _FROZEN

    change_id: str
    batch_id: str
    evidence_bundle_digest: str
    error_kind: str | None = None
    candidate_digest: str | None = None


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
    issue_analysis: dict[str, object] | None = None


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


def _analysis_route_fields(raw: object) -> tuple[str, bool]:
    if raw is None:
        return "unknown", False
    summary = classify_issue_candidates(FinalizedIssueAnalysisV1.model_validate(raw).agent_result)
    return summary.classification, summary.fix_eligible


def _open_reconcile(root: Path, raw: dict[str, Any], key: str, model: type[BaseModel]) -> None:
    ref = raw.get(key)
    if ref is None:
        return
    try:
        opened = open_artifact(root, EvidenceArtifactRefV1.model_validate(ref), model=model)
    except ArtifactReadError as error:
        if error.reason == "digest":
            raise InputError(f"evidence digest changed: {error.path}") from error
        raise InputError(str(error)) from error
    raw[key] = opened.model_dump(mode="json")


def _derive_reconcile_documents(root: Path, raw: Mapping[str, object]) -> dict[str, Any]:
    payload: dict[str, Any] = dict(raw)
    if payload.get("inspection_ref") is not None:
        _open_reconcile(root, payload, "inspection_ref", InspectionDocumentV1)
        document = payload.pop("inspection_ref")
        if isinstance(document, dict) and payload.get("inspection_receipt") is not None:
            document = {**document, "inspection_receipt": payload["inspection_receipt"]}
        payload["inspection_outcome"] = InspectionOutcomeV1.model_validate(document).model_dump(mode="json")
    if payload.get("assessment_ref") is not None:
        _open_reconcile(root, payload, "assessment_ref", AssessmentInputsV1)
        payload["assessment_inputs"] = payload.pop("assessment_ref")
    if payload.get("generation_ref") is not None:
        _open_reconcile(root, payload, "generation_ref", GenerationCycleResultV1)
        payload["generation_result"] = payload.pop("generation_ref")
    return derive_reconcile_input(payload)


class ReconcileIssuesHandler(_Handler):
    input_model = ReconcileInput
    builder = staticmethod(reconcile_issues)

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        try:
            raw = dict(request.input) if isinstance(request.input, dict) else request.input
            if isinstance(raw, dict):
                raw = _derive_reconcile_documents(context.project_root, raw)
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
            classification, fix_eligible = _analysis_route_fields(
                raw.get("issue_analysis") if isinstance(raw, dict) else None
            )
            sealed = ReconcileIssuesResultV1.model_validate(
                {
                    **result,
                    "issue_snapshot_ref": ref.model_dump(mode="json"),
                    "classification": classification,
                    "fix_eligible": fix_eligible,
                    "evidence_refs": [ref.model_dump(mode="json")],
                }
            )
            return succeeded(cast(dict[str, object], sealed.model_dump(mode="json")))
        except InputError as error:
            return failed_input(error)
        except (ValidationError, OSError, json.JSONDecodeError) as error:
            return failed_input(error)


# Re-export proposed types so tests can build candidates without hunting.
IssueCandidateProposed = IssueCandidateProposed
Field = Field
