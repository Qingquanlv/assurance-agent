import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.issues import (
    ChangeIssueSnapshot,
    IssueAnalysisStatus,
    IssueCandidate,
    IssueCandidateDocument,
    IssueCandidateProposed,
    IssueEvidenceManifest,
    IssueOccurrence,
    IssueReconcileStatus,
    Observation,
    ObservationDocument,
    ObservationSource,
    Problem,
    ProblemProjection,
    ProblemReviewQueue,
)


def make_observation(**overrides: object) -> dict:
    doc: dict = {
        "observation_id": "OBS-abc123",
        "change_id": "RET-dept-management",
        "batch_id": "20260725-124844",
        "kind": "test_failure",
        "target": "api",
        "case_id": "API_DEPT_NEG_001",
        "source": {
            "artifact": "execution/runs/20260725-124844/api-result.json",
            "json_pointer": "/cases/3",
        },
        "evidence_refs": ["execution/runs/20260725-124844/raw/api.log#L42"],
        "signature": "http_500_on_empty_department_name",
        "observed_at": "2026-07-25T04:48:44Z",
    }
    doc.update(overrides)
    return doc


def make_candidate(**overrides: object) -> dict:
    doc: dict = {
        "candidate_id": "CAND-001",
        "observation_ids": ["OBS-abc123"],
        "proposed": {
            "title": "Empty department name returns HTTP 500",
            "classification": "product_bug",
            "severity": "high",
            "root_cause_hypothesis": "Request validation is missing before persistence.",
        },
        "affected_surface": {"kind": "endpoint", "value": "POST /api/v1/dept"},
        "fingerprint_inputs": {
            "surface": "POST /api/v1/dept",
            "symptom": "invalid_empty_name_returns_500",
        },
        "possible_problem_ids": [],
        "confidence": 0.91,
        "recommended_action": "Add request validation and verify the negative case.",
    }
    doc.update(overrides)
    return doc


def make_occurrence(**overrides: object) -> dict:
    doc: dict = {
        "occurrence_id": "OCC-def456",
        "change_id": "RET-dept-management",
        "batch_id": "20260725-124844",
        "observation_ids": ["OBS-abc123"],
        "problem_id": "PROB-ghi789",
        "provisional_assessment": {
            "classification": "product_bug",
            "severity": "high",
            "authority": "llm_provisional",
            "root_cause_hypothesis": "Request validation is missing before persistence.",
        },
        "analysis": {
            "evidence_bundle_digest": "sha256:evidence",
            "analyzer": "issue-analyzer",
            "prompt_version": "1",
            "candidate_digest": "sha256:candidate",
        },
    }
    doc.update(overrides)
    return doc


def make_problem(**overrides: object) -> dict:
    doc: dict = {
        "problem_id": "PROB-ghi789",
        "fingerprint": {"version": "1", "digest": "sha256:fingerprint"},
        "title": "Empty department name returns HTTP 500",
        "assessment": {
            "classification": "product_bug",
            "severity": "high",
            "authority": "llm_provisional",
            "root_cause_hypothesis": "Request validation is missing before persistence.",
        },
        "status": "detected",
        "first_seen": {"change_id": "RET-dept-management", "occurrence_id": "OCC-def456"},
        "last_seen": {"change_id": "RET-dept-management", "occurrence_id": "OCC-def456"},
        "occurrences": ["OCC-def456"],
        "resolution": None,
        "version": 1,
    }
    doc.update(overrides)
    return doc


def test_observation_valid_fixture_parses() -> None:
    model = Observation.model_validate(make_observation())
    assert model.observation_id == "OBS-abc123"
    assert model.kind == "test_failure"
    assert model.source.artifact.endswith("api-result.json")
    assert model.source.json_pointer == "/cases/3"


def test_observation_unknown_kind_fails() -> None:
    with pytest.raises(ValidationError):
        Observation.model_validate(make_observation(kind="mystery"))


def test_observation_document_wraps_batch() -> None:
    model = ObservationDocument.model_validate(
        {
            "schema_version": "1.0",
            "change_id": "RET-dept-management",
            "batch_id": "20260725-124844",
            "observations": [make_observation()],
        }
    )
    assert len(model.observations) == 1
    assert model.observations[0].observation_id == "OBS-abc123"


