from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from langgraph.types import Send

from assurance_generation.graphs.api import (
    plan_advance_named_matches,
    plan_human_review_named_matches,
    plan_review_named_matches,
    route_plan_advance,
    route_plan_human_review,
    route_plan_review,
)
from assurance_generation.graphs.factory import (
    InsufficientRouteMatches,
    family_select_named_matches,
    route_families,
)
from graph_engine.stategraph.routing import AmbiguousRouteMatch, select_exclusive_route

_FAMILIES = ("api", "e2e", "fuzz", "performance")
_GRAPHS_ROOT = Path(__file__).resolve().parents[1] / "assurance_generation" / "graphs"
_API_ROUTES = _GRAPHS_ROOT / "api.py"
_FACTORY_ROUTES = _GRAPHS_ROOT / "factory.py"
_INIT_ROUTES = _GRAPHS_ROOT / "init_runtime.py"
_EXCLUSIVE_ROUTE_FUNCTIONS = (
    "_has_budget",
    "_within_spent_budget",
    "_named_matches",
    "family_entry_named_matches",
    "plan_advance_named_matches",
    "plan_human_review_named_matches",
    "plan_review_named_matches",
    "route_family_entry",
    "route_plan_advance",
    "route_plan_human_review",
    "route_plan_review",
)
_FANOUT_ROUTE_FUNCTIONS = ("family_select_named_matches", "route_families")
_ATTEMPT_RESULT_FUNCTIONS = ("route_attempt_result",)

_EXCLUSIVE_ROUTE_ROWS = (
    ("assurance.generation.workflow.graph.generation", "select-api", "api-skip"),
    ("assurance.generation.workflow.graph.generation", "select-e2e", "e2e-skip"),
    ("assurance.generation.workflow.graph.generation", "select-fuzz", "fuzz-skip"),
    ("assurance.generation.workflow.graph.generation", "select-performance", "performance-skip"),
    ("assurance.generation.workflow.graph.generation-api", "codegen-human-review", "exhausted"),
    ("assurance.generation.workflow.graph.generation-api", "codegen-review", "exhausted"),
    ("assurance.generation.workflow.graph.generation-api", "codegen-review-round-advance", "exhausted"),
    ("assurance.generation.workflow.graph.generation-e2e", "codegen-human-review", "exhausted"),
    ("assurance.generation.workflow.graph.generation-e2e", "codegen-review", "exhausted"),
    ("assurance.generation.workflow.graph.generation-e2e", "codegen-review-round-advance", "exhausted"),
    ("assurance.generation.workflow.graph.generation-fuzz", "codegen-human-review", "exhausted"),
    ("assurance.generation.workflow.graph.generation-fuzz", "codegen-review", "exhausted"),
    ("assurance.generation.workflow.graph.generation-fuzz", "codegen-review-round-advance", "exhausted"),
    ("assurance.generation.workflow.graph.generation-performance", "codegen-human-review", "exhausted"),
    ("assurance.generation.workflow.graph.generation-performance", "codegen-review", "exhausted"),
    (
        "assurance.generation.workflow.graph.generation-performance",
        "codegen-review-round-advance",
        "exhausted",
    ),
)


