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


def _complete(tmp_path):
    subject, assessment, summary, digest = _files(tmp_path)
    return complete_improvement_reviewer_outputs(
        subject_path=subject,
        assessment_path=assessment,
        summary_path=summary,
        expected_review_id="REV-1",
        expected_improvement_id="IMP-1",
        expected_version=1,
        expected_subject_sha256=digest,
    )


def test_completion_accepts_exact_identity_and_subject_binding(tmp_path) -> None:
    assert _complete(tmp_path).decision == "pass"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("review_id", "REV-OTHER"),
        ("improvement_id", "IMP-OTHER"),
        ("expected_improvement_version", 2),
        ("subject_sha256", "sha256:" + "0" * 64),
    ],
)
def test_completion_rejects_identity_or_binding_drift(tmp_path, field: str, value: object) -> None:
    subject, assessment, summary, digest = _files(tmp_path)
    payload = json.loads(assessment.read_text(encoding="utf-8"))
    payload[field] = value
    assessment.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ImprovementReviewerOutputError, match="binding mismatch"):
        complete_improvement_reviewer_outputs(
            subject_path=subject,
            assessment_path=assessment,
            summary_path=summary,
            expected_review_id="REV-1",
            expected_improvement_id="IMP-1",
            expected_version=1,
            expected_subject_sha256=digest,
        )


def test_completion_rejects_agent_authored_authority_field(tmp_path) -> None:
    subject, assessment, summary, digest = _files(tmp_path)
    payload = json.loads(assessment.read_text(encoding="utf-8"))
    payload["auto_eligible"] = True
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
