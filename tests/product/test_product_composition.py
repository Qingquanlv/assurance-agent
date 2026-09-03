from __future__ import annotations

import sys

import pytest

from graph_engine.composition import CapabilityBindingEntry
from graph_engine.composition.dependencies import DependencyConflict
from graph_engine.composition.resolver import ResolutionError

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


@pytest.mark.parametrize("adapter", ["opencode", "cursor"])
def test_composition_has_exact_provider_and_binding_closure(adapter, installed_sources):
    from assurance_product.agent_contracts import all_feature_agent_contracts, all_feature_task_contracts
    from assurance_product.product import resolve_assurance_composition

    composition = resolve_assurance_composition(request_for(adapter, installed_sources))
    assert composition.lock.engine_api == "2.0"
    entries = composition.registries.capabilities.entries
    bindings = {key: value for key, value in entries.items() if isinstance(value, CapabilityBindingEntry)}
    assert set(bindings) == set(ALL_BINDING_IDS)
    assert len(bindings) == 33
    assert not hasattr(composition, "workflow")
    assert composition.manifest.graph_factory_symbol == _PRODUCT_FACTORY
    assert isinstance(composition.lock, type(composition.lock))
    assert composition.lock.schema_version == "3"
    contracts = all_feature_agent_contracts()
    tasks = all_feature_task_contracts()
    assert len(contracts) == 33
    assert len(contracts) + len(tasks) == 41


@pytest.mark.parametrize("adapter", ["opencode", "cursor"])
def test_composition_binds_semantic_agent_contracts_not_phase_aliases(
    adapter,
    installed_sources,
) -> None:
    from assurance_product.agent_contracts import AGENT_EXECUTION_CONTRACTS
    from assurance_product.product import resolve_assurance_composition

    composition = resolve_assurance_composition(request_for(adapter, installed_sources))
    entries = composition.registries.capabilities.entries
    bindings = {key: value for key, value in entries.items() if isinstance(value, CapabilityBindingEntry)}
    assert set(bindings) == set(AGENT_EXECUTION_CONTRACTS)
    assert not any(item.startswith("assurance.product.agent.") for item in bindings)
    for contract_id, contract in AGENT_EXECUTION_CONTRACTS.items():
        binding = bindings[contract_id]
        assert binding.contract_id == contract.contract_id
        assert binding.data is not None


@pytest.mark.parametrize("adapter", ["opencode", "cursor"])
def test_composition_selects_exact_plugin_and_product_identity(adapter, installed_sources):
    from assurance_product.models import CONFIGURATION_PLUGIN_ID, PLUGIN_ID
    from assurance_product.product import resolve_assurance_composition

    composition = resolve_assurance_composition(request_for(adapter, installed_sources))
    descriptor_ids = tuple(descriptor.plugin_id for descriptor in composition.descriptors)
    assert PLUGIN_ID in descriptor_ids
    assert CONFIGURATION_PLUGIN_ID in descriptor_ids
    assert f"runtime.{adapter}" in descriptor_ids
    assert composition.manifest.product_id == "assurance.product"
    assert composition.manifest.source is not None
    assert composition.manifest.source.entrypoint_name == f"assurance-{adapter}"
    assert composition.manifest.source.distribution == "assurance-product"
    assert composition.lock.product.source.kind.value == "wheel_product"


def test_wrong_runtime_deployment_fails_closed(installed_sources):
    from assurance_product.product import (
        AssuranceCompositionError,
        AssuranceCompositionRequest,
        resolve_assurance_composition,
    )

    request = AssuranceCompositionRequest(
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
    from assurance_product.source_catalog import product_source_catalog

    selected_sources = product_source_catalog("opencode")
    unselected_source = product_source_catalog("cursor")[-1]
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
        lambda _adapter: (*selected_sources, unselected_source),
    )
    monkeypatch.setattr(
        resolver_module,
        "_load_snapshotted_entrypoint_binding",
        track_provider_load,
    )

    with pytest.raises(DependencyConflict, match="unexpected selected plugin source: runtime.cursor"):
        resolve_assurance_composition(request_for("opencode", installed_sources))

    assert loaded_unselected_providers == []


def test_resolve_accepts_already_imported_assurance_product(installed_sources):
    import assurance_intake
    import assurance_product
    from assurance_product.product import resolve_assurance_composition

    assert sys.modules["assurance_product"] is assurance_product
    assert sys.modules["assurance_intake"] is assurance_intake
    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    assert composition.lock.product.source.kind.value == "wheel_product"


@pytest.mark.parametrize("adapter", ["opencode", "cursor"])
def test_product_manifest_uses_only_the_graph_factory_form(adapter, installed_sources):
    from assurance_product.models import PRODUCT_ENTRYPOINTS
    from assurance_product.product import resolve_assurance_composition

    resolved = resolve_assurance_composition(request_for(adapter, installed_sources))
    product_manifest = resolved.manifest
    assert not hasattr(product_manifest, "workflow")
    assert getattr(product_manifest, "workflow", None) is None
    assert getattr(product_manifest, "workflow_resource_id", None) is None
    assert getattr(product_manifest, "workflow_module", None) is None
    assert getattr(product_manifest, "workflow_module_resources", ()) == ()
    assert getattr(product_manifest, "workflow_slot_bindings", ()) == ()
    assert product_manifest.graph_factory_symbol == _PRODUCT_FACTORY
    assert set(product_manifest.entrypoints) == set(PRODUCT_ENTRYPOINTS)
    assert len(product_manifest.entrypoints) == 14
    assert set(PUBLIC_BUNDLE_FIELDS) == set(_FEATURE_OWNERS)
    assert not hasattr(resolved, "workflow")
    assert getattr(resolved, "workflow", None) is None
    assert resolved.lock.schema_version == "3"
