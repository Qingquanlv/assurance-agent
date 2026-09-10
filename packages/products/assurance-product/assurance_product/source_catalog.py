from __future__ import annotations

from graph_engine.composition import WheelPluginSource
from graph_engine.plugin_api import ProviderSource

_CAPABILITY_SOURCES: tuple[ProviderSource, ...] = (
    ProviderSource(
        distribution="assurance-intake",
        version="0.2.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="intake",
        entrypoint_value="assurance_intake.plugin:IntakePlugin",
        declaration_path="assurance_intake/plugin-declaration.json",
        import_roots=("",),
    ),
    ProviderSource(
        distribution="assurance-generation",
        version="0.2.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="generation",
        entrypoint_value="assurance_generation.plugin:GenerationPlugin",
        declaration_path="assurance_generation/plugin-declaration.json",
        import_roots=("",),
    ),
    ProviderSource(
        distribution="assurance-execution",
        version="0.2.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="execution",
        entrypoint_value="assurance_execution.plugin:ExecutionPlugin",
        declaration_path="assurance_execution/plugin-declaration.json",
        import_roots=("",),
    ),
    ProviderSource(
        distribution="assurance-healing",
        version="0.2.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="healing",
        entrypoint_value="assurance_healing.plugin:HealingPlugin",
        declaration_path="assurance_healing/plugin-declaration.json",
        import_roots=("",),
    ),
    ProviderSource(
        distribution="assurance-quality",
        version="0.2.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="quality",
        entrypoint_value="assurance_quality.plugin:QualityPlugin",
        declaration_path="assurance_quality/plugin-declaration.json",
        import_roots=("",),
    ),
    ProviderSource(
        distribution="assurance-improvement",
        version="0.2.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="improvement",
        entrypoint_value="assurance_improvement.plugin:ImprovementPlugin",
        declaration_path="assurance_improvement/plugin-declaration.json",
        import_roots=("",),
    ),
    ProviderSource(
        distribution="assurance-telemetry",
        version="0.2.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="telemetry",
        entrypoint_value="assurance_telemetry.plugin:TelemetryPlugin",
        declaration_path="assurance_telemetry/plugin-declaration.json",
        import_roots=("",),
    ),
)

_OPENCODE_RUNTIME_SOURCE = ProviderSource(
    distribution="agent-runtime-opencode",
    version="0.1.0",
    entrypoint_group="graph_engine.plugins",
    entrypoint_name="opencode",
    entrypoint_value="agent_runtime_opencode.plugin:OpenCodePlugin",
    declaration_path="agent_runtime_opencode/plugin-declaration.json",
    import_roots=("",),
)


def product_source_catalog() -> tuple[ProviderSource, ...]:
    return (*_CAPABILITY_SOURCES, _OPENCODE_RUNTIME_SOURCE)


def wheel_plugin_source(source: ProviderSource) -> WheelPluginSource:
    if source.entrypoint_group != "graph_engine.plugins":
        raise ValueError("product catalog source must be a graph_engine.plugins coordinate")
    return WheelPluginSource(
        distribution=source.distribution,
        entrypoint_name=source.entrypoint_name,
        declaration_path=source.declaration_path,
    )
