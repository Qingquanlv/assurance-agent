from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from langgraph.graph import END
from langgraph.graph.state import CompiledStateGraph

from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.stategraph import AttemptGraph
from graph_engine.stategraph.routing import select_exclusive_route

from assurance_healing.contracts.coverage_repair import HEALING_REPAIR_OUTCOMES
from assurance_healing.graphs.failure import _add_shared_terminals, _as_int, _exhausted
from assurance_healing.graphs.nodes import (
    activation_repair,
    coverage_review,
    publish_repair,
    select_coverage,
)
from assurance_healing.graphs.state import HealingState

_COVERAGE_ID = "assurance.healing.agent.coverage-repair.v1"
_COVERAGE_NODE = "healing.coverage-repair"
_ADMIT_OTHERWISE = "not-eligible"
_ADMIT_ADVANCE = "repair-round-advance"
_ADMIT_EXHAUSTED = "exhausted"
_ADMIT_TARGETS = (_ADMIT_ADVANCE, _ADMIT_EXHAUSTED, _ADMIT_OTHERWISE)
_STATUS_OTHERWISE = "failed"
_STATUS_TARGETS: dict[str, str] = {
    "repaired": "done",
    "needs_review": "needs-review",
    "exhausted": _ADMIT_EXHAUSTED,
    "not_eligible": "not-eligible",
    "failed": _STATUS_OTHERWISE,
}
_COVERAGE_STATUS_TARGETS = (*_STATUS_TARGETS.values(), _STATUS_OTHERWISE)


def _node(fn: object) -> Callable[..., Any]:
    return cast(Callable[..., Any], fn)


def _coverage_eligible(state: Mapping[str, object]) -> bool:
    used = _as_int(state.get("rounds_used"))
    budget = _as_int(state.get("rounds_budget"))
    if used is None or budget is None:
        return False
    return state.get("fix_eligible") is True and used < budget


_ADMIT_COVERAGE_TABLE: dict[str, Callable[[Mapping[str, object]], bool]] = {
    "advance": _coverage_eligible,
    "exhausted": _exhausted,
}
_ADMIT_COVERAGE_NODES = {
    "advance": _ADMIT_ADVANCE,
    "exhausted": _ADMIT_EXHAUSTED,
}


def admit_coverage_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    return {
        label: _ADMIT_COVERAGE_NODES[label] if predicate(state) else None
        for label, predicate in _ADMIT_COVERAGE_TABLE.items()
    }


def route_admit_coverage(state: Mapping[str, object]) -> str:
    return select_exclusive_route(admit_coverage_named_matches(state), otherwise=_ADMIT_OTHERWISE)


def coverage_status_named_matches(state: Mapping[str, object]) -> dict[str, str | None]:
    current = state.get("status")
    return {name: _STATUS_TARGETS[name] if current == name else None for name in HEALING_REPAIR_OUTCOMES}


def route_coverage_status(state: Mapping[str, object]) -> str:
    if state.get("attempt_failure"):
        return _STATUS_OTHERWISE
    return select_exclusive_route(coverage_status_named_matches(state), otherwise=_STATUS_OTHERWISE)


def build_repair_coverage_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: AttemptGraph[HealingState] = AttemptGraph(
        HealingState,
        context,
        namespace="healing",
        activation=activation_repair,
    )
    _add_shared_terminals(builder)
    builder.add_attempt(
        _COVERAGE_NODE,
        _COVERAGE_ID,
        select=select_coverage,
        publish=publish_repair,
        activation=activation_repair,
        semantic_node_id=_COVERAGE_NODE,
    )
    builder.add_node("needs-review", _node(coverage_review))
    builder.add_route("admit", route_admit_coverage, targets=_ADMIT_TARGETS)
    builder.add_edge("repair-round-advance", _COVERAGE_NODE)
    builder.add_route(_COVERAGE_NODE, route_coverage_status, targets=_COVERAGE_STATUS_TARGETS)
    builder.add_edge("needs-review", END)
    return builder.compile_subgraph()


__all__ = [
    "admit_coverage_named_matches",
    "build_repair_coverage_graph",
    "coverage_status_named_matches",
    "route_admit_coverage",
    "route_coverage_status",
]