def test_issue_evidence_manifest_valid_fixture_parses() -> None:
    model = IssueEvidenceManifest.model_validate(
        {
            "schema_version": "1.0",
            "change_id": "RET-dept-management",
            "batch_id": "20260725-124844",
            "digest": "sha256:bundle",
            "entries": [
                {
                    "path": "execution/runs/20260725-124844/api-result.json",
                    "digest": "sha256:api-result",
                }
            ],
        }
    )
    assert model.digest == "sha256:bundle"
    assert model.entries[0].path.endswith("api-result.json")


def test_issue_candidate_valid_fixture_parses() -> None:
    model = IssueCandidate.model_validate(make_candidate())
    assert model.candidate_id == "CAND-001"
    assert model.proposed.classification == "product_bug"
    assert model.affected_surface.kind == "endpoint"
    assert model.fingerprint_inputs.symptom == "invalid_empty_name_returns_500"


def test_issue_candidate_without_observations_fails() -> None:
    with pytest.raises(ValidationError):
        IssueCandidate.model_validate(make_candidate(observation_ids=[]))


def test_issue_candidate_unknown_classification_fails() -> None:
    doc = make_candidate()
    doc["proposed"]["classification"] = "maybe_bug"
    with pytest.raises(ValidationError):
        IssueCandidate.model_validate(doc)


def test_issue_candidate_document_pins_evidence_digest() -> None:
    model = IssueCandidateDocument.model_validate(
        {
            "schema_version": "1.0",
            "change_id": "RET-dept-management",
            "batch_id": "20260725-124844",
            "evidence_bundle_digest": "sha256:bundle",
            "candidates": [make_candidate()],
        }
    )
    assert model.evidence_bundle_digest == "sha256:bundle"
    assert len(model.candidates) == 1


def test_issue_analysis_status_completed_parses() -> None:
    model = IssueAnalysisStatus.model_validate(
        {
            "schema_version": "1.0",
            "change_id": "RET-dept-management",
            "batch_id": "20260725-124844",
            "status": "completed",
            "evidence_bundle_digest": "sha256:bundle",
            "candidate_count": 0,
        }
    )
    assert model.status == "completed"
    assert model.candidate_count == 0


def test_issue_analysis_status_failed_parses() -> None:
    model = IssueAnalysisStatus.model_validate(
        {
            "schema_version": "1.0",
            "change_id": "RET-dept-management",
            "batch_id": "20260725-124844",
            "status": "failed",
            "reason": "timeout",
            "evidence_bundle_digest": "sha256:bundle",
            "candidate_count": 0,
            "retryable": True,
        }
    )
    assert model.reason == "timeout"
    assert model.retryable is True


def test_issue_analysis_status_unknown_reason_fails() -> None:
    with pytest.raises(ValidationError):
        IssueAnalysisStatus.model_validate(
            {
                "schema_version": "1.0",
                "change_id": "RET-dept-management",
                "batch_id": "20260725-124844",
                "status": "failed",
                "reason": "magic",
                "evidence_bundle_digest": "sha256:bundle",
                "candidate_count": 0,
            }
        )


def test_issue_reconcile_status_completed_parses() -> None:
    model = IssueReconcileStatus.model_validate(
        {
            "schema_version": "1.0",
            "change_id": "RET-dept-management",
            "batch_id": "20260725-124844",
            "status": "completed",
            "evidence_bundle_digest": "sha256:bundle",
            "candidate_digest": "sha256:candidate",
            "occurrence_count": 1,
        }
    )
    assert model.status == "completed"
    assert model.occurrence_count == 1


def test_issue_occurrence_valid_fixture_parses() -> None:
    model = IssueOccurrence.model_validate(make_occurrence())
    assert model.occurrence_id == "OCC-def456"
    assert model.provisional_assessment.authority == "llm_provisional"
    assert model.analysis.analyzer == "issue-analyzer"


