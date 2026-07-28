"""Strict Auto Review artifact invariants."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.improvement_review import (
    ImprovementAutoReviewBatchSummary,
    ImprovementAutoReviewStatus,
)


def test_batch_summary_counts_cover_each_child_exactly_once() -> None:
    summary = ImprovementAutoReviewBatchSummary(
        retro_id="RETRO-1",
        review_ids=("R-1", "R-2", "R-3", "R-4"),
        approved=1,
        escalated=1,
        errors=1,
        stale=1,
    )
    assert sum((summary.approved, summary.escalated, summary.errors, summary.stale)) == 4


def test_batch_summary_rejects_missing_semantic_result() -> None:
    with pytest.raises(ValidationError, match="counts"):
        ImprovementAutoReviewBatchSummary(
            retro_id="RETRO-1",
            review_ids=("R-1",),
            approved=0,
            escalated=0,
            errors=0,
            stale=0,
        )


def test_review_status_has_no_parallel_idempotent_result() -> None:
    with pytest.raises(ValidationError):
        ImprovementAutoReviewStatus.model_validate(
            {
                "review_id": "R-1",
                "improvement_id": "IMP-1",
                "result": "idempotent",
            }
        )
