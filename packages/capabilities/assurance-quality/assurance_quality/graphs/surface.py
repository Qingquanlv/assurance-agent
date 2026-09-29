from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from graph_engine.attempts.keys import BusinessActivation
from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.stategraph import add_attempt_node

from assurance_quality.contracts.surface import SurfaceProbeInputV1, SurfaceProbeResultV1
from assurance_quality.graphs.nodes import terminal_done
from assurance_quality.graphs.routes import route_attempt
from assurance_quality.graphs.state import QualityState

_SURFACE_BASELINE_ID = "assurance.quality.surface-baseline"


def select_surface_baseline(state: Mapping[str, object]) -> SurfaceProbeInputV1:
    return SurfaceProbeInputV1.model_validate(
        {
            "change_id": state["change_id"],
            "candidate_test_families": state.get("candidate_test_families") or (),
            "api_base_url": state.get("api_base_url"),
            "ui_base_url": state.get("ui_base_url"),
            "ui_paths": state.get("ui_paths") or (),
        }
    )


def activation_surface_baseline(state: Mapping[str, object]) -> BusinessActivation:
    del state
    return BusinessActivation.one_shot()


def publish_surface_baseline(
    state: Mapping[str, object],
    output: object,
    receipt: object,
) -> dict[str, object]:
    del state, receipt
    result = SurfaceProbeResultV1.model_validate(output)
    return {
        "ui_exploration_ref": result.ui_exploration_ref.model_dump(mode="json"),
        "api_discovery_ref": result.api_discovery_ref.model_dump(mode="json"),
    }


def build_surface_baseline_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: StateGraph[QualityState] = StateGraph(QualityState)
    add_attempt_node(
        builder,
        context,
        "quality.surface-baseline",
        contract_id=_SURFACE_BASELINE_ID,
        activation=activation_surface_baseline,
        select=select_surface_baseline,
        publish=publish_surface_baseline,
    )
    builder.add_node("done", cast(Callable[..., Any], terminal_done))
    builder.add_node("failed", cast(Callable[..., Any], terminal_done))
    builder.add_edge(START, "quality.surface-baseline")
    builder.add_conditional_edges(
        "quality.surface-baseline",
        cast(Callable[..., Any], route_attempt),
        {"ready": "done", "failed": "failed"},
    )
    builder.add_edge("done", END)
    builder.add_edge("failed", END)
    return context.compile_subgraph(builder)


__all__ = ["build_surface_baseline_graph"]
