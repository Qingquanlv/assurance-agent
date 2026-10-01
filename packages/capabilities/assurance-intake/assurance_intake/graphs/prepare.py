from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from langgraph.graph import END, START
from langgraph.graph.state import CompiledStateGraph

from assurance_intake.graphs.calls import (
    activation_one_shot,
    publish_artifacts,
    publish_plan,
    select_explore,
    select_intake,
    select_resolve_plan,
)
from assurance_intake.graphs.state import IntakeState, terminal_failed
from assurance_intake.ops.explore import op as explore
from assurance_intake.ops.intake import op as intake
from assurance_intake.ops.resolve_plan import op as resolve_plan
from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.stategraph import AttemptGraph


def _node(fn: object) -> Callable[..., Any]:
    return cast(Callable[..., Any], fn)


def terminal_prepared(state: Mapping[str, object]) -> dict[str, object]:
    del state
    return {"status": "prepared"}


def build_prepare_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: AttemptGraph[IntakeState] = AttemptGraph(
        IntakeState,
        context,
        namespace="intake",
        activation=activation_one_shot,
    )
    builder.add_attempt("intake", intake, select=select_intake, publish=publish_artifacts)
    builder.add_attempt("explore", explore, select=select_explore, publish=publish_artifacts)
    builder.add_attempt(
        "resolve-plan",
        resolve_plan,
        select=select_resolve_plan,
        publish=publish_plan,
    )
    builder.add_node("prepared", _node(terminal_prepared))
    builder.add_node("failed", _node(terminal_failed))
    builder.add_edge(START, "intake")
    builder.add_attempt_edge("intake", "explore", on_failure="failed")
    builder.add_attempt_edge("explore", "resolve-plan", on_failure="failed")
    builder.add_attempt_edge("resolve-plan", "prepared", on_failure="failed")
    builder.add_edge("prepared", END)
    builder.add_edge("failed", END)
    return builder.compile_subgraph()


__all__ = ["build_prepare_graph", "terminal_prepared"]
