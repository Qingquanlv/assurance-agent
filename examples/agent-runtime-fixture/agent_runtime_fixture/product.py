from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from graph_engine import ENGINE_API_VERSION
from graph_engine.attempts.keys import BusinessActivation
from graph_engine.boot.boot import GraphBuildContext
from graph_engine.boot.generic import entrypoint_digest
from graph_engine.boot.graph_revision import EntrypointGraphContract
from graph_engine.composition import PluginRequirement, ProductManifest
from graph_engine.plugin_api import ProviderSource

from agent_runtime_fixture import fixture_config
from agent_runtime_fixture.contracts import RUN_CONTRACT, frozen_run_request

_SOURCE = ProviderSource(
    distribution="agent-runtime-fixture",
    version="1.0.0",
    entrypoint_group="graph_engine.products",
    entrypoint_name="fixture",
    entrypoint_value="agent_runtime_fixture.product:FixtureProduct",
    declaration_path="agent_runtime_fixture/product-declaration.json",
    import_roots=("", "agent_runtime_fixture"),
)
_FACTORY_SYMBOL = "agent_runtime_fixture.product:build_fixture_graphs"


class FixtureState(TypedDict, total=False):
    artifact: str
    status: str


@dataclass(frozen=True, slots=True)
class FixtureGraphs:
    entrypoints: Mapping[str, CompiledStateGraph]
    contracts: Mapping[str, EntrypointGraphContract]


def _select_run(_state: FixtureState) -> object:
    return frozen_run_request()


def _publish_run(_state: FixtureState, output: object, _receipt: object) -> dict[str, object]:
    return {
        "artifact": str(getattr(output, "artifact", "result.json")),
        "status": str(getattr(output, "status", "ok")),
    }


def build_fixture_graphs(
    context: GraphBuildContext,
    features: Mapping[str, object] | None = None,
) -> FixtureGraphs:
    del features
    capability = context.for_capability("fixture.binding")
    builder: StateGraph[FixtureState] = StateGraph(FixtureState)
    builder.add_node(
        "run",
        cast(
            Any,
            capability.attempt(
                RUN_CONTRACT.contract_id,
                semantic_node_id="run",
                activation=BusinessActivation.one_shot(),
                select=_select_run,
                publish=_publish_run,
            ),
        ),
    )
    builder.add_edge(START, "run")
    builder.add_edge("run", END)
    entrypoints = {"run": context.compile_root(builder)}
    contracts = {
        "run": EntrypointGraphContract(
            name="run",
            input_model="agent_runtime_fixture.product.FixtureState",
            output_model="agent_runtime_fixture.product.FixtureState",
            state_model="agent_runtime_fixture.product.FixtureState",
            input_schema_digest=entrypoint_digest("run", "input"),
            output_schema_digest=entrypoint_digest("run", "output"),
            state_schema_digest=entrypoint_digest("run", "state"),
            state_schema_version="1",
            recursion_limit=32,
        )
    }
    return FixtureGraphs(entrypoints=entrypoints, contracts=contracts)


class FixtureProduct:
    @staticmethod
    def manifest() -> ProductManifest:
        return ProductManifest(
            schema_version="1",
            source=_SOURCE,
            product_id="fixture.product",
            product_version="1.0.0",
            engine_api=ENGINE_API_VERSION,
            plugins=(
                PluginRequirement(plugin_id="fixture.binding", version_specifier="==1.0.0"),
                PluginRequirement(plugin_id="fixture.runtime", version_specifier="==1.0.0"),
            ),
            entrypoints={"run": "root"},
            configuration={"fixture.runtime": fixture_config()},
            graph_factory_symbol=_FACTORY_SYMBOL,
        )
