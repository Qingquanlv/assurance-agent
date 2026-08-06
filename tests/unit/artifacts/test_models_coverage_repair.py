"""coverage-repair/* artifact models (brief, status, safety, apply-summary, baseline)."""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from assurance_agent.artifacts.models.coverage_repair import (
    CoverageRepairApplySummary,
    CoverageRepairBaseline,
    CoverageRepairBrief,
    CoverageRepairSafetyCheck,
    CoverageRepairStatus,
    MetricsSufficiencyVerdict,
    RepairItem,
)
from assurance_agent.evidence.metrics_sufficiency import (
    MetricsSufficiencyVerdict as EvidenceMetricsSufficiencyVerdict,
)


def _repair_item(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": "uncovered_required_case",
        "locator": {"case_id": "TC_API_001"},
        "metric": "constraint_coverage",
        "hint": "add a property asserting uniqueness",
    }
    payload.update(overrides)
    return payload


def _eligible_brief(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1",
        "change_id": "CH-REPAIR-001",
        "batch_id": "20260806-120000",
        "probe_verdict": "needs_human",
        "eligible": True,
        "shortboards": ({"code": "below_floor", "metric": "constraint_coverage", "detail": "0/2"},),
        "repair_items": (_repair_item(),),
        "deferred_to_meetup": (),
    }
    payload.update(overrides)
    return payload


def _status(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1",
        "change_id": "CH-REPAIR-001",
        "status": "in_progress",
        "attempts_used": 0,
    }
    payload.update(overrides)
    return payload


def _apply_summary(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1",
        "change_id": "CH-REPAIR-001",
        "attempt": 1,
        "attempt_token": "abcdef0123456789",
        "applied": True,
        "files_modified": ("tests/api/test_dept.py",),
        "addressed_items": ("case_id=TC_API_001",),
    }
    payload.update(overrides)
    return payload


def _baseline(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1",
        "change_id": "CH-REPAIR-001",
        "attempt": 1,
        "attempt_token": "abcdef0123456789",
        "test_tree_sha256": "aa" * 32,
        "test_files_sha256": {"tests/api/test_dept.py": "bb" * 32},
        "product_tree_sha256": "cc" * 32,
        "product_files_sha256": {},
        "declaration_tree_sha256": "dd" * 32,
        "declaration_files_sha256": {},
    }
    payload.update(overrides)
    return payload


def _safety_check(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1",
        "change_id": "CH-REPAIR-001",
        "attempt": 1,
        "passed": True,
        "needs_review": False,
    }
    payload.update(overrides)
    return payload


def test_eligible_brief_with_batch_and_items_round_trips_json() -> None:
    brief = CoverageRepairBrief.model_validate(_eligible_brief())
    restored = CoverageRepairBrief.model_validate_json(brief.model_dump_json())
    assert restored == brief
    assert restored.eligible is True
    assert restored.batch_id == "20260806-120000"
    assert len(restored.repair_items) == 1


def test_eligible_brief_requires_batch_id() -> None:
    with pytest.raises(ValidationError, match="batch_id"):
        CoverageRepairBrief.model_validate(_eligible_brief(batch_id=None))


def test_eligible_brief_requires_repair_items() -> None:
    with pytest.raises(ValidationError, match="repair item"):
        CoverageRepairBrief.model_validate(_eligible_brief(repair_items=()))


def test_ineligible_brief_without_batch_or_items_is_valid() -> None:
    brief = CoverageRepairBrief.model_validate(
        _eligible_brief(
            eligible=False,
            batch_id=None,
            repair_items=(),
            shortboards=(),
            probe_verdict="pass",
        )
    )
    assert brief.eligible is False
    assert brief.batch_id is None
    assert brief.repair_items == ()


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (CoverageRepairBrief, _eligible_brief()),
        (CoverageRepairStatus, _status()),
        (CoverageRepairApplySummary, _apply_summary()),
        (CoverageRepairBaseline, _baseline()),
        (CoverageRepairSafetyCheck, _safety_check()),
    ],
)
def test_extra_forbid_rejects_unknown_key_on_all_five_documents(
    model: type, payload: dict[str, object]
) -> None:
    dirty = {**payload, "unexpected_field": "oops"}
    with pytest.raises(ValidationError):
        model.model_validate(dirty)


def test_repair_item_rejects_unmapped_test_cluster() -> None:
    with pytest.raises(ValidationError):
        RepairItem.model_validate(
            _repair_item(kind="unmapped_test_cluster", locator={"cluster_key": "tests/api/z.py"})
        )


def test_status_rejects_negative_attempts_used() -> None:
    with pytest.raises(ValidationError):
        CoverageRepairStatus.model_validate(_status(attempts_used=-1))


def test_apply_summary_requires_applied_change_id_attempt_and_token() -> None:
    for key in ("applied", "change_id", "attempt", "attempt_token"):
        doc = _apply_summary()
        del doc[key]
        with pytest.raises(ValidationError):
            CoverageRepairApplySummary.model_validate(doc)


def test_apply_summary_noop_with_required_fields_is_valid() -> None:
    summary = CoverageRepairApplySummary.model_validate(
        _apply_summary(applied=False, files_modified=(), addressed_items=())
    )
    assert summary.applied is False
    assert summary.attempt_token == "abcdef0123456789"


def test_apply_summary_rejects_attempt_zero_and_empty_token() -> None:
    with pytest.raises(ValidationError):
        CoverageRepairApplySummary.model_validate(_apply_summary(attempt=0))
    with pytest.raises(ValidationError):
        CoverageRepairApplySummary.model_validate(_apply_summary(attempt_token=""))


def test_baseline_rejects_attempt_zero_empty_token_and_missing_tree_fields() -> None:
    with pytest.raises(ValidationError):
        CoverageRepairBaseline.model_validate(_baseline(attempt=0))
    with pytest.raises(ValidationError):
        CoverageRepairBaseline.model_validate(_baseline(attempt_token=""))
    for key in (
        "test_tree_sha256",
        "test_files_sha256",
        "product_tree_sha256",
        "product_files_sha256",
        "declaration_tree_sha256",
        "declaration_files_sha256",
    ):
        doc = _baseline()
        del doc[key]
        with pytest.raises(ValidationError):
            CoverageRepairBaseline.model_validate(doc)


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (CoverageRepairBrief, _eligible_brief()),
        (CoverageRepairStatus, _status()),
        (CoverageRepairApplySummary, _apply_summary()),
        (CoverageRepairBaseline, _baseline()),
        (CoverageRepairSafetyCheck, _safety_check()),
    ],
)
def test_all_five_models_are_frozen(model: type, payload: dict[str, object]) -> None:
    instance = model.model_validate(payload)
    with pytest.raises(ValidationError):
        instance.change_id = "CH-OTHER"  # type: ignore[misc]


def test_local_metrics_sufficiency_verdict_matches_evidence_layer() -> None:
    assert get_args(MetricsSufficiencyVerdict) == get_args(EvidenceMetricsSufficiencyVerdict)
