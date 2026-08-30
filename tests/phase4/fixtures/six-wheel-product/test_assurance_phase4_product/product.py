from __future__ import annotations

from pathlib import Path

from graph_engine import ENGINE_API_VERSION
from graph_engine.composition import PluginRequirement, ProductManifest
from graph_engine.graph.schema import parse_workflow
from graph_engine.plugin_api import ProviderSource

_PACKAGE = Path(__file__).resolve().parent
_FIXTURES = _PACKAGE.parents[1]
_WORKFLOW = parse_workflow((_PACKAGE / "workflow.yaml").read_text(encoding="utf-8"))
_PRODUCT_ID = "test.assurance.phase4"
_PRODUCT_VERSION = "1.0.0"
_DISTRIBUTION = "test-assurance-phase4-product"
_ASSURANCE_PLUGINS = (
    PluginRequirement(plugin_id="assurance.execution", version_specifier="==0.2.0"),
    PluginRequirement(plugin_id="assurance.generation", version_specifier="==0.2.0"),
    PluginRequirement(plugin_id="assurance.healing", version_specifier="==0.2.0"),
    PluginRequirement(plugin_id="assurance.improvement", version_specifier="==0.2.0"),
    PluginRequirement(plugin_id="assurance.intake", version_specifier="==0.2.0"),
    PluginRequirement(plugin_id="assurance.quality", version_specifier="==0.2.0"),
)


def _source(*, entrypoint_name: str, entrypoint_value: str, declaration_path: str) -> ProviderSource:
    return ProviderSource(
        distribution=_DISTRIBUTION,
        version=_PRODUCT_VERSION,
        entrypoint_group="graph_engine.products",
        entrypoint_name=entrypoint_name,
        entrypoint_value=entrypoint_value,
        declaration_path=declaration_path,
        import_roots=("",),
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
        entrypoints=dict(_WORKFLOW.entrypoints),
        configuration={},
        config_plugin_paths=(str((_FIXTURES / f"bindings-{bindings_kind}").resolve()),),
        workflow=_WORKFLOW,
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


class Phase4CursorProduct:
    @staticmethod
    def manifest() -> ProductManifest:
        return _manifest(
            runtime_plugin_id="runtime.cursor",
            bindings_kind="cursor",
            entrypoint_name="phase4-cursor",
            entrypoint_value="test_assurance_phase4_product.product:Phase4CursorProduct",
            declaration_path="test_assurance_phase4_product/product-cursor-declaration.json",
        )
