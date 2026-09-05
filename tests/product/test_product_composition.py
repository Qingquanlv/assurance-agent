from __future__ import annotations

import sys

import pytest

from graph_engine.composition import CapabilityBindingEntry
from graph_engine.composition.dependencies import DependencyConflict
from graph_engine.composition.resolver import ResolutionError
from graph_engine.composition.source_fs import SourceSnapshotError

from tests.product.composition_harness import request_for
from tests.product.conformance import ALL_BINDING_IDS
from tests.product.test_feature_graph_bundles import PUBLIC_BUNDLE_FIELDS

_FEATURE_OWNERS = (
    "assurance.execution",
    "assurance.generation",
    "assurance.healing",
    "assurance.improvement",
    "assurance.intake",
    "assurance.quality",
)
_PRODUCT_FACTORY = "assurance_product.graphs.factory:build_product_graphs"


def test_composition_has_exact_opencode_identity_and_binding_closure(opencode_composition):
    from assurance_product.agent_contracts import (
        AGENT_EXECUTION_CONTRACTS,
        all_feature_agent_contracts,
        all_feature_task_contracts,
    )
    from assurance_product.models import CONFIGURATION_PLUGIN_ID, PLUGIN_ID, PRODUCT_ENTRYPOINTS

    composition = opencode_composition
    assert composition.lock.engine_api == "2.0"
    entries = composition.registries.capabilities.entries
    bindings = {key: value for key, value in entries.items() if isinstance(value, CapabilityBindingEntry)}
    assert set(bindings) == set(ALL_BINDING_IDS) == set(AGENT_EXECUTION_CONTRACTS)
    assert len(bindings) == 34
    assert not hasattr(composition, "workflow")
    assert composition.manifest.graph_factory_symbol == _PRODUCT_FACTORY
    assert composition.lock.schema_version == "3"
    contracts = all_feature_agent_contracts()
    tasks = all_feature_task_contracts()
    assert len(contracts) == 34
    assert len(contracts) + len(tasks) == 44
    assert not any(item.startswith("assurance.product.agent.") for item in bindings)
    for contract_id, contract in AGENT_EXECUTION_CONTRACTS.items():
        binding = bindings[contract_id]
        assert binding.contract_id == contract.contract_id
        assert binding.data is not None
    descriptor_ids = tuple(descriptor.plugin_id for descriptor in composition.descriptors)
    assert PLUGIN_ID in descriptor_ids
    assert CONFIGURATION_PLUGIN_ID in descriptor_ids
    assert "runtime.opencode" in descriptor_ids
    assert composition.manifest.product_id == "assurance.product"
    assert composition.manifest.source is not None
    assert composition.manifest.source.entrypoint_name == "assurance-opencode"
    assert composition.manifest.source.distribution == "assurance-product"
    assert composition.lock.product.source.kind.value == "wheel_product"
    product_manifest = composition.manifest
    assert getattr(product_manifest, "workflow", None) is None
    assert getattr(product_manifest, "workflow_resource_id", None) is None
    assert getattr(product_manifest, "workflow_module", None) is None
    assert getattr(product_manifest, "workflow_module_resources", ()) == ()
    assert getattr(product_manifest, "workflow_slot_bindings", ()) == ()
    assert set(product_manifest.entrypoints) == set(PRODUCT_ENTRYPOINTS)
    assert len(product_manifest.entrypoints) == 14
    assert set(PUBLIC_BUNDLE_FIELDS) == set(_FEATURE_OWNERS)


def test_wrong_runtime_deployment_fails_closed(installed_sources):
    from assurance_product.product import (
        AssuranceCompositionError,
        AssuranceCompositionRequest,
        resolve_assurance_composition,
    )

    request = AssuranceCompositionRequest.model_construct(
        product_entrypoint="assurance-cursor",
        deployment_source=installed_sources.deployments["opencode"],
        configuration_tree=installed_sources.configuration_tree,
    )
    with pytest.raises((DependencyConflict, ResolutionError, AssuranceCompositionError)):
        resolve_assurance_composition(request)


def test_unselected_adapter_source_is_rejected_before_provider_import(
    installed_sources,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import graph_engine.composition.resolver as resolver_module

    import assurance_product.product as product_module
    from assurance_product.product import resolve_assurance_composition
    from graph_engine.plugin_api import ProviderSource
    from assurance_product.source_catalog import product_source_catalog

    selected_sources = product_source_catalog()
    unselected_source = ProviderSource(
        distribution="agent-runtime-cursor",
        version="0.1.0",
        entrypoint_group="graph_engine.plugins",
        entrypoint_name="cursor",
        entrypoint_value="agent_runtime_cursor.plugin:CursorPlugin",
        declaration_path="agent_runtime_cursor/plugin-declaration.json",
        import_roots=("",),
    )
    assert unselected_source.entrypoint_name == "cursor"
    loaded_unselected_providers: list[str] = []
    original_load = resolver_module._load_snapshotted_entrypoint_binding

    def track_provider_load(source, snapshot, metadata_provider, binding_cache):
        if source.entrypoint_name == unselected_source.entrypoint_name:
            loaded_unselected_providers.append(source.entrypoint_name)
        return original_load(source, snapshot, metadata_provider, binding_cache)

    monkeypatch.setattr(
        product_module,
        "product_source_catalog",
        lambda: (*selected_sources, unselected_source),
    )
    monkeypatch.setattr(
        resolver_module,
        "_load_snapshotted_entrypoint_binding",
        track_provider_load,
    )

    with pytest.raises(
        (DependencyConflict, SourceSnapshotError),
        match="runtime.cursor|agent-runtime-cursor",
    ):
        resolve_assurance_composition(request_for("opencode", installed_sources))

    assert loaded_unselected_providers == []


def test_resolve_accepts_already_imported_assurance_product(opencode_composition):
    import assurance_intake
    import assurance_product

    assert sys.modules["assurance_product"] is assurance_product
    assert sys.modules["assurance_intake"] is assurance_intake
    assert opencode_composition.lock.product.source.kind.value == "wheel_product"
