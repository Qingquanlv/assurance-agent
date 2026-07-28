"""Contracts for explicit Retro batches and always-finalized run artifacts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.retro_batch import (
    RetroBatchScope,
    RetroInvocationResult,
    RetroPipelineFailure,
    RetroPipelineFailureDocument,
    RetroRunStatus,
)


def _member(
    change_id: str,
    *,
    execution_status: str = "completed",
    evidence_availability: str = "complete",
) -> dict[str, str]:
    return {
        "change_id": change_id,
        "execution_status": execution_status,
        "evidence_availability": evidence_availability,
    }


def _failure(failure_id: str, *, stage: str = "collect") -> RetroPipelineFailure:
    return RetroPipelineFailure.model_validate(
        {
            "schema_version": "1",
            "failure_id": failure_id,
            "retro_id": "RETRO-BATCH-1",
            "batch_id": "BATCH-1",
            "stage": stage,
            "node_id": "collect-retro-evidence",
            "error_kind": "forbidden_write",
            "message_fingerprint": "sha256:" + "a" * 64,
            "runtime_event_id": "EVT-1",
            "occurred_at": datetime(2026, 7, 28, tzinfo=UTC),
        }
    )


def test_batch_scope_requires_canonical_unique_members() -> None:
    valid = RetroBatchScope.model_validate(
        {
            "schema_version": "1",
            "batch_id": "BATCH-1",
            "status": "complete",
            "members": [_member("CH-1"), _member("CH-2")],
        }
    )
    assert tuple(item.change_id for item in valid.members) == ("CH-1", "CH-2")

    for members in (
        [_member("CH-2"), _member("CH-1")],
        [_member("CH-1"), _member("CH-1")],
    ):
        with pytest.raises(ValidationError, match="canonical|unique"):
            RetroBatchScope.model_validate(
                {
                    "schema_version": "1",
                    "batch_id": "BATCH-1",
                    "status": "complete",
                    "members": members,
                }
            )


def test_complete_batch_rejects_partial_or_absent_evidence() -> None:
    with pytest.raises(ValidationError, match="complete.*evidence"):
        RetroBatchScope.model_validate(
            {
                "schema_version": "1",
                "batch_id": "BATCH-1",
                "status": "complete",
                "members": [
                    _member(
                        "CH-1",
                        execution_status="hard_timeout",
                        evidence_availability="partial",
                    )
                ],
            }
        )


def test_pipeline_failure_document_is_sorted_and_unique() -> None:
    first = _failure("FAIL-1", stage="collect")
    second = _failure("FAIL-2", stage="propose")
    document = RetroPipelineFailureDocument(
        schema_version="1",
        retro_id="RETRO-BATCH-1",
        failures=(first, second),
    )
    assert tuple(item.failure_id for item in document.failures) == ("FAIL-1", "FAIL-2")

    with pytest.raises(ValidationError, match="sorted|unique"):
        RetroPipelineFailureDocument(
            schema_version="1",
            retro_id="RETRO-BATCH-1",
            failures=(second, first),
        )
    with pytest.raises(ValidationError, match="sorted|unique"):
        RetroPipelineFailureDocument(
            schema_version="1",
            retro_id="RETRO-BATCH-1",
            failures=(first, first),
        )


def test_persisted_status_cannot_claim_technical_failure() -> None:
    with pytest.raises(ValidationError):
        RetroRunStatus.model_validate(
            {
                "schema_version": "1",
                "retro_id": "RETRO-BATCH-1",
                "batch_id": "BATCH-1",
                "result": "technical_failure",
                "improvement_ids": [],
                "outbox_id": None,
                "failure_ids": [],
            }
        )

    invocation = RetroInvocationResult(status=None, result="technical_failure")
    assert invocation.status is None


def test_pipeline_failure_rejects_raw_exception_payload() -> None:
    payload = _failure("FAIL-1").model_dump(mode="json")
    payload["traceback"] = "secret path and stack"
    with pytest.raises(ValidationError, match="traceback"):
        RetroPipelineFailure.model_validate(payload)
