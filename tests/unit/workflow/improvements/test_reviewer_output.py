"""Fail-closed completion for the read-only Improvement reviewer."""

from __future__ import annotations

import json

import pytest

from assurance_agent.artifacts.models.improvement_review import ImprovementAutoReviewAssessment
from assurance_agent.workflow.improvements.review_subject import build_review_subject
from assurance_agent.workflow.improvements.reviewer_output import (
    ImprovementReviewerOutputError,
    complete_improvement_reviewer_outputs,
)
from tests.unit.workflow.improvements.test_reconcile_v3 import _candidate, _context


def _files(tmp_path):
    _subject, digest, data = build_review_subject(_candidate(), _context(), improvement_id="IMP-1")
    subject_path = tmp_path / "subject.json"
    assessment_path = tmp_path / "assessment.json"
    summary_path = tmp_path / "summary.md"
    subject_path.write_bytes(data)
    assessment = ImprovementAutoReviewAssessment(
        review_id="REV-1",
        improvement_id="IMP-1",
        expected_improvement_version=1,
        subject_sha256=digest,
        decision="pass",
        evidence_traceability="complete",
        scope_readiness="ready",
        verification_readiness="ready",
        delivery_safety="ready",
        human_review_required=False,
    )
    assessment_path.write_text(assessment.model_dump_json(), encoding="utf-8")
    summary_path.write_text("# Review\n", encoding="utf-8")
    return subject_path, assessment_path, summary_path, digest


def _authoring_payload() -> dict[str, object]:
    return {
        "schema_version": "1",
        "review_type": "improvement",
        "decision": "pass",
        "findings": [],
        "evidence_traceability": "complete",
        "scope_readiness": "ready",
        "verification_readiness": "ready",
        "delivery_safety": "ready",
        "human_review_required": False,
    }


def _complete(tmp_path):
    subject, assessment, summary, digest = _files(tmp_path)
    assessment.write_text(json.dumps(_authoring_payload()), encoding="utf-8")
    return complete_improvement_reviewer_outputs(
        subject_path=subject,
        assessment_path=assessment,
        summary_path=summary,
        expected_review_id="REV-1",
        expected_improvement_id="IMP-1",
        expected_version=1,
        expected_subject_sha256=digest,
    )


def test_completion_accepts_semantic_assessment_and_binds_identity(tmp_path) -> None:
    assert _complete(tmp_path).decision == "pass"


def test_completion_injects_runtime_owned_identity_and_rewrites_canonical_json(tmp_path) -> None:
    subject, assessment, summary, digest = _files(tmp_path)
    assessment.write_text(json.dumps(_authoring_payload()), encoding="utf-8")

    completed = complete_improvement_reviewer_outputs(
        subject_path=subject,
        assessment_path=assessment,
        summary_path=summary,
        expected_review_id="REV-1",
        expected_improvement_id="IMP-1",
        expected_version=1,
        expected_subject_sha256=digest,
    )

    persisted = ImprovementAutoReviewAssessment.model_validate_json(assessment.read_bytes())
    assert persisted == completed
    assert persisted.review_id == "REV-1"
    assert persisted.improvement_id == "IMP-1"
    assert persisted.expected_improvement_version == 1
    assert persisted.subject_sha256 == digest


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("review_id", "REV-OTHER"),
        ("improvement_id", "IMP-OTHER"),
        ("expected_improvement_version", 999),
        ("subject_sha256", "sha256:" + "0" * 64),
        ("auto_eligible", True),
    ],
)
def test_completion_rejects_agent_authored_runtime_or_authority_field(
    tmp_path, field: str, value: object
) -> None:
    subject, assessment, summary, digest = _files(tmp_path)
    payload = _authoring_payload()
    payload[field] = value
    assessment.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ImprovementReviewerOutputError, match="invalid improvement reviewer output"):
        complete_improvement_reviewer_outputs(
            subject_path=subject,
            assessment_path=assessment,
            summary_path=summary,
            expected_review_id="REV-1",
            expected_improvement_id="IMP-1",
            expected_version=1,
            expected_subject_sha256=digest,
        )


def test_completion_requires_human_summary_artifact(tmp_path) -> None:
    subject, assessment, summary, digest = _files(tmp_path)
    summary.unlink()

    with pytest.raises(ImprovementReviewerOutputError, match="invalid improvement reviewer output"):
        complete_improvement_reviewer_outputs(
            subject_path=subject,
            assessment_path=assessment,
            summary_path=summary,
            expected_review_id="REV-1",
            expected_improvement_id="IMP-1",
            expected_version=1,
            expected_subject_sha256=digest,
        )
