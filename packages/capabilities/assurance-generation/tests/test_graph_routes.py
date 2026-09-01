from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from langgraph.types import Send

from graph_engine.stategraph.routing import AmbiguousRouteMatch, select_exclusive_route
from assurance_generation.graphs.routes import (
    InsufficientRouteMatches,
    codegen_named_matches,
    family_select_named_matches,
    plan_advance_named_matches,
    plan_advance_retry_named_matches,
    plan_human_review_named_matches,
    plan_human_review_retry_named_matches,
    plan_review_named_matches,
    plan_review_retry_named_matches,
    route_families,
)

_FAMILIES = ("api", "e2e", "fuzz", "performance")
_GRAPHS_ROOT = Path(__file__).resolve().parents[1] / "assurance_generation" / "graphs"
_ROUTES_PATH = _GRAPHS_ROOT / "routes.py"

_EXCLUSIVE_ROUTE_ROWS = (
    ("assurance.generation.workflow.graph.generation", "select-api", "api-skip"),
    ("assurance.generation.workflow.graph.generation", "select-e2e", "e2e-skip"),
    ("assurance.generation.workflow.graph.generation", "select-fuzz", "fuzz-skip"),
    ("assurance.generation.workflow.graph.generation", "select-performance", "performance-skip"),
    ("assurance.generation.workflow.graph.generation-api", "codegen", "exhausted"),
    ("assurance.generation.workflow.graph.generation-api", "plan-human-review", "exhausted"),
    ("assurance.generation.workflow.graph.generation-api", "plan-human-review-retry", "exhausted"),
    ("assurance.generation.workflow.graph.generation-api", "plan-review", "exhausted"),
    ("assurance.generation.workflow.graph.generation-api", "plan-review-retry", "exhausted"),
    ("assurance.generation.workflow.graph.generation-api", "plan-review-round-advance", "exhausted"),
    (
        "assurance.generation.workflow.graph.generation-api",
        "plan-review-round-advance-retry",
        "exhausted",
    ),
    ("assurance.generation.workflow.graph.generation-e2e", "codegen", "exhausted"),
    ("assurance.generation.workflow.graph.generation-e2e", "plan-human-review", "exhausted"),
    ("assurance.generation.workflow.graph.generation-e2e", "plan-human-review-retry", "exhausted"),
    ("assurance.generation.workflow.graph.generation-e2e", "plan-review", "exhausted"),
    ("assurance.generation.workflow.graph.generation-e2e", "plan-review-retry", "exhausted"),
    ("assurance.generation.workflow.graph.generation-e2e", "plan-review-round-advance", "exhausted"),
    (
        "assurance.generation.workflow.graph.generation-e2e",
        "plan-review-round-advance-retry",
        "exhausted",
    ),
    ("assurance.generation.workflow.graph.generation-fuzz", "plan-human-review", "exhausted"),
    ("assurance.generation.workflow.graph.generation-fuzz", "plan-human-review-retry", "exhausted"),
    ("assurance.generation.workflow.graph.generation-fuzz", "plan-review", "exhausted"),
    ("assurance.generation.workflow.graph.generation-fuzz", "plan-review-retry", "exhausted"),
    ("assurance.generation.workflow.graph.generation-fuzz", "plan-review-round-advance", "exhausted"),
    (
        "assurance.generation.workflow.graph.generation-fuzz",
        "plan-review-round-advance-retry",
        "exhausted",
    ),
    ("assurance.generation.workflow.graph.generation-performance", "plan-human-review", "exhausted"),
    (
        "assurance.generation.workflow.graph.generation-performance",
        "plan-human-review-retry",
        "exhausted",
    ),
    ("assurance.generation.workflow.graph.generation-performance", "plan-review", "exhausted"),
    ("assurance.generation.workflow.graph.generation-performance", "plan-review-retry", "exhausted"),
    (
        "assurance.generation.workflow.graph.generation-performance",
        "plan-review-round-advance",
        "exhausted",
    ),
    (
        "assurance.generation.workflow.graph.generation-performance",
        "plan-review-round-advance-retry",
        "exhausted",
    ),
)


def valid_input() -> dict[str, object]:
    return {
        "change_id": "CH-DEMO-001",
        "selected_test_families": ["api", "e2e"],
        "capability_leafs": ["entities.item.create"],
        "allowed_artifact_paths": ["qa/changes"],
        "rounds_used": 0,
        "rounds_budget": 2,
    }


def input_with_missing_lane() -> dict[str, object]:
    return {
        "change_id": "CH-DEMO-001",
        "selected_test_families": [],
        "capability_leafs": ["entities.item.create"],
        "allowed_artifact_paths": ["qa/changes"],
    }


