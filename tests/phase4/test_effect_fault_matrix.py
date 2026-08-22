from __future__ import annotations

import pytest

from tests.phase4.effect_faults import (
    EFFECT_KINDS,
    FAULT_CUTS,
    drive_effect_cut,
)

_KINDS = (
    "assurance.healing.effect.allocation.v2",
    "assurance.healing.effect.proposal-approved.v1",
    "assurance.healing.effect.heal-apply.v2",
    "assurance.improvement.effect.delivery.v1",
    "assurance.improvement.effect.promotion.v1",
    "assurance.improvement.effect.archive.v1",
)
_CUTS = (
    "before_mutation",
    "after_mutation",
    "before_receipt",
    "after_receipt",
    "reconcile_error",
)


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize("cut", _CUTS)
async def test_effect_crash_cuts_are_at_most_once_and_typed(kind: str, cut: str) -> None:
    assert kind in EFFECT_KINDS
    assert cut in FAULT_CUTS
    result = await drive_effect_cut(kind, cut)
    assert result.external_mutation_count <= 1
    assert result.idempotency_key == result.expected_key
    assert result.status in {"applied", "pending", "not_applied", "indeterminate", "permanently_failed"}
    if result.status == "applied":
        assert result.receipt == result.expected_receipt
        assert result.external_mutation_count == 1
    else:
        assert result.status in {"pending", "not_applied", "indeterminate"}
        if cut == "before_mutation":
            assert result.external_mutation_count == 0
    if cut == "before_mutation":
        assert result.status in {"not_applied", "pending"}
        assert result.external_mutation_count == 0
        assert result.apply_returned_applied is False
    elif cut == "after_mutation":
        assert result.status == "pending"
        assert result.external_mutation_count == 1
        assert result.apply_returned_applied is False
    elif cut == "before_receipt":
        assert result.status == "applied"
        assert result.receipt == result.expected_receipt
        assert result.apply_returned_applied is False
    elif cut == "after_receipt":
        assert result.status == "applied"
        assert result.receipt == result.expected_receipt
        assert result.apply_returned_applied is True
        assert result.apply_receipt == result.expected_receipt
    elif cut == "reconcile_error":
        assert result.status in {"applied", "pending", "not_applied", "indeterminate"}
