from __future__ import annotations

import pytest
from pydantic import ValidationError

from graph_engine.attempts.resolutions import ReceiptRef

from assurance_intake.contracts.workflow import EvidenceArtifactRefV1, ReviewedCaseV1
from assurance_product.graphs.execute import adapt_execute_tail_input, adapt_public_execute_tail
from assurance_product.graphs.loop_state import advance_coverage, can_reenter_case, clear_current_cycle
from assurance_product.graphs.state import make_assessment_trigger, merge_assessment_trigger
from assurance_product.graphs.tail_contracts import ExecuteTailResultV1
from assurance_product.models import BusinessBudgetsV1
from assurance_quality.contracts.assessment import InspectionOutcomeV1

from tests.product.test_product_input import valid_product_input

_SHA = "a" * 64


def _ref(path: str, digest: str = _SHA) -> EvidenceArtifactRefV1:
    return EvidenceArtifactRefV1(path=path, digest=digest)


def _receipt(name: str = "inspect-0") -> ReceiptRef:
    return ReceiptRef(receipt_id=name, receipt_digest=_SHA)


def _reviewed_case(epoch: int = 0) -> ReviewedCaseV1:
    return ReviewedCaseV1(
        change_id="CH-1",
        coverage_epoch=epoch,
        preparation_refs=(_ref("qa/changes/CH-1/preparation/context.json"),),
        case_refs=(_ref("qa/changes/CH-1/cases/system/case.yaml"),),
        review_ref=_ref("qa/changes/CH-1/review/case-review.json"),
    )


def _inspection(epoch: int = 0, receipt: ReceiptRef | None = None) -> InspectionOutcomeV1:
    reviewed = _reviewed_case(epoch)
    return InspectionOutcomeV1(
        change_id=reviewed.change_id,
        coverage_epoch=epoch,
        batch_id=f"batch-{epoch}",
        disposition="coverage_insufficient",
        inspection_receipt=receipt or _receipt(),
        reviewed_case=reviewed,
        mapping_ref=_ref(f"qa/changes/CH-1/generation/epochs/{epoch}/mapping.json"),
        assessment_refs=(_ref(f"qa/changes/CH-1/inspect/epochs/{epoch}/gaps.json"),),
        reason_codes=("uncovered_required_case",),
        coverage_state="repair_required",
    )


def _budgets(coverage_rounds: int) -> BusinessBudgetsV1:
    return BusinessBudgetsV1(
        review_rounds=2,
        coverage_rounds=coverage_rounds,
        healing_rounds=1,
        execution_retries=1,
    )


def test_coverage_budget_counts_additional_iterations() -> None:
    budget = _budgets(0)
    assert not can_reenter_case(coverage_epoch=0, budgets=budget)
    positive = budget.model_copy(update={"coverage_rounds": 1})
    assert can_reenter_case(coverage_epoch=0, budgets=positive)
    assert not can_reenter_case(coverage_epoch=1, budgets=positive)


def test_advance_coverage_switches_epoch_without_changing_review_budget() -> None:
    budgets = _budgets(2)
    tail = ExecuteTailResultV1(status="coverage_insufficient", inspection=_inspection())
    state = {
        "coverage_epoch": 0,
        "healing_rounds_used": 1,
        "budgets": budgets.model_dump(mode="json"),
        "tail_result": tail.model_dump(mode="json"),
        "generation_result": {"stale": True},
        "execution_result": {"stale": True},
        "report_refs": [{"path": "stale", "digest": _SHA}],
    }

    update = advance_coverage(state)  # type: ignore[arg-type]
    merged = {**state, **update}

    assert merged["coverage_epoch"] == 1
    assert merged["healing_rounds_used"] == 0
    assert merged["budgets"] == budgets.model_dump(mode="json")
    assert merged["generation_result"] == {}
    assert merged["execution_result"] == {}
    assert merged["assessment_inputs"] == {}
    assert merged["fact_baseline_ref"] == {}
    assert merged["report_refs"] == []
    assert update["last_coverage_source_receipt"] == _receipt().model_dump(mode="json")


