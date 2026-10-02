from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from langgraph.graph import END, START
from langgraph.graph.state import CompiledStateGraph

from graph_engine.attempts.keys import BusinessActivation
from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.stategraph import AttemptGraph

from assurance_quality.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from assurance_quality.contracts.surface import SurfaceProbeInputV1, SurfaceProbeResultV1
from assurance_quality.graphs.issues import route_attempt
from assurance_quality.graphs.nodes import terminal_done
from assurance_quality.graphs.state import QualityState

_ATTEMPT_TARGETS = ("done", "failed")


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
        "ui_exploration_source": result.ui_source,
        "api_discovery_source": result.api_source,
    }


def _node(fn: object) -> Callable[..., Any]:
    return cast(Callable[..., Any], fn)


def build_surface_baseline_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: AttemptGraph[QualityState] = AttemptGraph(QualityState, context, namespace="quality")
    builder.add_attempt(
        "quality.surface-baseline",
        TASK_ATTEMPT_CONTRACTS["surface-baseline"],
        select=select_surface_baseline,
        publish=publish_surface_baseline,
        activation=activation_surface_baseline,
        semantic_node_id="quality.surface-baseline",
    )
    builder.add_node("done", _node(terminal_done))
    builder.add_node("failed", _node(terminal_done))
    builder.add_edge(START, "quality.surface-baseline")
    builder.add_route(
        "quality.surface-baseline",
        route_attempt("done"),
        targets=_ATTEMPT_TARGETS,
    )
    builder.add_edge("done", END)
    builder.add_edge("failed", END)
    return builder.compile_subgraph()


__all__ = ["build_surface_baseline_graph"]
