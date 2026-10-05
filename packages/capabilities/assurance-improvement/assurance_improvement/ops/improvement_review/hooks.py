"""Seal an improvement review against the locked subject and projection."""

from __future__ import annotations

from agent_runtime_contracts.ops import FinalizeContext, InputError, OutputError

from assurance_improvement.contracts.agent import (
    ImprovementReviewResultV1,
    ImprovementSkillInputV1,
    ReviewPublishedV1,
)
from assurance_improvement.contracts.delivery import artifact_digest, digest_hex
from assurance_improvement.contracts.improvements import ImprovementProjection
from assurance_improvement.contracts.review import ImprovementReviewSubject


def after(
    ctx: FinalizeContext, business: ImprovementSkillInputV1, result: ImprovementReviewResultV1
) -> ReviewPublishedV1:
    subject_raw = ctx.prepared.get("subject")
    projection_raw = ctx.prepared.get("projection")
    if subject_raw is None or projection_raw is None:
        raise InputError("review finalize requires the authenticated subject and projection")
    subject = ImprovementReviewSubject.model_validate(subject_raw)
    projection = ImprovementProjection.model_validate(projection_raw)
    if subject.improvement_id != business.improvement_id:
        raise OutputError("review subject improvement_id does not match")
    if projection.improvement_id != business.improvement_id:
        raise OutputError("review projection improvement_id does not match")
    if projection.version != business.expected_improvement_version:
        raise OutputError("review version does not match the current improvement")
    if digest_hex(artifact_digest(subject)) != business.subject_digest:
        raise OutputError("review subject digest does not match")
    if result.decision == "pass" and result.evidence_traceability != "complete":
        raise OutputError("pass review requires complete evidence traceability")
    return ReviewPublishedV1(
        decision=result.decision,
        human_review_required=result.human_review_required,
        result=result,
    )
