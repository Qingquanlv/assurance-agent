from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
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

_PACKAGE = Path(__file__).resolve().parent
_FIXTURES = _PACKAGE.parents[1]
_PRODUCT_ID = "test.assurance.phase4"
_PRODUCT_VERSION = "1.0.0"
_DISTRIBUTION = "test-assurance-phase4-product"
_FACTORY_SYMBOL = "test_assurance_phase4_product.product:build_phase4_graphs"
_ASSURANCE_PLUGINS = (
    PluginRequirement(plugin_id="assurance.execution", version_specifier="==0.3.0"),
    PluginRequirement(plugin_id="assurance.generation", version_specifier="==0.3.0"),
    PluginRequirement(plugin_id="assurance.healing", version_specifier="==0.3.0"),
    PluginRequirement(plugin_id="assurance.improvement", version_specifier="==0.3.0"),
    PluginRequirement(plugin_id="assurance.intake", version_specifier="==0.3.0"),
    PluginRequirement(plugin_id="assurance.quality", version_specifier="==0.3.0"),
)
_PREPARE_ID = "test.assurance.bindings.prepare"
_EXECUTE_ID = "test.assurance.bindings.execute"
_FINALIZE_ID = "test.assurance.bindings.finalize"


class Phase4State(TypedDict, total=False):
    change_id: str
    prepared: bool
    executed: bool
    finalized: bool


@dataclass(frozen=True, slots=True)
class Phase4Graphs:
    entrypoints: Mapping[str, CompiledStateGraph]
    contracts: Mapping[str, EntrypointGraphContract]


def _select(_state: Phase4State) -> dict[str, object]:
    return {}


def _publish_prepare(_state: Phase4State, _output: object, _receipt: object) -> dict[str, object]:
    return {"prepared": True}


def _publish_execute(_state: Phase4State, _output: object, _receipt: object) -> dict[str, object]:
    return {"executed": True}


def _publish_finalize(_state: Phase4State, _output: object, _receipt: object) -> dict[str, object]:
    return {"finalized": True}


def build_phase4_graphs(
    context: GraphBuildContext,
    features: Mapping[str, object] | None = None,
) -> Phase4Graphs:
    del features
    capability = context.for_capability("test.assurance.bindings")
    builder: StateGraph[Phase4State] = StateGraph(Phase4State)
    for name, contract_id, publish in (
        ("prepare", _PREPARE_ID, _publish_prepare),
        ("execute", _EXECUTE_ID, _publish_execute),
        ("finalize", _FINALIZE_ID, _publish_finalize),
    ):
        builder.add_node(
            name,
            cast(
                Any,
                capability.attempt(
                    contract_id,
                    semantic_node_id=name,
                    activation=BusinessActivation.one_shot(),
                    select=_select,
                    publish=publish,
                ),
            ),
        )
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "execute")
    builder.add_edge("execute", "finalize")
    builder.add_edge("finalize", END)
    entrypoints = {"fixture": context.compile_root(builder)}
    contracts = {
        "fixture": EntrypointGraphContract(
            name="fixture",
            input_model="test_assurance_phase4_product.product.Phase4State",
            output_model="test_assurance_phase4_product.product.Phase4State",
            state_model="test_assurance_phase4_product.product.Phase4State",
            input_schema_digest=entrypoint_digest("fixture", "input"),
            output_schema_digest=entrypoint_digest("fixture", "output"),
            state_schema_digest=entrypoint_digest("fixture", "state"),
            state_schema_version="1",
            recursion_limit=32,
        )
    }
    return Phase4Graphs(entrypoints=entrypoints, contracts=contracts)


def _source(*, entrypoint_name: str, entrypoint_value: str, declaration_path: str) -> ProviderSource:
    return ProviderSource(
        distribution=_DISTRIBUTION,
        version=_PRODUCT_VERSION,
        entrypoint_group="graph_engine.products",
        entrypoint_name=entrypoint_name,
        entrypoint_value=entrypoint_value,
        declaration_path=declaration_path,
        import_roots=("", "test_assurance_phase4_product"),
    )


def _manifest(
    *,
    runtime_plugin_id: str,
    bindings_kind: str,
    entrypoint_name: str,
    entrypoint_value: str,
    declaration_path: str,
) -> ProductManifest:
    return ProductManifest(
        schema_version="1",
        source=_source(
            entrypoint_name=entrypoint_name,
            entrypoint_value=entrypoint_value,
            declaration_path=declaration_path,
        ),
        product_id=_PRODUCT_ID,
        product_version=_PRODUCT_VERSION,
        engine_api=ENGINE_API_VERSION,
        plugins=(
            *_ASSURANCE_PLUGINS,
            PluginRequirement(plugin_id=runtime_plugin_id, version_specifier="==0.1.0"),
            PluginRequirement(plugin_id="test.assurance.bindings", version_specifier="==1.0.0"),
        ),
        entrypoints={"fixture": "root"},
        configuration={},
        config_plugin_paths=(str((_FIXTURES / f"bindings-{bindings_kind}").resolve()),),
        graph_factory_symbol=_FACTORY_SYMBOL,
    )


class Phase4OpenCodeProduct:
    @staticmethod
    def manifest() -> ProductManifest:
        return _manifest(
            runtime_plugin_id="runtime.opencode",
            bindings_kind="opencode",
            entrypoint_name="phase4-opencode",
            entrypoint_value="test_assurance_phase4_product.product:Phase4OpenCodeProduct",
            declaration_path="test_assurance_phase4_product/product-opencode-declaration.json",
        )
