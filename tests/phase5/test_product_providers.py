from __future__ import annotations

from graph_engine import ENGINE_API_VERSION
from graph_engine.plugin_api import ProviderSource

from assurance_product.models import ENGINE_API, PRODUCT_ID
from assurance_product.product import (
    AssuranceCursorProductProvider,
    AssuranceOpenCodeProductProvider,
)
from assurance_product.source_catalog import product_source_catalog

_SIX_CAPABILITY_DISTRIBUTIONS = frozenset(
    {
        "assurance-intake",
        "assurance-generation",
        "assurance-execution",
        "assurance-healing",
        "assurance-quality",
        "assurance-improvement",
    }
)
_RUNTIME_DISTRIBUTIONS = frozenset({"agent-runtime-opencode", "agent-runtime-cursor"})
_SIX_CAPABILITY_SOURCES = (
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


def six_capability_coordinates(catalog: tuple[ProviderSource, ...]) -> tuple[ProviderSource, ...]:
    return tuple(source for source in catalog if source.distribution in _SIX_CAPABILITY_DISTRIBUTIONS)


def runtime_coordinates(catalog: tuple[ProviderSource, ...]) -> set[str]:
    return {
        f"{source.distribution}=={source.version}"
        for source in catalog
        if source.distribution in _RUNTIME_DISTRIBUTIONS
    }


def test_provider_catalogs_differ_only_by_runtime_adapter():
    opencode = product_source_catalog("opencode")
    cursor = product_source_catalog("cursor")
    assert six_capability_coordinates(opencode) == six_capability_coordinates(cursor)
    assert runtime_coordinates(opencode) == {"agent-runtime-opencode==0.1.0"}
    assert runtime_coordinates(cursor) == {"agent-runtime-cursor==0.1.0"}


def test_source_catalogs_are_six_wheels_plus_selected_adapter():
    opencode = product_source_catalog("opencode")
    cursor = product_source_catalog("cursor")
    assert six_capability_coordinates(opencode) == _SIX_CAPABILITY_SOURCES
    assert opencode == (*_SIX_CAPABILITY_SOURCES, _runtime_source("opencode"))
    assert cursor == (*_SIX_CAPABILITY_SOURCES, _runtime_source("cursor"))
    assert {source.distribution for source in opencode} - _SIX_CAPABILITY_DISTRIBUTIONS == {
        "agent-runtime-opencode"
    }
    assert {source.distribution for source in cursor} - _SIX_CAPABILITY_DISTRIBUTIONS == {
        "agent-runtime-cursor"
    }


def test_providers_return_one_minimal_manifest_per_adapter():
    opencode = AssuranceOpenCodeProductProvider.manifest()
    cursor = AssuranceCursorProductProvider.manifest()
    assert PRODUCT_ID == "assurance"
    assert ENGINE_API == ENGINE_API_VERSION == "2.0"
    assert opencode.engine_api == cursor.engine_api == ENGINE_API
    assert tuple(requirement.plugin_id for requirement in opencode.plugins) == (
        "assurance.intake",
        "assurance.generation",
        "assurance.execution",
        "assurance.healing",
        "assurance.quality",
        "assurance.improvement",
        "runtime.opencode",
    )
    assert tuple(requirement.plugin_id for requirement in cursor.plugins) == (
        "assurance.intake",
        "assurance.generation",
        "assurance.execution",
        "assurance.healing",
        "assurance.quality",
        "assurance.improvement",
        "runtime.cursor",
    )
    assert opencode.entrypoints
    assert opencode.entrypoints == cursor.entrypoints
    assert opencode.configuration == {}
    assert cursor.configuration == {}
    assert opencode.workflow == cursor.workflow


def _runtime_source(adapter: str) -> ProviderSource:
    if adapter == "opencode":
        return ProviderSource(
            distribution="agent-runtime-opencode",
            version="0.1.0",
            entrypoint_group="graph_engine.plugins",
            entrypoint_name="opencode",
            entrypoint_value="agent_runtime_opencode.plugin:OpenCodePlugin",
            declaration_path="agent_runtime_opencode/plugin-declaration.json",
            import_roots=("",),
        )
    return ProviderSource(
        distribution="agent-runtime-cursor",
        version="0.1.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="cursor",
        entrypoint_value="agent_runtime_cursor.plugin:CursorPlugin",
        declaration_path="agent_runtime_cursor/plugin-declaration.json",
        import_roots=("",),
    )
