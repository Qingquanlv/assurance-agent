from __future__ import annotations

from graph_engine.composition.lock import _manifest_projection
from graph_engine.composition.models import PluginRequirement, ProductManifest
from graph_engine.graph.schema import WorkflowDef
from graph_engine.plugin_api import ProviderSource


def test_manifest_projection_keeps_input_projection_discriminator() -> None:
    workflow = WorkflowDef.model_validate(
        {
            "name": "proj",
            "entrypoints": {"run": "root"},
            "retry": {"once": {"max_attempts": 1}},
            "timeout": {"short": {"run_seconds": 1}},
            "graphs": {
                "root": {
                    "max_activations": 2,
                    "start": "run",
                    "nodes": {
                        "run": {
                            "kind": "task",
                            "capability": "test.run",
                            "input": {"ok": True},
                            "input_projection": {"type": "config_pointer", "pointer": ""},
                            "retry": "once",
                            "timeout": "short",
                        },
                        "done": {"kind": "end"},
                    },
                    "edges": [{"from": "run", "to": "done"}],
                }
            },
        }
    )
    manifest = ProductManifest(
        schema_version="1",
        source=ProviderSource(
            distribution="test-product",
            version="1.0.0",
            entrypoint_group="graph_engine.products",
            entrypoint_name="test",
            entrypoint_value="test_product:Provider",
            declaration_path="test_product/product-declaration.json",
            import_roots=("",),
        ),
        product_id="test.product",
        product_version="1.0.0",
        engine_api="2.0",
        plugins=(PluginRequirement(plugin_id="test.runtime", version_specifier="==1.0.0"),),
        entrypoints={"run": "root"},
        workflow=workflow,
    )
    projection = _manifest_projection(manifest)
    assert projection["workflow"]["graphs"]["root"]["nodes"]["run"]["input_projection"] == {
        "type": "config_pointer",
        "pointer": "",
    }
    ProductManifest.model_validate(projection)
