from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from assurance_quality.contracts.attempts import AGENT_JOB_CONTRACTS
from assurance_quality.contracts.workflow import COVERAGE_STATES, FailureClassification
from assurance_quality.graphs.factory import build_quality_graphs
from assurance_quality.graphs.routes import (
    coverage_named_matches,
    failure_named_matches,
    route_coverage,
    route_failure,
)
from graph_engine.attempts.contracts import TaskAttemptContract
from graph_engine.stategraph.routing import AmbiguousRouteMatch, select_exclusive_route
from graph_engine.testing import GraphHarness


def quality_contracts() -> dict[str, TaskAttemptContract[Any, Any]]:
    return {contract.contract_id: contract.to_task_contract() for contract in AGENT_JOB_CONTRACTS.values()}


_GRAPHS_ROOT = Path(__file__).resolve().parents[1] / "assurance_quality" / "graphs"
_ROUTES_PATH = _GRAPHS_ROOT / "routes.py"
_FORBIDDEN_IMPLEMENTATION = (
    "operations",
    "validators",
    "effects",
    "resources",
    "resource_loader",
    "plugin",
)
_FOREIGN_GRAPH_OWNERS = (
    "assurance_intake",
    "assurance_generation",
    "assurance_execution",
    "assurance_healing",
    "assurance_improvement",
)
_COVERAGE_OTHERWISE = "failed"
_FAILURE_OTHERWISE = "failed"


def _imported_names(path: Path, *, module_name: str) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    parts = module_name.split(".")
    is_package = path.name == "__init__.py"
    package_parts = parts if is_package else parts[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                drop = node.level - 1
                base_parts = package_parts[: max(0, len(package_parts) - drop)]
                parent_parts = [*base_parts, *([node.module] if node.module else [])]
                parent = ".".join(part for part in parent_parts if part)
            else:
                parent = node.module or ""
            if parent:
                found.append(parent)
            for alias in node.names:
                if alias.name == "*":
                    continue
                found.append(f"{parent}.{alias.name}" if parent else alias.name)
    return tuple(found)


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
        (coverage_named_matches, _COVERAGE_OTHERWISE),
        (failure_named_matches, _FAILURE_OTHERWISE),
    ],
)
def test_exclusive_route_zero_and_two_simultaneous_named_matches(
    builder: Callable[[Mapping[str, object]], dict[str, str | None]], otherwise: str
) -> None:
    empty = builder({})
    assert select_exclusive_route(empty, otherwise=otherwise) == otherwise
    with pytest.raises(AmbiguousRouteMatch):
        select_exclusive_route(
            {"first": otherwise, "second": f"{otherwise}-alt"},
            otherwise=otherwise,
        )


@pytest.mark.parametrize("coverage_state", COVERAGE_STATES)
def test_known_coverage_states_route_to_themselves(coverage_state: str) -> None:
    assert route_coverage({"coverage_state": coverage_state}) == coverage_state


def test_unknown_coverage_classification_fails_closed() -> None:
    first_branch = COVERAGE_STATES[0]
    assert route_coverage({"coverage_state": "not_a_coverage_state"}) == _COVERAGE_OTHERWISE
    assert route_coverage({}) == _COVERAGE_OTHERWISE
    assert route_coverage({"coverage_state": "not_a_coverage_state"}) != first_branch
    assert coverage_named_matches({"coverage_state": "not_a_coverage_state"})[first_branch] is None


def test_known_failure_classifications_do_not_select_healing() -> None:
    assert route_failure({"classification": "test", "fix_eligible": True}) == "fix-eligible"
    assert route_failure({"classification": "test-data", "fix_eligible": True}) == "fix-eligible"
    assert route_failure({"classification": "product_bug"}) == "report-issue"
    assert route_failure({"classification": "environment_failure"}) == "report-issue"
    assert route_failure({"classification": "infrastructure_failure"}) == "report-issue"
    assert "heal" not in route_failure({"classification": "test", "fix_eligible": True})
    assert "repair" not in route_failure({"classification": "test", "fix_eligible": True})


def test_unknown_failure_classification_fails_closed() -> None:
    first_branch = "environment_failure"
    assert route_failure({"classification": "unknown"}) == _FAILURE_OTHERWISE
    assert route_failure({"classification": "pending"}) == _FAILURE_OTHERWISE
    assert route_failure({"classification": "failed"}) == _FAILURE_OTHERWISE
    assert route_failure({"classification": "not_a_failure"}) == _FAILURE_OTHERWISE
    assert route_failure({}) == _FAILURE_OTHERWISE
    assert route_failure({"classification": "unknown"}) != first_branch
    assert route_failure({"classification": "test", "fix_eligible": False}) == _FAILURE_OTHERWISE
    matches = failure_named_matches({"classification": "unknown"})
    assert all(target is None for target in matches.values())


def test_attempt_failure_fails_closed_despite_leftover_successful_outcome() -> None:
    failure = {
        "resolution_kind": "rejected",
        "reason": "kernel rejected",
        "writes_promoted": False,
    }
    assess_recheck = {
        "coverage_state": "satisfied",
        "attempt_failure": failure,
    }
    issue_recheck = {
        "classification": "test",
        "fix_eligible": True,
        "attempt_failure": failure,
    }
    assert route_coverage(assess_recheck) == _COVERAGE_OTHERWISE
    assert route_failure(issue_recheck) == _FAILURE_OTHERWISE
    assert route_coverage(assess_recheck) != "satisfied"
    assert route_failure(issue_recheck) != "fix-eligible"


def test_quality_graphs_do_not_import_healing_graphs_or_implementation() -> None:
    violations: list[str] = []
    healing_hits: list[str] = []
    for path in sorted(_GRAPHS_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(_GRAPHS_ROOT.parent)
        module_name = "assurance_quality." + ".".join(relative.with_suffix("").parts)
        if module_name.endswith(".__init__"):
            module_name = module_name[: -len(".__init__")]
        for imported in _imported_names(path, module_name=module_name):
            parts = imported.split(".")
            if any(part in _FORBIDDEN_IMPLEMENTATION for part in parts):
                violations.append(f"{path}:{imported}")
            if imported == "assurance_healing.graphs" or imported.startswith("assurance_healing.graphs."):
                healing_hits.append(f"{path}:{imported}")
            if imported.startswith("assurance_healing.") and parts[1:2] != ["contracts"]:
                healing_hits.append(f"{path}:{imported}")
            if parts[0] in _FOREIGN_GRAPH_OWNERS and "graphs" in parts:
                violations.append(f"{path}:{imported}")
    assert violations == []
    assert healing_hits == []


def test_quality_graphs_do_not_own_healing_selection() -> None:
    context = GraphHarness().recording_context(
        owner_id="assurance.quality",
        contracts=quality_contracts(),
    )
    bundle = build_quality_graphs(context)
    del bundle
    for path in sorted(_GRAPHS_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        assert "assurance_healing.graphs" not in source
        assert "repair_coverage" not in source
        assert "repair_failure" not in source
        assert "healing.repair" not in source


def test_failure_classification_literal_is_closed() -> None:
    allowed: tuple[FailureClassification, ...] = (
        "environment_failure",
        "failed",
        "infrastructure_failure",
        "pending",
        "product_bug",
        "test",
        "test-data",
        "unknown",
    )
    assert set(allowed) == {
        "environment_failure",
        "failed",
        "infrastructure_failure",
        "pending",
        "product_bug",
        "test",
        "test-data",
        "unknown",
    }