def _review_state(
    *,
    decision: str = "unknown",
    auto_fix_allowed: bool = False,
    human_review_required: bool = False,
    codegen_readiness: str = "not_ready",
    rounds_used: int = 0,
    rounds_budget: int = 2,
    action: str | None = None,
    verdict: str | None = None,
    needs_fix: bool | None = None,
    selected_test_families: tuple[str, ...] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "decision": decision,
        "auto_fix_allowed": auto_fix_allowed,
        "human_review_required": human_review_required,
        "codegen_readiness": codegen_readiness,
        "rounds_used": rounds_used,
        "rounds_budget": rounds_budget,
    }
    if action is not None:
        payload["human_action"] = action
    if verdict is not None:
        payload["codegen_verdict"] = verdict
    if needs_fix is not None:
        payload["needs_fix"] = needs_fix
    if selected_test_families is not None:
        payload["selected_test_families"] = list(selected_test_families)
    return payload


def _named_matches_for(node_id: str) -> Callable[[Mapping[str, object]], dict[str, str | None]]:
    if node_id.startswith("select-"):
        family = node_id.removeprefix("select-")
        return lambda state: family_select_named_matches(state, family)
    builders = {
        "plan-review": plan_review_named_matches,
        "plan-review-retry": plan_review_retry_named_matches,
        "plan-human-review": plan_human_review_named_matches,
        "plan-human-review-retry": plan_human_review_retry_named_matches,
        "plan-review-round-advance": plan_advance_named_matches,
        "plan-review-round-advance-retry": plan_advance_retry_named_matches,
        "codegen": codegen_named_matches,
    }
    return builders[node_id]


def test_generation_route_emits_exactly_four_sends() -> None:
    sends = route_families(valid_input())
    assert all(isinstance(send, Send) for send in sends)
    assert tuple(send.node for send in sends) == ("api", "e2e", "fuzz", "performance")
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
    source = _ROUTES_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_ROUTES_PATH))
    assert "select_exclusive_route" in source
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and node.orelse:
            for child in node.orelse:
                assert not isinstance(child, ast.If), "exclusive routes must not use priority if/elif"


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
    assert all(hit.startswith("routes.py:") for hit in send_hits)
    assert shim_hits == []
    source = _ROUTES_PATH.read_text(encoding="utf-8")
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
    empty = builder(_review_state(rounds_used=2, rounds_budget=2))
    assert select_exclusive_route(empty, otherwise=otherwise) == otherwise
    with pytest.raises(AmbiguousRouteMatch):
        select_exclusive_route(
            {"first": otherwise, "second": f"{otherwise}-alt"},
            otherwise=otherwise,
        )


def test_plan_review_pass_auto_fix_reject_human_and_not_ready() -> None:
    from assurance_generation.graphs.routes import route_plan_review

    assert route_plan_review(_review_state(decision="pass", codegen_readiness="ready")) == "codegen"
    assert route_plan_review(_review_state(decision="approved", codegen_readiness="ready")) == "codegen"
    assert route_plan_review(_review_state(decision="pass", codegen_readiness="not_ready")) == "exhausted"
    assert (
        route_plan_review(
            _review_state(decision="needs_fix", auto_fix_allowed=True, rounds_used=0, rounds_budget=2)
        )
        == "plan-review-round-advance"
    )
    assert route_plan_review(_review_state(decision="reject")) == "rejected"
    assert (
        route_plan_review(_review_state(decision="needs_human_review", human_review_required=True))
        == "plan-human-review"
    )
    assert (
        route_plan_review(
            _review_state(decision="needs_fix", auto_fix_allowed=True, rounds_used=2, rounds_budget=2)
        )
        == "exhausted"
    )


def test_plan_review_retry_and_human_review_routes() -> None:
    from assurance_generation.graphs.routes import (
        route_plan_human_review,
        route_plan_human_review_retry,
        route_plan_review_retry,
    )

    assert (
        route_plan_review_retry(
            _review_state(decision="needs_fix", auto_fix_allowed=True, rounds_used=1, rounds_budget=2)
        )
        == "plan-review-round-advance-retry"
    )
    assert (
        route_plan_review_retry(_review_state(decision="needs_human_review", human_review_required=True))
        == "plan-human-review-retry"
    )
    assert route_plan_human_review(_review_state(action="approve")) == "codegen"
    assert route_plan_human_review(_review_state(action="reject")) == "rejected"
    assert (
        route_plan_human_review(_review_state(action="request_rework", rounds_used=0, rounds_budget=2))
        == "plan-review-round-advance"
    )
    assert (
        route_plan_human_review(_review_state(action="request_rework", rounds_used=2, rounds_budget=2))
        == "exhausted"
    )
    assert (
        route_plan_human_review_retry(_review_state(action="request_rework", rounds_used=1, rounds_budget=2))
        == "plan-review-round-advance-retry"
    )


def test_codegen_routes_accepted_needs_fix_and_otherwise() -> None:
    from assurance_generation.graphs.routes import route_codegen

    assert route_codegen(_review_state(verdict="accepted")) == "done"
    assert route_codegen(_review_state(needs_fix=False)) == "done"
    assert route_codegen(_review_state(verdict="needs_fix")) == "codegen-round-advance"
    assert route_codegen(_review_state(needs_fix=True)) == "codegen-round-advance"
    assert route_codegen(_review_state()) == "exhausted"


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
