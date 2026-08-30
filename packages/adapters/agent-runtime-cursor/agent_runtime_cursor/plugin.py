from __future__ import annotations

from graph_engine import ENGINE_API_VERSION
from graph_engine.plugin_api import ProviderSource

from agent_runtime_contracts.plugin_kit import RuntimeAdapterPlugin, RuntimeAdapterSpec
from agent_runtime_cursor.handler import CursorHandler

_SOURCE = ProviderSource(
    distribution="agent-runtime-cursor",
    version="0.1.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="cursor",
    entrypoint_value="agent_runtime_cursor.plugin:CursorPlugin",
    declaration_path="agent_runtime_cursor/plugin-declaration.json",
    import_roots=("",),
)


class CursorPlugin(RuntimeAdapterPlugin):
    spec = RuntimeAdapterSpec(
        name="cursor",
        version="0.1.0",
        engine_api=ENGINE_API_VERSION,
        source=_SOURCE,
        handler=CursorHandler,
    )
