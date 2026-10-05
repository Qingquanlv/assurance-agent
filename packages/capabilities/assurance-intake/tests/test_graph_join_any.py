"""The case flow's review cycle is the intake loop, without a round-advance node."""

from __future__ import annotations

from typing import Any

from graph_engine.attempts.contracts import TaskAttemptContract
from graph_engine.testing import GraphHarness

from assurance_intake.feature import AGENT_JOB_CONTRACTS, TASK_ATTEMPT_CONTRACTS
from assurance_intake.graphs.factory import build_intake_graphs as _build_intake_graphs
from tests.architecture.loop_scc_inventory import LOOP_SCC_INVENTORY

from graph_engine.testing.feature_bundle import compile_bundle


def build_intake_graphs(*args, **kwargs):
    return compile_bundle(_build_intake_graphs(*args, **kwargs))


def _contracts() -> dict[str, TaskAttemptContract[Any, Any]]:
    return {
        **{contract.contract_id: contract.to_task_contract() for contract in AGENT_JOB_CONTRACTS.values()},
        **{contract.contract_id: contract for contract in TASK_ATTEMPT_CONTRACTS.values()},
    }


def _cycle_members(edges: set[tuple[str, str]]) -> set[str]:
    nodes = {node for edge in edges for node in edge if node not in {"__start__", "__end__"}}

    def reachable(start: str) -> set[str]:
        seen: set[str] = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(
                target
                for source, target in edges
                if source == current and target not in seen and target not in {"__start__", "__end__"}
            )
        return seen

    forward = {node: reachable(node) for node in nodes}
    return {
        node
        for node in nodes
        if any(other != node and node in forward[other] and other in forward[node] for other in nodes)
    }


def test_case_loop_reenters_through_the_review_cycle() -> None:
    harness = GraphHarness()
    bundle = build_intake_graphs(
        harness.recording_context(owner_id="assurance.intake", contracts=_contracts())
    )
    assert "review-round-advance" not in bundle.case.nodes
    edges = {(str(edge.source), str(edge.target)) for edge in bundle.case.get_graph().edges}
    assert ("case-design", "case-review") in edges
    assert ("case-repair", "case-review") in edges
    assert ("human-review", "case-design") in edges
    assert ("case-design", "failed") in edges
    assert ("case-repair", "exhausted") in edges
    assert ("case-review", "exhausted") in edges
    row = next(
        item for item in LOOP_SCC_INVENTORY if item.graph_id == "assurance.intake.workflow.graph.entry"
    )
    assert set(row.membership) == _cycle_members(edges)
