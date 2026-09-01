from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot.boot import CapabilityBuildContext

from assurance_healing.graphs.nodes import (
    activation_repair,
    admit_passthrough,
    advance_repair_round_node,
    coverage_review,
    publish_repair,
    select_coverage,
    select_failure,
    terminal_done,
    terminal_exhausted,
    terminal_failed,
    terminal_not_eligible,
)
from assurance_healing.graphs.routes import route_admit_coverage, route_admit_failure, route_coverage_status
from assurance_healing.graphs.state import HealingState

_COVERAGE_ID = "assurance.healing.agent.coverage-repair.v1"
_FIX_PROPOSAL_ID = "assurance.healing.agent.fix-proposal.v1"
_ADMIT_PATHS: dict[Hashable, str] = {
    "repair-round-advance": "repair-round-advance",
    "exhausted": "exhausted",
    "not-eligible": "not-eligible",
}
_COVERAGE_STATUS_PATHS: dict[Hashable, str] = {
    "done": "done",
    "needs-review": "needs-review",
    "exhausted": "exhausted",
    "not-eligible": "not-eligible",
    "failed": "failed",
}


@dataclass(frozen=True, slots=True)
class HealingGraphs:
    repair_failure: CompiledStateGraph
    repair_coverage: CompiledStateGraph


def build_healing_graphs(context: CapabilityBuildContext) -> HealingGraphs:
    return HealingGraphs(
        repair_failure=_build_repair_failure_graph(context),
        repair_coverage=_build_repair_coverage_graph(context),
    )


def _add_shared_terminals(builder: StateGraph[HealingState]) -> None:
    builder.add_node("admit", cast(Callable[..., Any], admit_passthrough))
    builder.add_node("repair-round-advance", cast(Callable[..., Any], advance_repair_round_node))
    builder.add_node("done", cast(Callable[..., Any], terminal_done))
    builder.add_node("exhausted", cast(Callable[..., Any], terminal_exhausted))
    builder.add_node("not-eligible", cast(Callable[..., Any], terminal_not_eligible))
    builder.add_edge(START, "admit")
    builder.add_edge("done", END)
    builder.add_edge("exhausted", END)
    builder.add_edge("not-eligible", END)


def _build_repair_failure_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: StateGraph[HealingState] = StateGraph(HealingState)
    _add_shared_terminals(builder)
    builder.add_node(
        "healing.fix-proposal",
        cast(
            Callable[..., Any],
            context.attempt(
                _FIX_PROPOSAL_ID,
                semantic_node_id="healing.fix-proposal",
                activation=activation_repair,
                select=select_failure,
                publish=publish_repair,
            ),
        ),
    )
    builder.add_conditional_edges(
        "admit",
        cast(Callable[..., Any], route_admit_failure),
        _ADMIT_PATHS,
    )
    builder.add_edge("repair-round-advance", "healing.fix-proposal")
    builder.add_edge("healing.fix-proposal", "done")
    return context.compile_subgraph(builder)


def _build_repair_coverage_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: StateGraph[HealingState] = StateGraph(HealingState)
    _add_shared_terminals(builder)
    builder.add_node(
        "healing.coverage-repair",
        cast(
            Callable[..., Any],
            context.attempt(
                _COVERAGE_ID,
                semantic_node_id="healing.coverage-repair",
                activation=activation_repair,
                select=select_coverage,
                publish=publish_repair,
            ),
        ),
    )
    builder.add_node("needs-review", cast(Callable[..., Any], coverage_review))
    builder.add_node("failed", cast(Callable[..., Any], terminal_failed))
    builder.add_conditional_edges(
        "admit",
        cast(Callable[..., Any], route_admit_coverage),
        _ADMIT_PATHS,
    )
    builder.add_edge("repair-round-advance", "healing.coverage-repair")
    builder.add_conditional_edges(
        "healing.coverage-repair",
        cast(Callable[..., Any], route_coverage_status),
        _COVERAGE_STATUS_PATHS,
    )
    builder.add_edge("needs-review", END)
    builder.add_edge("failed", END)
    return context.compile_subgraph(builder)


__all__ = ["HealingGraphs", "build_healing_graphs"]