def test_issue_occurrence_without_observations_fails() -> None:
    with pytest.raises(ValidationError):
        IssueOccurrence.model_validate(make_occurrence(observation_ids=[]))


def test_issue_occurrence_rejects_mutable_status_field() -> None:
    with pytest.raises(ValidationError):
        IssueOccurrence.model_validate(make_occurrence(status="detected"))


def test_issue_occurrence_is_frozen() -> None:
    model = IssueOccurrence.model_validate(make_occurrence())
    with pytest.raises(ValidationError):
        model.occurrence_id = "OCC-other"  # type: ignore[misc]


def test_problem_valid_fixture_parses() -> None:
    model = Problem.model_validate(make_problem())
    assert model.status == "detected"
    assert model.version == 1
    assert model.first_seen.occurrence_id == "OCC-def456"


def test_problem_resolved_requires_resolution() -> None:
    with pytest.raises(ValidationError, match="resolution"):
        Problem.model_validate(make_problem(status="resolved", resolution=None))


def test_problem_resolved_with_resolution_parses() -> None:
    model = Problem.model_validate(
        make_problem(
            status="resolved",
            resolution={
                "resolved_at": "2026-07-26T00:00:00Z",
                "change_id": "RET-dept-management",
                "batch_id": "20260726-010000",
                "disposition": "healing/api-apply-summary.json",
                "verification_scope": ["API_DEPT_NEG_001"],
                "evidence_digest": "sha256:verify",
            },
        )
    )
    assert model.status == "resolved"
    assert model.resolution is not None
    assert model.resolution.verification_scope == ["API_DEPT_NEG_001"]


def test_problem_unknown_status_fails() -> None:
    with pytest.raises(ValidationError):
        Problem.model_validate(make_problem(status="closed"))


def test_change_issue_snapshot_retains_batches() -> None:
    model = ChangeIssueSnapshot.model_validate(
        {
            "schema_version": "1.0",
            "change_id": "RET-dept-management",
            "authoritative_batch_id": "20260725-124844",
            "observations": [make_observation()],
            "occurrences": [make_occurrence()],
            "analysis_status": {
                "schema_version": "1.0",
                "change_id": "RET-dept-management",
                "batch_id": "20260725-124844",
                "status": "completed",
                "evidence_bundle_digest": "sha256:bundle",
                "candidate_count": 1,
            },
            "project_sync_status": "completed",
            "batches": ["20260725-120000", "20260725-124844"],
        }
    )
    assert model.authoritative_batch_id == "20260725-124844"
    assert model.batches == ["20260725-120000", "20260725-124844"]
    assert len(model.observations) == 1


def test_problem_projection_wraps_problems() -> None:
    model = ProblemProjection.model_validate(
        {
            "schema_version": "1.0",
            "generated_at": "2026-07-25T05:00:00Z",
            "problems": [make_problem()],
        }
    )
    assert len(model.problems) == 1
    assert model.problems[0].problem_id == "PROB-ghi789"


def test_problem_review_queue_wraps_entries() -> None:
    model = ProblemReviewQueue.model_validate(
        {
            "schema_version": "1.0",
            "entries": [
                {
                    "entry_id": "REV-001",
                    "change_id": "RET-dept-management",
                    "occurrence_id": "OCC-def456",
                    "candidate_id": "CAND-001",
                    "possible_problem_ids": ["PROB-other"],
                    "reason": "semantic_similarity",
                    "created_at": "2026-07-25T05:00:00Z",
                }
            ],
        }
    )
    assert model.entries[0].possible_problem_ids == ["PROB-other"]


def test_nested_issue_models_are_frozen() -> None:
    source = ObservationSource.model_validate(
        {"artifact": "execution/runs/x/api-result.json", "json_pointer": "/cases/0"}
    )
    with pytest.raises(ValidationError):
        source.artifact = "other"  # type: ignore[misc]

    proposed = IssueCandidateProposed.model_validate(
        {
            "title": "title",
            "classification": "product_bug",
            "severity": "high",
            "root_cause_hypothesis": "cause",
        }
    )
    with pytest.raises(ValidationError):
        proposed.title = "other"  # type: ignore[misc]
