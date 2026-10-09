"""Case flow routes: every public outcome, both failures, and budget exhaustion."""

from __future__ import annotations

from typing import Any

import pytest

from graph_engine.attempts.models.contracts import TaskAttemptContract
from graph_engine.attempts.models.resolutions import ReceiptRef, RejectedTaskResult
from graph_engine.testing import GraphHarness, committed

from assurance_intake.feature import AGENT_JOB_CONTRACTS, TASK_ATTEMPT_CONTRACTS
from assurance_intake.graphs.factory import build_intake_graphs as _build_intake_graphs

from graph_engine.testing.feature_bundle import compile_bundle


def build_intake_graphs(*args, **kwargs):
    return compile_bundle(_build_intake_graphs(*args, **kwargs))


_SHA = "a" * 64
_RECEIPT = ReceiptRef(receipt_id="receipt-1", receipt_digest=_SHA)


def _contracts() -> dict[str, TaskAttemptContract[Any, Any]]:
    return {
        **{contract.contract_id: contract.to_task_contract() for contract in AGENT_JOB_CONTRACTS.values()},
        **{contract.contract_id: contract for contract in TASK_ATTEMPT_CONTRACTS.values()},
    }


def _reviewed_case() -> dict[str, object]:
    plan_ref = {"path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json", "digest": _SHA}
    return {
        "change_id": "CH-DEMO-001",
        "coverage_epoch": 0,
        "plan_digest": _SHA,
        "plan_ref": plan_ref,
        "preparation_refs": [{"path": "qa/requirement.md", "digest": _SHA}, plan_ref],
        "case_refs": [{"path": "qa/cases/menus/case.yaml", "digest": _SHA}],
        "review_ref": {"path": "qa/results/review/case-review.json", "digest": _SHA},
        "selection_ref": {"path": "qa/results/cases/epochs/0/selection.json", "digest": _SHA},
    }


def _review(public_outcome: str) -> dict[str, object]:
    return {"public_outcome": public_outcome, "reviewed_case": _reviewed_case()}


def _input(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "change_id": "CH-DEMO-001",
        "capability_leafs": ["entities.item.create"],
        "allowed_artifact_paths": ["qa/cases", "qa/proposal.md", "qa/results"],
        "budgets": {"review_rounds": 2},
        "plan_digest": _SHA,
        "plan_ref": {"path": f"qa/results/plan/{_SHA}/resolved-assurance-plan.json", "digest": _SHA},
        "selected_test_families": ["api"],
        "case_delta_paths": ["qa/cases/menus/case.yaml"],
        "preparation_refs": [{"path": "qa/requirement.md", "digest": _SHA}],
        "coverage_epoch": 0,
    }
    payload.update(overrides)
    return payload


async def _run(script: Any, **overrides: object) -> dict[str, Any]:
    harness = GraphHarness()
    bundle = build_intake_graphs(
        harness.recording_context(owner_id="assurance.intake", contracts=_contracts())
    )
    result = await harness.run(bundle.case, input=_input(**overrides), script=script)
    terminal = result.terminal
    assert isinstance(terminal, dict)
    return {"terminal": terminal, "calls": [call.semantic_node_id for call in result.semantic_calls]}


@pytest.mark.parametrize(
    ("outcome", "status"),
    [("pass", "reviewed"), ("reject", "rejected")],
)
async def test_review_outcome_routes_to_its_terminal(outcome: str, status: str) -> None:
    result = await _run(
        {
            "intake.case-design": [committed({"artifacts": []}, _RECEIPT)],
            "intake.case-review": [committed(_review(outcome), _RECEIPT)],
        }
    )
    assert result["calls"] == ["intake.case-design", "intake.case-review"]
    assert result["terminal"]["status"] == status
    assert result["terminal"]["flow_outcome"] == status
    assert result["terminal"]["reviewed_refs"] == _reviewed_case()["preparation_refs"]
    assert "reviewed_case" not in result["terminal"]


async def test_needs_fix_runs_repair_then_reviews_again() -> None:
    result = await _run(
        {
            "intake.case-design": [committed({"artifacts": []}, _RECEIPT)],
            "intake.case-repair": [committed({"artifacts": []}, _RECEIPT)],
            "intake.case-review": [
                committed(_review("needs_fix"), _RECEIPT),
                committed(_review("pass"), _RECEIPT),
            ],
        }
    )
    assert result["calls"] == [
        "intake.case-design",
        "intake.case-review",
        "intake.case-repair",
        "intake.case-review",
    ]
    assert result["terminal"]["status"] == "reviewed"


async def test_needs_fix_past_the_budget_is_exhausted() -> None:
    result = await _run(
        {
            "intake.case-design": [committed({"artifacts": []}, _RECEIPT)],
            "intake.case-review": [committed(_review("needs_fix"), _RECEIPT)],
        },
        budgets={"review_rounds": 0},
    )
    assert result["calls"] == ["intake.case-design", "intake.case-review"]
    assert result["terminal"]["status"] == "exhausted"


async def test_case_design_failure_is_failed() -> None:
    result = await _run({"intake.case-design": [RejectedTaskResult(reason="case design failed")]})
    assert result["calls"] == ["intake.case-design"]
    assert result["terminal"]["status"] == "failed"
    assert result["terminal"].get("reviewed_case") is None


async def test_case_review_failure_is_exhausted() -> None:
    result = await _run(
        {
            "intake.case-design": [committed({"artifacts": []}, _RECEIPT)],
            "intake.case-review": [RejectedTaskResult(reason="review failed")],
        }
    )
    assert result["calls"] == ["intake.case-design", "intake.case-review"]
    assert result["terminal"]["status"] == "exhausted"


async def test_case_repair_failure_is_exhausted() -> None:
    result = await _run(
        {
            "intake.case-design": [committed({"artifacts": []}, _RECEIPT)],
            "intake.case-review": [committed(_review("needs_fix"), _RECEIPT)],
            "intake.case-repair": [RejectedTaskResult(reason="repair failed")],
        }
    )
    assert result["calls"] == ["intake.case-design", "intake.case-review", "intake.case-repair"]
    assert result["terminal"]["status"] == "exhausted"