def valid_input() -> dict[str, object]:
    return {
        "change_id": "CH-DEMO-001",
        "selected_test_families": ["api", "e2e"],
        "capability_leafs": ["entities.item.create"],
        "allowed_artifact_paths": [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
        "rounds_used": 0,
        "rounds_budget": 2,
    }


def input_with_missing_lane() -> dict[str, object]:
    return {
        "change_id": "CH-DEMO-001",
        "selected_test_families": [],
        "capability_leafs": ["entities.item.create"],
        "allowed_artifact_paths": [
            "qa/.qa.yaml",
            "qa/cases",
            "qa/fixtures",
            "qa/proposal.md",
            "qa/requirement.md",
            "qa/results",
            "qa/tests",
        ],
    }


def _review_state(
    *,
    route: str = "unknown",
    rounds_used: int = 0,
    rounds_budget: int = 2,
    action: str | None = None,
    selected_test_families: tuple[str, ...] | None = None,
    attempt_failure: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "route": route,
        "rounds_used": rounds_used,
        "rounds_budget": rounds_budget,
    }
    if action is not None:
        payload["human_action"] = action
    if selected_test_families is not None:
        payload["selected_test_families"] = list(selected_test_families)
    if attempt_failure is not None:
        payload["attempt_failure"] = attempt_failure
    return payload


def _named_matches_for(node_id: str) -> Callable[[Mapping[str, object]], dict[str, str | None]]:
    if node_id.startswith("select-"):
        family = node_id.removeprefix("select-")
        return lambda state: family_select_named_matches(state, family)
    builders = {
        "codegen-review": plan_review_named_matches,
        "codegen-human-review": plan_human_review_named_matches,
        "codegen-review-round-advance": plan_advance_named_matches,
    }
    return builders[node_id]


def _functions(path: Path) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}


def _assert_no_priority_if(node: ast.AST) -> None:
    for child in ast.walk(node):
        if isinstance(child, ast.If) and child.orelse:
            for branch in child.orelse:
                assert not isinstance(branch, ast.If), "exclusive routes must not use priority if/elif"


def _table_value(node: ast.AST) -> ast.AST | None:
    if (
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and (node.target.id.endswith("_TABLE") or node.target.id.endswith("named_matches"))
    ):
        return node.value
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and (node.targets[0].id.endswith("_TABLE") or node.targets[0].id.endswith("named_matches"))
    ):
        return node.value
    return None


def test_generation_route_emits_exactly_four_sends() -> None:
    sends = route_families(valid_input())
    assert all(isinstance(send, Send) for send in sends)
    assert tuple(send.node for send in sends) == ("api", "e2e", "fuzz", "performance")
    assert {send.arg.get("rounds_budget") for send in sends} == {3}
    selected = {send.node: send.arg.get("lane_selected") for send in sends}
    assert selected == {"api": True, "e2e": True, "fuzz": False, "performance": False}


def test_generation_route_fails_before_producing_any_send() -> None:
    with pytest.raises(InsufficientRouteMatches):
        route_families(input_with_missing_lane())
    for invalid in (
        {"selected_test_families": ["mobile"]},
        {"selected_test_families": ["api", "api"]},
        {},
    ):
        with pytest.raises(InsufficientRouteMatches):
            route_families(invalid)


def test_routes_use_select_exclusive_route_without_priority_if_elif() -> None:
    source = _API_ROUTES.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_API_ROUTES))
    functions = _functions(_API_ROUTES)
    inspected = [functions[name] for name in _EXCLUSIVE_ROUTE_FUNCTIONS]
    assert [name for name in functions if name.startswith("route_")] == [
        "route_family_entry",
        "route_plan_review",
        "route_plan_human_review",
        "route_plan_advance",
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
        table = _table_value(node)
        if table is not None:
            _assert_no_priority_if(table)

    fanout = _functions(_FACTORY_ROUTES)
    assert [name for name in fanout if name.startswith("route_")] == ["route_families"]
    for name in _FANOUT_ROUTE_FUNCTIONS:
        _assert_no_priority_if(fanout[name])
        assert not any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "select_exclusive_route"
            for child in ast.walk(fanout[name])
        ), name
    for node in ast.parse(_FACTORY_ROUTES.read_text(encoding="utf-8"), filename=str(_FACTORY_ROUTES)).body:
        table = _table_value(node)
        if table is not None:
            _assert_no_priority_if(table)

    attempt = _functions(_INIT_ROUTES)
    assert [name for name in attempt if name.startswith("route_")] == ["route_attempt_result"]
    for name in _ATTEMPT_RESULT_FUNCTIONS:
        _assert_no_priority_if(attempt[name])


