"""Fail-closed completion of Improvement reviewer outputs."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from assurance_agent.artifacts.canonical import sha256_bytes
from assurance_agent.artifacts.models.improvement_review import (
    ImprovementAutoReviewAssessment,
    ImprovementReviewSubject,
)
from assurance_agent.exceptions import AaError


class ImprovementReviewerOutputError(AaError):
    """Reviewer output is missing, malformed, or bound to another subject."""


def complete_improvement_reviewer_outputs(
    *,
    subject_path: Path,
    assessment_path: Path,
    summary_path: Path,
    expected_review_id: str,
    expected_improvement_id: str,
    expected_version: int,
    expected_subject_sha256: str,
) -> ImprovementAutoReviewAssessment:
    """Validate identity, schema and subject binding before workspace freeze."""
    try:
        subject_bytes = subject_path.read_bytes()
        subject = ImprovementReviewSubject.model_validate_json(subject_bytes)
        assessment = ImprovementAutoReviewAssessment.model_validate_json(
            assessment_path.read_text(encoding="utf-8")
        )
        summary_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValidationError, ValueError) as exc:
        raise ImprovementReviewerOutputError(f"invalid improvement reviewer output: {exc}") from exc
    if sha256_bytes(subject_bytes) != expected_subject_sha256:
        raise ImprovementReviewerOutputError("review subject bytes do not match expected digest")
    if subject.improvement_id != expected_improvement_id:
        raise ImprovementReviewerOutputError("review subject improvement_id mismatch")
    expected = (
        expected_review_id,
        expected_improvement_id,
        expected_version,
        expected_subject_sha256,
    )
    actual = (
        assessment.review_id,
        assessment.improvement_id,
        assessment.expected_improvement_version,
        assessment.subject_sha256,
    )
    if actual != expected:
        raise ImprovementReviewerOutputError("assessment identity or subject binding mismatch")
    return assessment


__all__ = ["ImprovementReviewerOutputError", "complete_improvement_reviewer_outputs"]
