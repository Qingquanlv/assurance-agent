from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypedDict, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from graph_engine import ENGINE_API_VERSION
from graph_engine.attempts.keys import BusinessActivation
from graph_engine.boot.boot import GraphBuildContext
from graph_engine.boot.generic import entrypoint_digest
from graph_engine.boot.graph_revision import EntrypointGraphContract
from graph_engine.composition import PluginRequirement, ProductManifest
from graph_engine.plugin_api import ProviderSource

from graph_engine_toy_a.contracts import GREET_CONTRACT, GreetInput

_SOURCE = ProviderSource(
    distribution="graph-engine-toy-a",
    version="1.0.0",
    entrypoint_group="graph_engine.products",
    entrypoint_name="toy-a",
    entrypoint_value="graph_engine_toy_a.product:ToyAProduct",
    declaration_path="graph_engine_toy_a/product-declaration.json",
    import_roots=("", "graph_engine_toy_a"),
)
_FACTORY_SYMBOL = "graph_engine_toy_a.product:build_toy_a_graphs"


class ToyAState(TypedDict, total=False):
    name: str
    message: str
    attempt_failure: object


@dataclass(frozen=True, slots=True)
class ToyAGraphs:
    entrypoints: Mapping[str, CompiledStateGraph]
    contracts: Mapping[str, EntrypointGraphContract]


def _select_greet(state: ToyAState) -> GreetInput:
    return GreetInput(name=str(state.get("name") or "Ada"))


def _publish_greet(_state: ToyAState, output: object, _receipt: object) -> dict[str, object]:
    message = getattr(output, "message", None)
    if message is None and isinstance(output, Mapping):
        message = output.get("message")
    return {"message": str(message)}


def build_toy_a_graphs(
    context: GraphBuildContext, features: Mapping[str, object] | None = None
) -> ToyAGraphs:
    del features
    capability = context.for_capability("toy.a")
    builder: StateGraph[ToyAState] = StateGraph(ToyAState)
    builder.add_node(
        "greet",
        cast(
            object,
            capability.attempt(
                GREET_CONTRACT.contract_id,
                semantic_node_id="greet",
                activation=BusinessActivation.one_shot(),
                select=_select_greet,
                publish=_publish_greet,
            ),
        ),
    )
    builder.add_edge(START, "greet")
    builder.add_edge("greet", END)
    entrypoints = {"hello": context.compile_root(builder)}
    contracts = {
        "hello": EntrypointGraphContract(
            name="hello",
            input_model="graph_engine_toy_a.product.ToyAState",
            output_model="graph_engine_toy_a.product.ToyAState",
            state_model="graph_engine_toy_a.product.ToyAState",
            input_schema_digest=entrypoint_digest("hello", "input"),
            output_schema_digest=entrypoint_digest("hello", "output"),
            state_schema_digest=entrypoint_digest("hello", "state"),
            state_schema_version="1",
            recursion_limit=32,
        )
    }
    return ToyAGraphs(entrypoints=entrypoints, contracts=contracts)


class ToyAProduct:
    @staticmethod
    def manifest() -> ProductManifest:
        return ProductManifest(
            schema_version="1",
            source=_SOURCE,
            product_id="toy.a",
            product_version="1.0.0",
            engine_api=ENGINE_API_VERSION,
            plugins=(PluginRequirement(plugin_id="toy.a", version_specifier="==1.0.0"),),
            entrypoints={"hello": "root"},
            configuration={},
            graph_factory_symbol=_FACTORY_SYMBOL,
        )
