from __future__ import annotations

from graph_engine import ENGINE_API_VERSION
from graph_engine.composition import PluginRequirement, ProductManifest
from graph_engine.graph.schema import WorkflowDef
from graph_engine.plugin_api import ProviderSource

from agent_runtime_fixture import (
    RUN_CAPABILITY_ID,
    assemble_request,
    fixture_config,
    fixture_resources,
)

_SOURCE = ProviderSource(
    distribution="agent-runtime-fixture",
    version="1.0.0",
    entrypoint_group="graph_engine.products",
    entrypoint_name="fixture",
    entrypoint_value="agent_runtime_fixture.product:FixtureProduct",
    declaration_path="agent_runtime_fixture/product-declaration.json",
    import_roots=("",),
)


def _workflow() -> WorkflowDef:
    request = assemble_request(fixture_resources(), fixture_config())
    return WorkflowDef.model_validate(
        {
            "name": "agent-runtime-fixture",
            "entrypoints": {"run": "root"},
            "retry": {"once": {"max_attempts": 1}},
            "timeout": {"short": {"run_seconds": 120}},
            "graphs": {
                "root": {
                    "max_activations": 2,
                    "start": "run",
                    "nodes": {
                        "run": {
                            "kind": "task",
                            "capability": RUN_CAPABILITY_ID,
                            "input": request.model_dump(mode="json"),
                            "input_projection": {"type": "config_pointer", "pointer": ""},
                            "retry": "once",
                            "timeout": "short",
                            "resources": {"writes": ["result.json"]},
                        },
                        "done": {"kind": "end"},
                    },
                    "edges": [{"from": "run", "to": "done"}],
                }
            },
        }
    )


class FixtureProduct:
    @staticmethod
    def manifest() -> ProductManifest:
        workflow = _workflow()
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
            entrypoints=dict(workflow.entrypoints),
            configuration={"fixture.runtime": fixture_config()},
            workflow=workflow,
        )
