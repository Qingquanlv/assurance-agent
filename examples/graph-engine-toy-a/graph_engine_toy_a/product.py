from __future__ import annotations

from graph_engine import ENGINE_API_VERSION
from graph_engine.graph.schema import WorkflowDef
from graph_engine.plugin_api import ProviderSource
from graph_engine.product import PluginRequirement, ProductManifest


class ToyAProduct:
    @staticmethod
    def manifest() -> ProductManifest:
        return ProductManifest(
            schema_version="1",
            source=ProviderSource(
                distribution="graph-engine-toy-a",
                version="1.0.0",
                entrypoint_group="graph_engine.products",
                entrypoint_name="toy-a",
                entrypoint_value="graph_engine_toy_a.product:ToyAProduct",
                declaration_path="graph_engine_toy_a/product-declaration.json",
                import_roots=("",),
            ),
            product_id="toy.a",
            product_version="1.0.0",
            engine_api=ENGINE_API_VERSION,
            plugins=(PluginRequirement(plugin_id="toy.a", version_specifier="==1.0.0"),),
            entrypoints={"hello": "root"},
            configuration={},
            workflow=WorkflowDef.model_validate(
                {
                    "name": "toy-a",
                    "entrypoints": {"hello": "root"},
                    "retry": {"once": {"max_attempts": 2, "retry_on": ["transient"]}},
                    "timeout": {"short": {"run_seconds": 5}},
                    "graphs": {
                        "root": {
                            "max_activations": 2,
                            "start": "greet",
                            "nodes": {
                                "greet": {
                                    "kind": "task",
                                    "capability": "toy.a.greet",
                                    "input": {"name": "Ada"},
                                    "retry": "once",
                                    "timeout": "short",
                                    "resources": {"writes": ["greeting.txt"]},
                                },
                                "done": {"kind": "end"},
                            },
                            "edges": [{"from": "greet", "to": "done"}],
                        }
                    },
                }
            ),
        )
