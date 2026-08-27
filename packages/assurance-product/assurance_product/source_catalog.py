from __future__ import annotations

from graph_engine.composition import WheelPluginSource
from graph_engine.plugin_api import ProviderSource

from assurance_product.models import AdapterName

_SIX_CAPABILITY_SOURCES: tuple[ProviderSource, ...] = (
    ProviderSource(
        distribution="assurance-intake",
        version="0.1.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="intake",
        entrypoint_value="assurance_intake.plugin:IntakePlugin",
        declaration_path="assurance_intake/plugin-declaration.json",
        import_roots=("",),
    ),
    ProviderSource(
        distribution="assurance-generation",
        version="0.1.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="generation",
        entrypoint_value="assurance_generation.plugin:GenerationPlugin",
        declaration_path="assurance_generation/plugin-declaration.json",
        import_roots=("",),
    ),
    ProviderSource(
        distribution="assurance-execution",
        version="0.1.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="execution",
        entrypoint_value="assurance_execution.plugin:ExecutionPlugin",
        declaration_path="assurance_execution/plugin-declaration.json",
        import_roots=("",),
    ),
    ProviderSource(
        distribution="assurance-healing",
        version="0.1.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="healing",
        entrypoint_value="assurance_healing.plugin:HealingPlugin",
        declaration_path="assurance_healing/plugin-declaration.json",
        import_roots=("",),
    ),
    ProviderSource(
        distribution="assurance-quality",
        version="0.1.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="quality",
        entrypoint_value="assurance_quality.plugin:QualityPlugin",
        declaration_path="assurance_quality/plugin-declaration.json",
        import_roots=("",),
    ),
    ProviderSource(
        distribution="assurance-improvement",
        version="0.1.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="improvement",
        entrypoint_value="assurance_improvement.plugin:ImprovementPlugin",
        declaration_path="assurance_improvement/plugin-declaration.json",
        import_roots=("",),
    ),
)

_RUNTIME_SOURCES: dict[AdapterName, ProviderSource] = {
    "opencode": ProviderSource(
        distribution="agent-runtime-opencode",
        version="0.1.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="opencode",
        entrypoint_value="agent_runtime_opencode.plugin:OpenCodePlugin",
        declaration_path="agent_runtime_opencode/plugin-declaration.json",
        import_roots=("",),
    ),
    "cursor": ProviderSource(
        distribution="agent-runtime-cursor",
        version="0.1.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="cursor",
        entrypoint_value="agent_runtime_cursor.plugin:CursorPlugin",
        declaration_path="agent_runtime_cursor/plugin-declaration.json",
        import_roots=("",),
    ),
}


def product_source_catalog(adapter: AdapterName) -> tuple[ProviderSource, ...]:
    try:
        runtime = _RUNTIME_SOURCES[adapter]
    except KeyError as error:
        raise ValueError(f"unsupported product adapter: {adapter!r}") from error
    return (*_SIX_CAPABILITY_SOURCES, runtime)


def adapter_for_entrypoint(entrypoint: str) -> AdapterName:
    if entrypoint == "assurance-opencode":
        return "opencode"
    if entrypoint == "assurance-cursor":
        return "cursor"
    raise ValueError(f"unsupported product entrypoint: {entrypoint!r}")


def wheel_plugin_source(source: ProviderSource) -> WheelPluginSource:
    if source.entrypoint_group != "graph_engine.plugins":
        raise ValueError("product catalog source must be a graph_engine.plugins coordinate")
    return WheelPluginSource(
        distribution=source.distribution,
        entrypoint_name=source.entrypoint_name,
        declaration_path=source.declaration_path,
    )