def test_generation_send_is_only_in_route_families_and_has_no_fanout_shim() -> None:
    send_hits: list[str] = []
    shim_hits: list[str] = []
    for path in sorted(_GRAPHS_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "Send":
                send_hits.append(f"{path.name}:{node.lineno}")
            if isinstance(node, ast.Name) and node.id in {"fanout", "min_matches"}:
                shim_hits.append(f"{path.name}:{node.lineno}:{node.id}")
            if isinstance(node, ast.Attribute) and node.attr in {"fanout"}:
                shim_hits.append(f"{path.name}:{node.lineno}:{node.attr}")
    assert send_hits
    assert all(hit.startswith("factory.py:") for hit in send_hits)
    assert shim_hits == []
    source = _FACTORY_ROUTES.read_text(encoding="utf-8")
    assert "def route_families" in source
    assert "finalize-inputs" not in source
    assert "repair-prepare" not in source


@pytest.mark.parametrize(
    "row",
    _EXCLUSIVE_ROUTE_ROWS,
    ids=lambda row: f"{row[0]}/{row[1]}",
)
def test_exclusive_route(row: tuple[str, str, str]) -> None:
    _graph_id, node_id, otherwise = row
    builder = _named_matches_for(node_id)
    empty = builder(_review_state(rounds_used=3, rounds_budget=2))
    assert select_exclusive_route(empty, otherwise=otherwise) == otherwise
    with pytest.raises(AmbiguousRouteMatch):
        select_exclusive_route(
            {"first": otherwise, "second": f"{otherwise}-alt"},
            otherwise=otherwise,
        )


def test_plan_review_pass_auto_fix_reject_human_and_not_ready() -> None:
    assert route_plan_review(_review_state(route="codegen")) == "done"
    assert route_plan_review(_review_state(route="approved")) == "exhausted"
    assert (
        route_plan_review(_review_state(route="auto_fix", rounds_used=0, rounds_budget=2))
        == "codegen-review-round-advance"
    )
    assert route_plan_review(_review_state(route="reject")) == "rejected"
    assert route_plan_review(_review_state(route="human")) == "codegen-human-review"
    assert route_plan_review(_review_state(route="auto_fix", rounds_used=2, rounds_budget=2)) == "exhausted"
    assert (
        route_plan_review(
            _review_state(
                route="auto_fix",
                rounds_used=0,
                rounds_budget=2,
                attempt_failure={"resolution_kind": "permanent"},
            )
        )
        == "codegen-review-round-advance"
    )


def test_plan_human_review_routes() -> None:
    assert route_plan_human_review(_review_state(action="approve")) == "done"
    assert route_plan_human_review(_review_state(action="reject")) == "rejected"
    assert (
        route_plan_human_review(_review_state(action="request_rework", rounds_used=0, rounds_budget=2))
        == "codegen-review-round-advance"
    )
    assert (
        route_plan_human_review(_review_state(action="request_rework", rounds_used=2, rounds_budget=2))
        == "exhausted"
    )


def test_last_budgeted_plan_advance_joins_after_increment() -> None:
    assert route_plan_advance(_review_state(rounds_used=2, rounds_budget=2)) == "codegen-round-join"
    assert route_plan_advance(_review_state(rounds_used=1, rounds_budget=2)) == "codegen-round-join"
    assert route_plan_advance(_review_state(rounds_used=3, rounds_budget=2)) == "exhausted"


@pytest.mark.parametrize("family", _FAMILIES)
def test_family_select_named_matches_are_selected_or_skip(family: str) -> None:
    selected = family_select_named_matches(
        _review_state(selected_test_families=(family,)),
        family,
    )
    skipped = family_select_named_matches(_review_state(selected_test_families=()), family)
    otherwise = f"{family}-skip"
    assert select_exclusive_route(selected, otherwise=otherwise) == family
    assert select_exclusive_route(skipped, otherwise=otherwise) == otherwise
