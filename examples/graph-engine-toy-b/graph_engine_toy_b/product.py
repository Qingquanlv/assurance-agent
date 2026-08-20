from __future__ import annotations

from graph_engine import ENGINE_API_VERSION
from graph_engine.graph.schema import WorkflowDef
from graph_engine.plugin_api import ProviderSource
from graph_engine.product import PluginRequirement, ProductManifest


class ToyBProduct:
    @staticmethod
    def manifest() -> ProductManifest:
        return ProductManifest(
            schema_version="1",
            source=ProviderSource(
                distribution="graph-engine-toy-b",
                version="1.0.0",
                entrypoint_group="graph_engine.products",
                entrypoint_name="toy-b",
                declaration_path="graph_engine_toy_b/product-declaration.json",
            ),
            product_id="toy.b",
            product_version="1.0.0",
            engine_api=ENGINE_API_VERSION,
            plugins=(PluginRequirement(plugin_id="toy.b", version_specifier="==1.0.0"),),
            entrypoints={"review": "root"},
            configuration={},
            workflow=WorkflowDef.model_validate(
                {
                    "name": "toy-b",
                    "entrypoints": {"review": "root"},
                    "retry": {
                        "once": {"max_attempts": 1},
                        "twice": {
                            "max_attempts": 2,
                            "retry_on": ["transient"],
                        },
                    },
                    "timeout": {"short": {"run_seconds": 5}},
                    "graphs": {
                        "root": {
                            "max_activations": 7,
                            "start": "seed",
                            "nodes": {
                                "seed": {
                                    "kind": "task",
                                    "capability": "toy.b.seed",
                                    "retry": "once",
                                    "timeout": "short",
                                },
                                "left": {
                                    "kind": "task",
                                    "capability": "toy.b.left",
                                    "retry": "twice",
                                    "timeout": "short",
                                    "resources": {"writes": ["left.txt"]},
                                },
                                "child-subgraph": {
                                    "kind": "subgraph",
                                    "graph": "child",
                                    "resources": {"writes": ["child.txt"]},
                                },
                                "joined": {"kind": "join", "join": "all"},
                                "combine": {
                                    "kind": "task",
                                    "capability": "toy.b.combine",
                                    "retry": "once",
                                    "timeout": "short",
                                },
                                "review": {
                                    "kind": "interrupt",
                                    "reason": "review combined result",
                                    "actions": ["approve", "reject"],
                                },
                                "done": {"kind": "end"},
                            },
                            "edges": [
                                {"from": "seed", "to": "left"},
                                {"from": "seed", "to": "child-subgraph"},
                                {"from": "left", "to": "joined"},
                                {"from": "child-subgraph", "to": "joined"},
                                {"from": "joined", "to": "combine"},
                                {"from": "combine", "to": "review"},
                                {"from": "review", "to": "done"},
                            ],
                        },
                        "child": {
                            "max_activations": 2,
                            "start": "child",
                            "nodes": {
                                "child": {
                                    "kind": "task",
                                    "capability": "toy.b.child",
                                    "retry": "once",
                                    "timeout": "short",
                                    "resources": {"writes": ["child.txt"]},
                                },
                                "done": {"kind": "end"},
                            },
                            "edges": [{"from": "child", "to": "done"}],
                        },
                    },
                }
            ),
        )
