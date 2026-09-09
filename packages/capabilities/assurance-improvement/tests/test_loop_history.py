from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from assurance_improvement.contracts.retro import (
    LoopRoundEvidenceEntry,
    WorkflowEvidenceEntry,
)

SHA = "a" * 64


def _entry(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "entry_kind": "loop_round",
        "evidence_id": "loop-1",
        "change_id": "CH-1",
        "coverage_epoch": 1,
        "loop_kind": "case_review",
        "family": None,
        "round_index": 0,
        "outcome": "pass",
        "source_refs": [
            {
                "path": "qa/changes/CH-1/cases/reviews/epochs/1/rounds/0.json",
                "digest": SHA,
            }
        ],
    }
    payload.update(updates)
    return payload


def test_loop_round_is_a_discriminated_workflow_evidence_entry() -> None:
    parsed = TypeAdapter(WorkflowEvidenceEntry).validate_python(_entry())

    assert isinstance(parsed, LoopRoundEvidenceEntry)
    assert parsed.coverage_epoch == 1
    assert parsed.round_index == 0


@pytest.mark.parametrize(
    ("loop_kind", "family", "valid"),
    [
        ("plan_review", "api", True),
        ("case_review", None, True),
        ("coverage", None, True),
        ("implementation_repair", None, True),
        ("plan_review", None, False),
        ("case_review", "api", False),
        ("implementation_repair", "e2e", False),
    ],
)
def test_loop_round_family_is_present_only_for_generation_loops(
    loop_kind: str, family: str | None, valid: bool
) -> None:
    if valid:
        LoopRoundEvidenceEntry.model_validate(_entry(loop_kind=loop_kind, family=family))
        return
    with pytest.raises(ValidationError, match="family is required only"):
        LoopRoundEvidenceEntry.model_validate(_entry(loop_kind=loop_kind, family=family))


def test_legacy_loop_history_without_round_identity_is_not_coerced_to_zero() -> None:
    legacy = _entry()
    del legacy["coverage_epoch"]
    del legacy["round_index"]

    with pytest.raises(ValidationError):
        LoopRoundEvidenceEntry.model_validate(legacy)
