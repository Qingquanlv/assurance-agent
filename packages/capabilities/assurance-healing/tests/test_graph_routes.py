from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from assurance_healing.contracts.agent import FixProposalResultV1
from assurance_healing.contracts.proposal import FixProposalSummary
from assurance_healing.contracts.attempts import AGENT_JOB_CONTRACTS
from assurance_healing.graphs.factory import build_healing_graphs as _build_healing_graphs
from graph_engine.attempts.contracts import TaskAttemptContract
from graph_engine.attempts.resolutions import ReceiptRef, RejectedTaskResult
from graph_engine.testing import GraphHarness, committed

from test_healing_graph_factory import (  # type: ignore[import-not-found]
    failure_graph_input,
    healing_contracts,
)

from graph_engine.testing.feature_bundle import compile_bundle


def build_healing_graphs(*args, **kwargs):
    return compile_bundle(_build_healing_graphs(*args, **kwargs))


_GRAPHS_ROOT = Path(__file__).resolve().parents[1] / "assurance_healing" / "graphs"
_ROUTE_PATHS = ()
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


def test_repair_flow_has_no_handwritten_route_functions() -> None:
    source = (_GRAPHS_ROOT / "failure.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="failure.py")
    names = [node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)]
    assert [name for name in names if name.startswith("route_")] == []


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
    envelope = result.interrupt_envelope
    assert envelope is not None
    assert result.terminal is None
    assert [call.semantic_node_id for call in result.semantic_calls] == ["healing.fix-proposal"]


def test_healing_contracts_keep_empty_validators() -> None:
    contracts: dict[str, TaskAttemptContract[Any, Any]] = healing_contracts()
    assert all(item.validators == () for item in contracts.values())
    assert set(AGENT_JOB_CONTRACTS) == {
        "apply-test-repair",
        "fix-proposal",
    }
