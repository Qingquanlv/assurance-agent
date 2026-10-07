from __future__ import annotations

from typing import get_args

import pytest
from pydantic import BaseModel, ValidationError

from graph_engine.attempts import (
    AttemptResolution,
    CommittedTaskResult,
    IndeterminateTaskResult,
    PendingTaskResult,
    PermanentTaskFailure,
    ReceiptRef,
    RejectedTaskResult,
    SystemReference,
)


class RunOutput(BaseModel):
    status: str


def _receipt(*, receipt_id: str = "receipt-1") -> ReceiptRef:
    return ReceiptRef(receipt_id=receipt_id, receipt_digest="b" * 64)


def _system_reference(*, reference_id: str = "wake-1") -> SystemReference:
    return SystemReference(reference_id=reference_id)


def test_attempt_resolution_is_the_closed_five_variant_union() -> None:
    assert set(get_args(AttemptResolution)) == {
        CommittedTaskResult,
        RejectedTaskResult,
        PermanentTaskFailure,
        PendingTaskResult,
        IndeterminateTaskResult,
    }


def test_committed_task_result_carries_typed_output_and_receipt() -> None:
    result = CommittedTaskResult(
        output=RunOutput(status="ok"),
        receipt=_receipt(),
    )
    assert result.output == RunOutput(status="ok")
    assert result.receipt == _receipt()


def test_pending_and_indeterminate_require_system_reference_without_output() -> None:
    reference = _system_reference()
    pending = PendingTaskResult(wakeup=reference)
    indeterminate = IndeterminateTaskResult(reconciliation=reference)
    assert pending.wakeup == reference
    assert indeterminate.reconciliation == reference
    assert getattr(pending, "output", None) is None
    assert getattr(indeterminate, "output", None) is None

    with pytest.raises(ValidationError):
        PendingTaskResult()
    with pytest.raises(ValidationError):
        IndeterminateTaskResult()
    with pytest.raises(ValidationError):
        PendingTaskResult(wakeup=reference, output=RunOutput(status="leaked"))
    with pytest.raises(ValidationError):
        IndeterminateTaskResult(reconciliation=reference, output=RunOutput(status="leaked"))


def test_rejected_and_permanent_failures_cannot_claim_promotion() -> None:
    receipt = _receipt()
    rejected = RejectedTaskResult(reason="validator rejected")
    permanent = PermanentTaskFailure(kind="internal", message="handler crashed")
    assert rejected.writes_promoted is False
    assert permanent.writes_promoted is False

    with pytest.raises(ValidationError):
        RejectedTaskResult(reason="validator rejected", writes_promoted=True)
    with pytest.raises(ValidationError):
        RejectedTaskResult(reason="validator rejected", promotion_receipt=receipt)
    with pytest.raises(ValidationError):
        PermanentTaskFailure(kind="internal", message="handler crashed", writes_promoted=True)
    with pytest.raises(ValidationError):
        PermanentTaskFailure(kind="internal", message="handler crashed", promotion_receipt=receipt)
