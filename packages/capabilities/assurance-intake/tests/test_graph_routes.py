from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from graph_engine.stategraph.routing import AmbiguousRouteMatch, select_exclusive_route
from assurance_intake.graphs.routes import (
    case_design_named_matches,
    case_review_named_matches,
    human_review_named_matches,
    route_case_design,
    route_case_design_result,
    route_case_review,
    route_human_review,
    route_review_round,
)

_ROUTES_PATH = Path(__file__).resolve().parents[1] / "assurance_intake" / "graphs" / "routes.py"


def _review_state(
    *,
    decision: str = "needs_fix",
    auto_fix_allowed: bool = False,
    human_review_required: bool = False,
    rounds_used: int = 0,
    rounds_budget: int = 2,
    action: str | None = None,
    validation_status: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "decision": decision,
        "auto_fix_allowed": auto_fix_allowed,
        "human_review_required": human_review_required,
        "rounds_used": rounds_used,
        "rounds_budget": rounds_budget,
    }
    if action is not None:
        payload["human_action"] = action
    if validation_status is not None:
        payload["validation_status"] = validation_status
    return payload


def test_routes_use_select_exclusive_route_without_priority_if_elif() -> None:
    source = _ROUTES_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_ROUTES_PATH))
    assert "select_exclusive_route" in source
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and node.orelse:
            for child in node.orelse:
                assert not isinstance(child, ast.If), "exclusive routes must not use priority if/elif"


@pytest.mark.parametrize(
    ("builder", "otherwise"),
    [
        (case_review_named_matches, "exhausted"),
        (human_review_named_matches, "exhausted"),
        (case_design_named_matches, "case-design-validation-retry"),
    ],
)
def test_exclusive_route_zero_and_two_simultaneous_named_matches(
    builder: Callable[[Mapping[str, object]], dict[str, str | None]], otherwise: str
) -> None:
    empty = builder(_review_state(decision="unknown", validation_status="unknown"))
    assert select_exclusive_route(empty, otherwise=otherwise) == otherwise
    with pytest.raises(AmbiguousRouteMatch):
        select_exclusive_route(
            {"first": otherwise, "second": f"{otherwise}-alt"},
            otherwise=otherwise,
        )


def test_case_review_pass_and_automatic_fix_and_reject_and_human() -> None:
    assert route_case_review(_review_state(decision="pass")) == "done"
    assert route_case_review(_review_state(decision="approved")) == "exhausted"
    assert (
        route_case_review(
            _review_state(decision="needs_fix", auto_fix_allowed=True, rounds_used=0, rounds_budget=2)
        )
        == "review-round-advance"
    )
    assert route_case_review(_review_state(decision="reject")) == "rejected"
    assert route_case_review(_review_state(decision="needs_human_review", human_review_required=True)) == (
        "human-review"
    )
    assert (
        route_case_review(
            _review_state(decision="needs_fix", auto_fix_allowed=True, rounds_used=2, rounds_budget=2)
        )
        == "exhausted"
    )


def test_human_review_routes_and_budget_exhaustion() -> None:
    assert route_human_review(_review_state(action="approve")) == "done"
    assert route_human_review(_review_state(action="reject")) == "rejected"
    assert (
        route_human_review(_review_state(action="request_rework", rounds_used=0, rounds_budget=2))
        == "review-round-advance"
    )
    assert (
        route_human_review(_review_state(action="request_rework", rounds_used=2, rounds_budget=2))
        == "exhausted"
    )


def test_case_design_result_names_case_review_or_exhausted() -> None:
    assert route_case_design_result({"status": "passed"}) == "case-review"
    assert route_case_design_result({"status": "failed"}) == "exhausted"


def test_case_design_routes_pass_to_done_and_otherwise_to_repair() -> None:
    assert route_case_design(_review_state(validation_status="pass")) == "done"
    assert route_case_design(_review_state(validation_status="needs_fix")) == "case-design-validation-retry"


def test_review_round_sends_automatic_fix_to_repair_and_human_rework_to_full_design() -> None:
    budget_spent_by_advance = _review_state(
        decision="needs_fix", auto_fix_allowed=True, rounds_used=2, rounds_budget=2
    )
    assert route_review_round(budget_spent_by_advance) == "case-repair"
    assert (
        route_review_round(
            _review_state(
                decision="needs_fix",
                auto_fix_allowed=True,
                human_review_required=True,
                action="request_rework",
            )
        )
        == "case-design"
    )
    assert (
        route_review_round(_review_state(decision="needs_human_review", action="request_rework"))
        == "case-design"
    )
    assert route_review_round(_review_state(decision="needs_fix", action="request_rework")) == "case-design"
