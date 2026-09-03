from __future__ import annotations

from graph_engine import ENGINE_API_VERSION
from graph_engine.plugin_api import ProviderSource

from agent_runtime_contracts.plugin_kit import RuntimeAdapterPlugin, RuntimeAdapterSpec
from agent_runtime_opencode.handler import OpenCodeHandler
from agent_runtime_opencode.protocol import OPENCODE_RUNTIME_CAPABILITIES

_SOURCE = ProviderSource(
    distribution="agent-runtime-opencode",
    version="0.1.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="opencode",
    entrypoint_value="agent_runtime_opencode.plugin:OpenCodePlugin",
    declaration_path="agent_runtime_opencode/plugin-declaration.json",
    import_roots=("",),
)


class OpenCodePlugin(RuntimeAdapterPlugin):
    spec = RuntimeAdapterSpec(
        name="opencode",
        version="0.1.0",
        engine_api=ENGINE_API_VERSION,
        source=_SOURCE,
        handler=OpenCodeHandler,
        capabilities=OPENCODE_RUNTIME_CAPABILITIES,
    )
