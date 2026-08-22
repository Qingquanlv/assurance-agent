from __future__ import annotations

from graph_engine.composition import PluginRequirement, ProductManifest
from graph_engine.graph.schema import WorkflowDef
from graph_engine.plugin_api import ProviderSource

from assurance_product.models import ENGINE_API, PRODUCT_ID, AdapterName
from assurance_product.source_catalog import product_source_catalog

_PRODUCT_VERSION = "0.1.0"
_MANIFEST_PRODUCT_ID = "assurance.product"
_DECLARATION_PATH = "assurance_product/resources/declarations/product.yaml"
_CAPABILITY_PLUGIN_IDS: tuple[str, ...] = (
    "assurance.intake",
    "assurance.generation",
    "assurance.execution",
    "assurance.healing",
    "assurance.quality",
    "assurance.improvement",
)
_RUNTIME_PLUGIN_IDS: dict[AdapterName, str] = {
    "opencode": "runtime.opencode",
    "cursor": "runtime.cursor",
}
_ENTRYPOINTS = {"run": "root"}
_WORKFLOW = WorkflowDef.model_validate(
    {
        "name": PRODUCT_ID,
        "entrypoints": dict(_ENTRYPOINTS),
        "retry": {"once": {"max_attempts": 1}},
        "timeout": {"short": {"run_seconds": 1}},
        "graphs": {
            "root": {
                "max_activations": 1,
                "start": "done",
                "nodes": {"done": {"kind": "end"}},
                "edges": [],
            }
        },
    }
)


def _product_source(adapter: AdapterName) -> ProviderSource:
    entrypoint_name = f"assurance-{adapter}"
    provider = (
        "AssuranceOpenCodeProductProvider" if adapter == "opencode" else "AssuranceCursorProductProvider"
    )
    return ProviderSource(
        distribution="assurance-product",
        version=_PRODUCT_VERSION,
        entrypoint_group="graph_engine.products",
        entrypoint_name=entrypoint_name,
        entrypoint_value=f"assurance_product.product:{provider}",
        declaration_path=_DECLARATION_PATH,
        import_roots=("",),
    )


def _manifest(adapter: AdapterName) -> ProductManifest:
    product_source_catalog(adapter)
    plugins = tuple(
        PluginRequirement(plugin_id=plugin_id, version_specifier="==0.1.0")
        for plugin_id in (*_CAPABILITY_PLUGIN_IDS, _RUNTIME_PLUGIN_IDS[adapter])
    )
    return ProductManifest(
        schema_version="1",
        source=_product_source(adapter),
        product_id=_MANIFEST_PRODUCT_ID,
        product_version=_PRODUCT_VERSION,
        engine_api=ENGINE_API,
        plugins=plugins,
        entrypoints=dict(_ENTRYPOINTS),
        configuration={},
        workflow=_WORKFLOW,
    )


class AssuranceOpenCodeProductProvider:
    @staticmethod
    def manifest() -> ProductManifest:
        return _manifest("opencode")


class AssuranceCursorProductProvider:
    @staticmethod
    def manifest() -> ProductManifest:
        return _manifest("cursor")
