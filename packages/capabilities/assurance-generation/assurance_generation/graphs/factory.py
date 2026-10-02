from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from langgraph.graph import END, START
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send

from assurance_generation.contracts.attempts import TASK_ATTEMPT_CONTRACTS
from assurance_generation.contracts.families import GENERATION_FAMILIES, validate_selected_families
from assurance_generation.feature import GenerationGraphs
from assurance_generation.graphs.api import compile_family_pair
from assurance_generation.graphs.init_runtime import build_init_runtime_graph, route_attempt_result
from assurance_generation.graphs.nodes import (
    activation_generation_cycle,
    activation_generation_inputs,
    complete_generation_node,
    generation_done,
    join_selected,
    publish_generation_cycle,
    publish_generation_inputs,
    route_generation_completion,
    select_generation_cycle,
    select_generation_inputs,
)
from assurance_generation.graphs.state import GenerationState
from graph_engine.boot.boot import CapabilityBuildContext
from graph_engine.errors import GraphEngineError
from graph_engine.stategraph import AttemptGraph


def _node(fn: object) -> Callable[..., Any]:
    return cast(Callable[..., Any], fn)


class InsufficientRouteMatches(GraphEngineError):
    """Raised when Generation fanout cannot emit all four family Sends."""


def family_select_named_matches(state: Mapping[str, object], family: str) -> dict[str, str | None]:
    selected = state.get("selected_test_families")
    names = selected if isinstance(selected, list | tuple) else ()
    return {"selected": family if family in names else None}


def route_families(state: Mapping[str, object]) -> list[Send]:
    raw = state.get("selected_test_families")
    try:
        if not isinstance(raw, list | tuple):
            raise ValueError("selected_test_families is missing")
        selected = validate_selected_families(list(raw))
    except (TypeError, ValueError) as error:
        raise InsufficientRouteMatches(str(error)) from error
    destinations = GENERATION_FAMILIES
    if len(destinations) != 4:
        raise InsufficientRouteMatches("generation fanout requires four family destinations")
    sends: list[Send] = []
    for family in destinations:
        sends.append(
            Send(
                family,
                {
                    "change_id": state.get("change_id"),
                    "plan_digest": state.get("plan_digest"),
                    "plan_ref": state.get("plan_ref"),
                    "coverage_epoch": state.get("coverage_epoch", 0),
                    "reviewed_case": state.get("reviewed_case"),
                    "selected_test_families": list(selected),
                    "capability_leafs": state.get("capability_leafs"),
                    "allowed_artifact_paths": state.get("allowed_artifact_paths"),
                    "family": family,
                    "lane_selected": family in selected,
                    "rounds_used": 0,
                    "rounds_budget": 3,
                    "review_stage": "codegen",
                },
            )
        )
    return sends


def _build_resolve_inputs_graph(context: CapabilityBuildContext) -> CompiledStateGraph:
    builder: AttemptGraph[GenerationState] = AttemptGraph(
        GenerationState,
        context,
        namespace="generation",
    )
    builder.add_attempt(
        "generation.resolve-inputs",
        TASK_ATTEMPT_CONTRACTS["resolve-inputs"],
        select=select_generation_inputs,
        publish=publish_generation_inputs,
        activation=activation_generation_inputs,
        semantic_node_id="generation.resolve-inputs",
    )
    builder.add_node(
        "done",
        _node(lambda state: {"status": "failed" if state.get("attempt_failure") else "completed"}),
    )
    builder.add_edge(START, "generation.resolve-inputs")
    builder.add_edge("generation.resolve-inputs", "done")
    builder.add_edge("done", END)
    return builder.compile_subgraph()


def _build_root_graph(
    context: CapabilityBuildContext,
    *,
    api: CompiledStateGraph,
    e2e: CompiledStateGraph,
    fuzz: CompiledStateGraph,
    performance: CompiledStateGraph,
) -> CompiledStateGraph:
    builder: AttemptGraph[GenerationState] = AttemptGraph(
        GenerationState,
        context,
        namespace="generation",
    )
    builder.add_attempt(
        "generation.resolve-inputs",
        TASK_ATTEMPT_CONTRACTS["resolve-inputs"],
        select=select_generation_inputs,
        publish=publish_generation_inputs,
        activation=activation_generation_inputs,
        semantic_node_id="generation.resolve-inputs",
    )
    builder.add_node("fanout", _node(lambda _state: {}))
    builder.add_node("api", api)
    builder.add_node("e2e", e2e)
    builder.add_node("fuzz", fuzz)
    builder.add_node("performance", performance)
    builder.add_node("join-selected", _node(join_selected))
    builder.add_node("complete", _node(complete_generation_node))
    builder.add_attempt(
        "generation.publish-cycle",
        TASK_ATTEMPT_CONTRACTS["publish-cycle"],
        select=select_generation_cycle,
        publish=publish_generation_cycle,
        activation=activation_generation_cycle,
        semantic_node_id="generation.publish-cycle",
    )
    builder.add_node("done", _node(generation_done))
    builder.add_edge(START, "generation.resolve-inputs")
    builder.add_conditional_edges(
        "generation.resolve-inputs",
        _node(route_attempt_result),
        {"committed": "fanout", "failed": "done"},
    )
    builder.add_conditional_edges("fanout", _node(route_families))
    builder.add_edge("api", "join-selected")
    builder.add_edge("e2e", "join-selected")
    builder.add_edge("fuzz", "join-selected")
    builder.add_edge("performance", "join-selected")
    builder.add_edge("join-selected", "complete")
    builder.add_conditional_edges(
        "complete",
        _node(route_generation_completion),
        {"publish": "generation.publish-cycle", "failed": "done"},
    )
    builder.add_edge("generation.publish-cycle", "done")
    builder.add_edge("done", END)
    return builder.compile_subgraph()


def build_generation_graphs(context: CapabilityBuildContext) -> GenerationGraphs:
    api, api_lane = compile_family_pair(context, "api")
    e2e, e2e_lane = compile_family_pair(context, "e2e")
    fuzz, fuzz_lane = compile_family_pair(context, "fuzz")
    performance, performance_lane = compile_family_pair(context, "performance")
    return GenerationGraphs(
        generation=_build_root_graph(
            context,
            api=api_lane,
            e2e=e2e_lane,
            fuzz=fuzz_lane,
            performance=performance_lane,
        ),
        api=api,
        e2e=e2e,
        fuzz=fuzz,
        performance=performance,
        init_runtime=build_init_runtime_graph(context),
        resolve_inputs=_build_resolve_inputs_graph(context),
    )


__all__ = [
    "GenerationGraphs",
    "InsufficientRouteMatches",
    "build_generation_graphs",
    "family_select_named_matches",
    "route_families",
]