def test_same_inspection_receipt_cannot_advance_coverage_twice() -> None:
    receipt = _receipt("inspect-replayed")
    inspection = _inspection(receipt=receipt)
    tail = ExecuteTailResultV1(status="coverage_insufficient", inspection=inspection)
    state = {
        "coverage_epoch": 0,
        "budgets": _budgets(2).model_dump(mode="json"),
        "tail_result": tail.model_dump(mode="json"),
        "last_coverage_source_receipt": receipt.model_dump(mode="json"),
    }
    with pytest.raises(ValueError, match="cannot advance coverage twice"):
        advance_coverage(state)  # type: ignore[arg-type]


def test_current_cycle_clear_preserves_epoch_scoped_reducer_history() -> None:
    update = clear_current_cycle(next_epoch=2)
    assert update["coverage_epoch"] == 2
    assert "generation_results" not in update
    assert "generation_receipts" not in update
    assert update["report_receipt"] is None
    assert update["report_outcome"] == {}
    assert update["assessment_trigger"] is None
    previous = make_assessment_trigger(
        source="quality",
        coverage_state="satisfied",
        rounds={"rounds_used": 0, "rounds_budget": 1},
        evidence=(),
    )
    assert merge_assessment_trigger(previous, update["assessment_trigger"]) == {}


def test_tail_result_status_and_evidence_round_trip() -> None:
    result = ExecuteTailResultV1(status="coverage_insufficient", inspection=_inspection())
    assert ExecuteTailResultV1.model_validate(result.model_dump(mode="json")) == result

    with pytest.raises(ValidationError, match="committed Report"):
        ExecuteTailResultV1(status="reported", inspection=None)

    blocked = ExecuteTailResultV1(
        status="blocked",
        inspection=_inspection().model_copy(update={"disposition": "blocked", "coverage_state": None}),
        reason="inspection policy blocked progression",
    )
    assert blocked.inspection is not None

    for status in ("repairable_execution_failure", "needs_human"):
        inspection = _inspection().model_copy(update={"disposition": status, "coverage_state": None})
        unresolved = ExecuteTailResultV1(status=status, inspection=inspection)  # type: ignore[arg-type]
        assert ExecuteTailResultV1.model_validate(unresolved.model_dump(mode="json")) == unresolved


def test_public_execute_adapter_initializes_standalone_tail_from_artifacts() -> None:
    artifact = {"path": "qa/changes/CH-DEMO-001/cases/reviewed-case.json", "digest": _SHA}
    payload = valid_product_input(
        selected_test_families=("api",),
        capability_leafs=("auth.session",),
        artifacts=(artifact,),
    )
    adapted = adapt_public_execute_tail(payload)  # type: ignore[arg-type]
    assert adapted["coverage_epoch"] == 0
    assert adapted["healing_rounds_used"] == 0
    assert adapted["reviewed_case"] is None
    assert adapted["source_artifacts"] == [artifact]


def test_full_tail_adapter_preserves_case_scope_and_current_epoch() -> None:
    reviewed = ReviewedCaseV1(
        change_id="CH-DEMO-001",
        coverage_epoch=1,
        preparation_refs=(_ref("qa/changes/CH-DEMO-001/preparation/context.json"),),
        case_refs=(_ref("qa/changes/CH-DEMO-001/cases/system/case.yaml"),),
        review_ref=_ref("qa/changes/CH-DEMO-001/review/case-review.json"),
    )
    payload = valid_product_input(
        selected_test_families=("api",),
        case_delta_paths=("qa/changes/CH-DEMO-001/cases/system/case.yaml",),
        capability_leafs=("auth.session",),
    )
    state = {**payload, "coverage_epoch": 1, "reviewed_case": reviewed.model_dump(mode="json")}
    adapted = adapt_execute_tail_input(state, standalone=False)  # type: ignore[arg-type]
    assert adapted["coverage_epoch"] == 1
    assert adapted["reviewed_case"] == reviewed.model_dump(mode="json")
    assert "case_delta_paths" not in adapted
