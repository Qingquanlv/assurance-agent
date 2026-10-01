from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from assurance_healing.contracts.agent import FixProposalResultV1
from assurance_healing.contracts.proposal import FixProposalSummary
from assurance_healing.contracts.attempts import AGENT_JOB_CONTRACTS
from assurance_healing.contracts.coverage_repair import HEALING_REPAIR_OUTCOMES
from assurance_healing.graphs.coverage import (
    admit_coverage_named_matches,
    coverage_status_named_matches,
    route_admit_coverage,
    route_coverage_status,
)
from assurance_healing.graphs.factory import build_healing_graphs
from assurance_healing.graphs.failure import (
    admit_failure_named_matches,
    failure_status_named_matches,
    route_admit_failure,
    route_failure_status,
    route_proposal_approval,
)
from assurance_healing.graphs.nodes import publish_repair
from graph_engine.attempts.contracts import TaskAttemptContract
from graph_engine.attempts.resolutions import ReceiptRef, RejectedTaskResult
from graph_engine.stategraph.routing import AmbiguousRouteMatch, select_exclusive_route
from graph_engine.testing import GraphHarness, committed

from test_healing_graph_factory import (  # type: ignore[import-not-found]
    coverage_agent_output,
    coverage_graph_input,
    failure_graph_input,
    healing_contracts,
)

_GRAPHS_ROOT = Path(__file__).resolve().parents[1] / "assurance_healing" / "graphs"
_ROUTE_PATHS = (_GRAPHS_ROOT / "failure.py", _GRAPHS_ROOT / "coverage.py")
_ROUTE_FUNCTIONS = {
    "failure.py": (
        "route_admit_failure",
        "route_proposal_status",
        "route_proposal_approval",
        "route_failure_status",
    ),
    "coverage.py": ("route_admit_coverage", "route_coverage_status"),
}
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
    "assurance_quality",
    "assurance_improvement",
)
_ADMIT_OTHERWISE = "not-eligible"
_STATUS_OTHERWISE = "failed"
_SHA = "a" * 64


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


def _assert_no_priority_if(node: ast.AST) -> None:
    for child in ast.walk(node):
        if isinstance(child, ast.If) and child.orelse:
            for branch in child.orelse:
                assert not isinstance(branch, ast.If), "exclusive routes must not use priority if/elif"


