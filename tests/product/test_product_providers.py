from __future__ import annotations

import importlib.metadata
from pathlib import Path

from graph_engine import ENGINE_API_VERSION
from graph_engine.canonical import canonical_json_bytes
from graph_engine.plugin_api import ProviderSource

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
_RUNTIME_DISTRIBUTIONS = frozenset({"agent-runtime-opencode"})
_SIX_CAPABILITY_SOURCES = (
    ProviderSource(
        distribution="assurance-intake",
        version="0.3.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="intake",
        entrypoint_value="assurance_intake.plugin:IntakePlugin",
        declaration_path="assurance_intake/plugin-declaration.json",
        import_roots=("",),
    ),
    ProviderSource(
        distribution="assurance-generation",
        version="0.3.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="generation",
        entrypoint_value="assurance_generation.plugin:GenerationPlugin",
        declaration_path="assurance_generation/plugin-declaration.json",
        import_roots=("",),
    ),
    ProviderSource(
        distribution="assurance-execution",
        version="0.3.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="execution",
        entrypoint_value="assurance_execution.plugin:ExecutionPlugin",
        declaration_path="assurance_execution/plugin-declaration.json",
        import_roots=("",),
    ),
    ProviderSource(
        distribution="assurance-healing",
        version="0.3.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="healing",
        entrypoint_value="assurance_healing.plugin:HealingPlugin",
        declaration_path="assurance_healing/plugin-declaration.json",
        import_roots=("",),
    ),
    ProviderSource(
        distribution="assurance-quality",
        version="0.3.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="quality",
        entrypoint_value="assurance_quality.plugin:QualityPlugin",
        declaration_path="assurance_quality/plugin-declaration.json",
        import_roots=("",),
    ),
    ProviderSource(
        distribution="assurance-improvement",
        version="0.3.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="improvement",
        entrypoint_value="assurance_improvement.plugin:ImprovementPlugin",
        declaration_path="assurance_improvement/plugin-declaration.json",
        import_roots=("",),
    ),
)


def test_product_exposes_only_opencode_provider() -> None:
    providers = {
        item.name
        for item in importlib.metadata.entry_points(group="graph_engine.products")
        if item.dist and item.dist.name == "assurance-product"
    }
    assert providers == {"assurance-opencode"}


def six_capability_coordinates(catalog: tuple[ProviderSource, ...]) -> tuple[ProviderSource, ...]:
    return tuple(source for source in catalog if source.distribution in _SIX_CAPABILITY_DISTRIBUTIONS)


def runtime_coordinates(catalog: tuple[ProviderSource, ...]) -> set[str]:
    return {
        f"{source.distribution}=={source.version}"
        for source in catalog
        if source.distribution in _RUNTIME_DISTRIBUTIONS
    }


def test_source_catalog_is_six_wheels_plus_opencode() -> None:
    from assurance_product.source_catalog import product_source_catalog

    catalog = product_source_catalog()
    assert six_capability_coordinates(catalog) == _SIX_CAPABILITY_SOURCES
    assert catalog == (*_SIX_CAPABILITY_SOURCES, _runtime_source())
    assert runtime_coordinates(catalog) == {"agent-runtime-opencode==0.1.0"}
    assert {source.distribution for source in catalog} - _SIX_CAPABILITY_DISTRIBUTIONS == {
        "agent-runtime-opencode"
    }


def test_provider_returns_one_minimal_opencode_manifest() -> None:
    from assurance_product.models import ENGINE_API, PRODUCT_ID
    from assurance_product.product import AssuranceOpenCodeProductProvider

    opencode = AssuranceOpenCodeProductProvider.manifest()
    assert PRODUCT_ID == "assurance"
    assert ENGINE_API == ENGINE_API_VERSION == "2.0"
    assert opencode.engine_api == ENGINE_API
    assert tuple(requirement.plugin_id for requirement in opencode.plugins) == (
        "assurance.execution",
        "assurance.generation",
        "assurance.healing",
        "assurance.improvement",
        "assurance.intake",
        "assurance.product.agent",
        "assurance.product.configuration",
        "assurance.quality",
        "runtime.opencode",
    )
    capability_ids = {
        "assurance.execution",
        "assurance.generation",
        "assurance.healing",
        "assurance.improvement",
        "assurance.intake",
        "assurance.quality",
    }
    assert {
        requirement.plugin_id: requirement.version_specifier
        for requirement in opencode.plugins
        if requirement.plugin_id in capability_ids
    } == {plugin_id: "==0.3.0" for plugin_id in capability_ids}
    assert opencode.product_id == "assurance.product"
    assert opencode.source is not None
    assert opencode.source.declaration_path == "assurance_product/product-declaration-opencode.json"
    assert opencode.entrypoints
    assert opencode.configuration == {}
    assert not hasattr(opencode, "workflow")
    assert getattr(opencode, "workflow", None) is None
    assert getattr(opencode, "workflow_resource_id", None) is None
    assert getattr(opencode, "workflow_module", None) is None
    assert getattr(opencode, "workflow_module_resources", ()) == ()
    assert getattr(opencode, "workflow_slot_bindings", ()) == ()
    assert opencode.graph_factory_symbol == "assurance_product.graphs.factory:build_product_graphs"


def test_committed_product_declaration_bytes_match_canonical_documents() -> None:
    import assurance_product.product as product
    from assurance_product.product import product_declaration_document

    package_root = Path(product.__file__).resolve().parent
    assert (package_root / "product-declaration-opencode.json").read_bytes() == (
        canonical_json_bytes(product_declaration_document()) + b"\n"
    )
    assert not (package_root / "product-declaration-cursor.json").exists()


def test_provider_loaded_manifests_have_exact_change_local_execute_claims() -> None:
    from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS
    from assurance_product.output_routes import OutputRouteCatalog
    from assurance_product.product import AssuranceOpenCodeProductProvider
    from graph_engine.plugin_api import ResourceClaims, ResourceClaimTemplate

    change_id = "CH-CURRENT-001"
    catalog = OutputRouteCatalog()
    assert len(AGENT_EXECUTION_CONTRACTS) == 26
    manifest = AssuranceOpenCodeProductProvider.manifest()
    assert not hasattr(manifest, "workflow")
    assert getattr(manifest, "workflow", None) is None
    assert getattr(manifest, "workflow_module", None) is None
    assert manifest.graph_factory_symbol == "assurance_product.graphs.factory:build_product_graphs"
    for contract_id, contract in AGENT_EXECUTION_CONTRACTS.items():
        resources = contract.resources
        if isinstance(resources, ResourceClaimTemplate):
            writes = resources.resolve({"change_id": change_id}).writes
        else:
            assert isinstance(resources, ResourceClaims)
            writes = resources.writes
        assert writes == catalog.resource_claims(contract_id, change_id)


def _runtime_source() -> ProviderSource:
    return ProviderSource(
        distribution="agent-runtime-opencode",
        version="0.1.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="opencode",
        entrypoint_value="agent_runtime_opencode.plugin:OpenCodePlugin",
        declaration_path="agent_runtime_opencode/plugin-declaration.json",
        import_roots=("",),
    )
