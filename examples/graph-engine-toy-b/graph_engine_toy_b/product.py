from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from graph_engine import ENGINE_API_VERSION
from graph_engine.attempts.keys import BusinessActivation
from graph_engine.boot.boot import CapabilityBuildContext, GraphBuildContext
from graph_engine.boot.generic import entrypoint_digest
from graph_engine.boot.graph_revision import EntrypointGraphContract
from graph_engine.composition import PluginRequirement, ProductManifest
from graph_engine.plugin_api import ProviderSource

from graph_engine_toy_b.contracts import (
    CHILD_CONTRACT,
    COMBINE_CONTRACT,
    EmptyInput,
    LEFT_CONTRACT,
    SEED_CONTRACT,
)

_SOURCE = ProviderSource(
    distribution="graph-engine-toy-b",
    version="1.0.0",
    entrypoint_group="graph_engine.products",
    entrypoint_name="toy-b",
    entrypoint_value="graph_engine_toy_b.product:ToyBProduct",
    declaration_path="graph_engine_toy_b/product-declaration.json",
    import_roots=("", "graph_engine_toy_b"),
)
_FACTORY_SYMBOL = "graph_engine_toy_b.product:build_toy_b_graphs"


class ToyBState(TypedDict, total=False):
    route: str
    left: bool
    child: bool
    combined: bool
    left_round: int
    attempt_failure: dict[str, object]
    review: str


@dataclass(frozen=True, slots=True)
class ToyBGraphs:
    entrypoints: Mapping[str, CompiledStateGraph]
    contracts: Mapping[str, EntrypointGraphContract]


def _select_empty(_state: ToyBState) -> EmptyInput:
    return EmptyInput()


def _publish_seed(_state: ToyBState, output: object, _receipt: object) -> dict[str, object]:
    return {"route": getattr(output, "route", "both"), "attempt_failure": None}


def _publish_left(_state: ToyBState, output: object, _receipt: object) -> dict[str, object]:
    return {"left": bool(getattr(output, "left", True)), "attempt_failure": None}


def _publish_child(_state: ToyBState, output: object, _receipt: object) -> dict[str, object]:
    return {"child": bool(getattr(output, "child", True)), "attempt_failure": None}


def _publish_combine(_state: ToyBState, output: object, _receipt: object) -> dict[str, object]:
    return {"combined": bool(getattr(output, "combined", True)), "attempt_failure": None}


def _activation_left(state: ToyBState) -> BusinessActivation:
    return BusinessActivation.for_round(int(state.get("left_round") or 0))


def _route_after_left(state: ToyBState) -> Literal["left", "child"]:
    failure = state.get("attempt_failure")
    if isinstance(failure, Mapping) and int(state.get("left_round") or 0) == 0:
        return "left"
    return "child"


def _bump_left_round(state: ToyBState) -> ToyBState:
    return {"left_round": int(state.get("left_round") or 0) + 1, "attempt_failure": None}


def _review(state: ToyBState) -> dict[str, object]:
    decision = interrupt(
        {
            "kind": "human",
            "reason": "review combined result",
            "actions": ["approve", "reject"],
        }
    )
    return {"review": str(decision)}


def _add_attempt(
    capability: CapabilityBuildContext,
    builder: StateGraph[ToyBState],
    *,
    name: str,
    contract_id: str,
    activation: object,
    publish: object,
) -> None:
    builder.add_node(
        name,
        cast(
            object,
            capability.attempt(
                contract_id,
                semantic_node_id=name,
                activation=activation,
                select=_select_empty,
                publish=publish,
            ),
        ),
    )


def build_toy_b_graphs(
    context: GraphBuildContext, features: Mapping[str, object] | None = None
) -> ToyBGraphs:
    del features
    capability = context.for_capability("toy.b")
    child_builder: StateGraph[ToyBState] = StateGraph(ToyBState)
    _add_attempt(
        capability,
        child_builder,
        name="child",
        contract_id=CHILD_CONTRACT.contract_id,
        activation=BusinessActivation.one_shot(),
        publish=_publish_child,
    )
    child_builder.add_edge(START, "child")
    child_builder.add_edge("child", END)
    child_graph = capability.compile_subgraph(child_builder)

    builder: StateGraph[ToyBState] = StateGraph(ToyBState)
    _add_attempt(
        capability,
        builder,
        name="seed",
        contract_id=SEED_CONTRACT.contract_id,
        activation=BusinessActivation.one_shot(),
        publish=_publish_seed,
    )
    _add_attempt(
        capability,
        builder,
        name="left",
        contract_id=LEFT_CONTRACT.contract_id,
        activation=_activation_left,
        publish=_publish_left,
    )
    builder.add_node("bump-left-round", _bump_left_round)
    builder.add_node("child-subgraph", child_graph)
    _add_attempt(
        capability,
        builder,
        name="combine",
        contract_id=COMBINE_CONTRACT.contract_id,
        activation=BusinessActivation.one_shot(),
        publish=_publish_combine,
    )
    builder.add_node("review", _review)
    builder.add_edge(START, "seed")
    builder.add_edge("seed", "left")
    builder.add_conditional_edges(
        "left",
        _route_after_left,
        {"left": "bump-left-round", "child": "child-subgraph"},
    )
    builder.add_edge("bump-left-round", "left")
    builder.add_edge("child-subgraph", "combine")
    builder.add_edge("combine", "review")
    builder.add_edge("review", END)
    entrypoints = {"review": context.compile_root(builder)}
    contracts = {
        "review": EntrypointGraphContract(
            name="review",
            input_model="graph_engine_toy_b.product.ToyBState",
            output_model="graph_engine_toy_b.product.ToyBState",
            state_model="graph_engine_toy_b.product.ToyBState",
            input_schema_digest=entrypoint_digest("review", "input"),
            output_schema_digest=entrypoint_digest("review", "output"),
            state_schema_digest=entrypoint_digest("review", "state"),
            state_schema_version="1",
            recursion_limit=64,
        )
    }
    return ToyBGraphs(entrypoints=entrypoints, contracts=contracts)


class ToyBProduct:
    @staticmethod
    def manifest() -> ProductManifest:
        return ProductManifest(
            schema_version="1",
            source=_SOURCE,
            product_id="toy.b",
            product_version="1.0.0",
            engine_api=ENGINE_API_VERSION,
            plugins=(PluginRequirement(plugin_id="toy.b", version_specifier="==1.0.0"),),
            entrypoints={"review": "root"},
            configuration={},
            graph_factory_symbol=_FACTORY_SYMBOL,
        )
