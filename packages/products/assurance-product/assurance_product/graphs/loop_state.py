"""Pure coverage-loop state transitions owned by Product."""

from __future__ import annotations

from collections.abc import Mapping

from assurance_product.graphs.state import ProductState
from assurance_product.graphs.tail_contracts import ExecuteTailResultV1
from assurance_product.models import BusinessBudgetsV1


def can_reenter_case(*, coverage_epoch: int, budgets: BusinessBudgetsV1) -> bool:
    if coverage_epoch < 0:
        raise ValueError("coverage_epoch must be non-negative")
    return coverage_epoch < budgets.coverage_rounds


def clear_current_cycle(*, next_epoch: int) -> dict[str, object]:
    if next_epoch < 0:
        raise ValueError("next_epoch must be non-negative")
    return {
        "coverage_epoch": next_epoch,
        "healing_rounds_used": 0,
        "generation_result": {},
        "execution_result": {},
        "assessment_inputs": {},
        "fact_baseline_ref": {},
        "inspection_outcome": {},
        "tail_result": {},
        "report_refs": [],
        "report_receipt": None,
        "assessment_trigger": None,
        "current_trigger": None,
        "coverage_state": "",
        "classification": "",
        "fix_eligible": False,
        "coverage_decision": "",
        "human_action": "",
        "feature_action": "",
        "rounds_used": 0,
    }


def advance_coverage(state: ProductState) -> dict[str, object]:
    epoch = state.get("coverage_epoch", 0)
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise TypeError("coverage_epoch must be a non-negative int")
    budgets = BusinessBudgetsV1.model_validate(state.get("budgets"))
    result = ExecuteTailResultV1.model_validate(state.get("tail_result"))
    if result.status != "coverage_insufficient" or result.inspection is None:
        raise ValueError("coverage advance requires a coverage_insufficient tail result")
    if result.inspection.coverage_epoch != epoch:
        raise ValueError("coverage advance source epoch does not match current state")
    source = result.inspection.inspection_receipt.model_dump(mode="json")
    previous = state.get("last_coverage_source_receipt")
    if isinstance(previous, Mapping) and dict(previous) == source:
        raise ValueError("the same Inspect receipt cannot advance coverage twice")
    if not can_reenter_case(coverage_epoch=epoch, budgets=budgets):
        raise ValueError("coverage retry budget is exhausted")
    return {
        **clear_current_cycle(next_epoch=epoch + 1),
        "last_coverage_source_receipt": source,
    }


__all__ = ["advance_coverage", "can_reenter_case", "clear_current_cycle"]