def test_routes_use_select_exclusive_route_without_priority_if_elif() -> None:
    for path in _ROUTE_PATHS:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        functions = {
            node.name: node for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        expected = _ROUTE_FUNCTIONS[path.name]
        assert tuple(name for name in functions if name.startswith("route_")) == expected
        for name in expected:
            node = functions[name]
            assert any(
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "select_exclusive_route"
                for child in ast.walk(node)
            ), name
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
        (admit_failure_named_matches, _ADMIT_OTHERWISE),
        (admit_coverage_named_matches, _ADMIT_OTHERWISE),
        (coverage_status_named_matches, _STATUS_OTHERWISE),
        (failure_status_named_matches, _STATUS_OTHERWISE),
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


def test_failure_admit_routes_eligible_exhausted_and_not_eligible() -> None:
    assert route_admit_failure(failure_graph_input()) == "repair-round-advance"
    assert route_admit_failure(failure_graph_input(classification="test-data")) == "repair-round-advance"
    assert route_admit_failure(failure_graph_input(rounds_used=2, rounds_budget=2)) == "exhausted"
    assert route_admit_failure(failure_graph_input(classification="product_bug")) == "not-eligible"
    assert route_admit_failure(failure_graph_input(fix_eligible=False)) == "not-eligible"
    assert route_admit_failure({}) == "not-eligible"


def test_proposal_approval_routes_only_authenticated_approval_to_application() -> None:
    assert route_proposal_approval(failure_graph_input(human_action="approve")) == "healing.apply-test-repair"
    assert (
        route_proposal_approval(failure_graph_input(human_action="reject", approval_ref=None))
        == "needs-review"
    )
    assert (
        route_proposal_approval(failure_graph_input(human_action="approve", approval_ref=None))
        == "needs-review"
    )


def test_coverage_admit_routes_eligible_exhausted_and_not_eligible() -> None:
    assert route_admit_coverage(coverage_graph_input()) == "repair-round-advance"
    assert route_admit_coverage(coverage_graph_input(rounds_used=4, rounds_budget=4)) == "exhausted"
    assert route_admit_coverage(coverage_graph_input(fix_eligible=False)) == "not-eligible"
    assert route_admit_coverage({}) == "not-eligible"


@pytest.mark.parametrize("status", HEALING_REPAIR_OUTCOMES)
def test_known_coverage_statuses_route_to_named_terminals(status: str) -> None:
    expected = {
        "repaired": "done",
        "needs_review": "needs-review",
        "exhausted": "exhausted",
        "not_eligible": "not-eligible",
        "failed": "failed",
    }[status]
    assert route_coverage_status({"status": status}) == expected


def test_unknown_coverage_status_fails_closed() -> None:
    first_branch = HEALING_REPAIR_OUTCOMES[0]
    assert route_coverage_status({"status": "unknown"}) == _STATUS_OTHERWISE
    assert route_coverage_status({"status": "pending"}) == _STATUS_OTHERWISE
    assert route_coverage_status({"status": "in_progress"}) == _STATUS_OTHERWISE
    assert route_coverage_status({}) == _STATUS_OTHERWISE
    assert route_coverage_status({"status": "unknown"}) != first_branch
    matches = coverage_status_named_matches({"status": "unknown"})
    assert all(target is None for target in matches.values())


def test_attempt_failure_fails_closed_despite_leftover_successful_outcome() -> None:
    failure = {
        "resolution_kind": "rejected",
        "reason": "kernel rejected",
        "writes_promoted": False,
    }
    leftover = {"status": "repaired", "attempt_failure": failure}
    assert route_coverage_status(leftover) == _STATUS_OTHERWISE
    assert route_coverage_status(leftover) != "done"
    assert route_failure_status(leftover) == _STATUS_OTHERWISE
    assert route_failure_status(leftover) != "done"


def test_publish_repair_fails_closed_for_missing_unknown_and_in_progress() -> None:
    state = coverage_graph_input()
    receipt = ReceiptRef(receipt_id="r1", receipt_digest=_SHA)
    missing = coverage_agent_output()
    del missing["status"]
    published_missing = publish_repair(state, missing, receipt)
    assert published_missing["status"] == "failed"
    assert published_missing["status"] != "repaired"

    published_unknown = publish_repair(state, coverage_agent_output(status="unknown"), receipt)
    assert published_unknown["status"] == "failed"

    published_in_progress = publish_repair(state, coverage_agent_output(status="in_progress"), receipt)
    assert published_in_progress["status"] == "failed"

    failure_model = FixProposalResultV1(
        schema_version="1",
        change_id="CH-FIX-001",
        summary=FixProposalSummary(eligible_count=1),
    ).model_dump()
    assert "status" not in failure_model
    published_failure = publish_repair(failure_graph_input(), failure_model, receipt)
    assert published_failure["status"] == "failed"


def test_publish_repair_uses_kernel_receipt_effect_refs_not_output_extras() -> None:
    from assurance_healing.contracts.attempts import HEALING_EFFECT_IDS

    receipt = {
        "receipt_id": "receipt-1",
        "receipt_digest": _SHA,
        "effect_refs": [{"kind": kind, "digest": _SHA} for kind in HEALING_EFFECT_IDS],
    }
    output = coverage_agent_output()
    output["effect_refs"] = [{"kind": "forged.healing.effect", "digest": _SHA}]
    published = publish_repair(coverage_graph_input(), output, receipt)
    effect_refs = cast(list[dict[str, object]], published["effect_refs"])
    assert {item["kind"] for item in effect_refs} == set(HEALING_EFFECT_IDS)
    assert "forged.healing.effect" not in {item["kind"] for item in effect_refs}


def test_healing_graphs_do_not_import_foreign_graphs_or_implementation() -> None:
    violations: list[str] = []
    for path in sorted(_GRAPHS_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(_GRAPHS_ROOT.parent)
        module_name = "assurance_healing." + ".".join(relative.with_suffix("").parts)
        if module_name.endswith(".__init__"):
            module_name = module_name[: -len(".__init__")]
        for imported in _imported_names(path, module_name=module_name):
            parts = imported.split(".")
            if any(part in _FORBIDDEN_IMPLEMENTATION for part in parts):
                violations.append(f"{path}:{imported}")
            if parts[0] in _FOREIGN_GRAPH_OWNERS and "graphs" in parts:
                violations.append(f"{path}:{imported}")
            if parts[0] in _FOREIGN_GRAPH_OWNERS and parts[1:2] != ["contracts"]:
                violations.append(f"{path}:{imported}")
    assert violations == []


async def test_not_eligible_and_exhausted_are_business_terminals() -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.healing", contracts=healing_contracts())
    bundle = build_healing_graphs(context)
    not_eligible = await harness.run(
        bundle.repair_failure,
        input=failure_graph_input(classification="product_bug"),
        script={},
    )
    assert not_eligible.semantic_calls == ()
    assert not_eligible.terminal is not None
    not_eligible_terminal = not_eligible.terminal
    assert isinstance(not_eligible_terminal, dict)
    assert not_eligible_terminal["status"] == "not_eligible"

    exhausted = await harness.run(
        bundle.repair_coverage,
        input=coverage_graph_input(rounds_used=4, rounds_budget=4),
        script={},
    )
    assert exhausted.semantic_calls == ()
    assert exhausted.terminal is not None
    exhausted_terminal = exhausted.terminal
    assert isinstance(exhausted_terminal, dict)
    assert exhausted_terminal["status"] == "exhausted"
    assert exhausted_terminal["rounds_used"] == 4


async def test_rejected_fix_proposal_terminates_failed_not_repaired() -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.healing", contracts=healing_contracts())
    bundle = build_healing_graphs(context)
    result = await harness.run(
        bundle.repair_failure,
        input=failure_graph_input(),
        script={"healing.fix-proposal": [RejectedTaskResult(reason="kernel rejected")]},
    )
    assert result.promotion_decision == "rejected"
    assert result.published_update is None
    assert result.terminal is not None
    terminal = result.terminal
    assert isinstance(terminal, dict)
    assert terminal["status"] == "failed"
    assert terminal["status"] != "repaired"
    assert terminal.get("attempt_failure") == {
        "resolution_kind": "rejected",
        "reason": "kernel rejected",
        "writes_promoted": False,
    }


@pytest.mark.parametrize("status", ("in_progress", "unknown", None))
async def test_coverage_missing_unknown_in_progress_finalize_fails_closed(status: str | None) -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.healing", contracts=healing_contracts())
    bundle = build_healing_graphs(context)
    output = coverage_agent_output()
    if status is None:
        del output["status"]
    else:
        output["status"] = status
    result = await harness.run(
        bundle.repair_coverage,
        input=coverage_graph_input(),
        script={
            "healing.coverage-repair": [committed(output, ReceiptRef(receipt_id="r1", receipt_digest=_SHA))]
        },
    )
    assert result.terminal is not None
    terminal = result.terminal
    assert isinstance(terminal, dict)
    assert terminal["status"] == "failed"
    assert terminal["status"] != "repaired"


async def test_committed_fix_proposal_without_application_cannot_terminate_applied() -> None:
    from graph_engine.attempts.resolutions import RejectedTaskResult

    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.healing", contracts=healing_contracts())
    bundle = build_healing_graphs(context)
    output = FixProposalResultV1(
        schema_version="1",
        change_id="CH-FIX-001",
        summary=FixProposalSummary(eligible_count=1),
    ).model_dump()
    assert "status" not in output
    result = await harness.run(
        bundle.repair_failure,
        input=failure_graph_input(),
        script={
            "healing.fix-proposal": [committed(output, ReceiptRef(receipt_id="r1", receipt_digest=_SHA))],
            "healing.apply-test-repair": [RejectedTaskResult(reason="no committed test change")],
        },
    )
    published = result.published_update
    assert published is not None
    assert "status" not in published
    assert result.terminal is not None
    terminal = result.terminal
    assert isinstance(terminal, dict)
    assert terminal["status"] == "failed"


@pytest.mark.parametrize("status", ("failed", "needs_review", "repaired", "not_eligible", "exhausted"))
async def test_coverage_finalize_routes_known_statuses(status: str) -> None:
    harness = GraphHarness()
    context = harness.recording_context(owner_id="assurance.healing", contracts=healing_contracts())
    bundle = build_healing_graphs(context)
    result = await harness.run(
        bundle.repair_coverage,
        input=coverage_graph_input(),
        script={
            "healing.coverage-repair": [
                committed(
                    coverage_agent_output(status=status), ReceiptRef(receipt_id="r1", receipt_digest=_SHA)
                )
            ]
        },
    )
    if status == "needs_review":
        assert result.interrupt_envelope is not None
        assert result.terminal is None
        return
    assert result.terminal is not None
    terminal = result.terminal
    assert isinstance(terminal, dict)
    assert terminal["status"] == status


def test_healing_contracts_keep_empty_validators() -> None:
    contracts: dict[str, TaskAttemptContract[Any, Any]] = healing_contracts()
    assert all(item.validators == () for item in contracts.values())
    assert set(AGENT_JOB_CONTRACTS) == {
        "apply-test-repair",
        "coverage-repair",
        "fix-proposal",
    }
