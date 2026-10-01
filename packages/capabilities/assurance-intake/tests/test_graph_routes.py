from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from graph_engine.stategraph.routing import AmbiguousRouteMatch, select_exclusive_route
from assurance_intake.graphs.case import (
    case_review_named_matches,
    human_review_named_matches,
    route_case_review,
    route_human_review,
    route_review_round,
)

_ROUTES_PATH = Path(__file__).resolve().parents[1] / "assurance_intake" / "graphs" / "case.py"
_ROUTE_FUNCTIONS = (
    "_has_budget",
    "_is_review_repair",
    "_named_matches",
    "case_review_named_matches",
    "human_review_named_matches",
    "route_case_review",
    "route_human_review",
    "route_review_round",
)


def _review_state(
    *,
    decision: str = "needs_fix",
    auto_fix_allowed: bool = False,
    human_review_required: bool = False,
    rounds_used: int = 0,
    rounds_budget: int = 2,
    action: str | None = None,
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
    return payload


def _assert_no_priority_if(node: ast.AST) -> None:
    for child in ast.walk(node):
        if isinstance(child, ast.If) and child.orelse:
            for branch in child.orelse:
                assert not isinstance(branch, ast.If), "exclusive routes must not use priority if/elif"


def test_routes_use_select_exclusive_route_without_priority_if_elif() -> None:
    source = _ROUTES_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_ROUTES_PATH))
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    inspected = [functions[name] for name in _ROUTE_FUNCTIONS]
    assert [name for name in functions if name.startswith("route_")] == [
        "route_case_review",
        "route_human_review",
        "route_review_round",
    ]
    for node in inspected:
        if node.name.startswith("route_"):
            assert any(
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "select_exclusive_route"
                for child in ast.walk(node)
            ), node.name
        _assert_no_priority_if(node)
    for node in tree.body:
        table: ast.AST | None = None
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id.endswith("_TABLE")
        ):
            table = node.value
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.endswith("_TABLE")
        ):
            table = node.value
        if table is not None:
            _assert_no_priority_if(table)


@pytest.mark.parametrize(
    ("builder", "otherwise"),
    [
        (case_review_named_matches, "exhausted"),
        (human_review_named_matches, "exhausted"),
    ],
)
def test_exclusive_route_zero_and_two_simultaneous_named_matches(
    builder: Callable[[Mapping[str, object]], dict[str, str | None]], otherwise: str
) -> None:
    empty = builder(_review_state(decision="unknown"))
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
