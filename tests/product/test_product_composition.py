from __future__ import annotations

import sys

import pytest

from graph_engine.composition import CapabilityBindingEntry
from graph_engine.composition.dependencies import DependencyConflict
from graph_engine.composition.resolver import ResolutionError

from tests.product.composition_harness import request_for
from tests.product.conformance import ALL_BINDING_IDS

_FEATURE_OWNERS = (
    "assurance.execution",
    "assurance.generation",
    "assurance.healing",
    "assurance.improvement",
    "assurance.intake",
    "assurance.quality",
)
_PUBLIC_IMPORT_ALIASES = {
    "intake.prepare",
    "intake.case",
    "generation.generate",
    "execution.execute",
    "execution.rerun",
    "quality.assess",
    "quality.issue-review",
    "quality.issue-analyze",
    "quality.issue-reconcile",
    "quality.report",
    "healing.repair-failure",
    "healing.repair-coverage",
    "improvement.archive",
    "improvement.retro",
    "improvement.review",
    "improvement.evaluate",
    "improvement.export",
    "improvement.apply",
    "improvement.rollback",
}
_FEATURE_PREFIXES = tuple(f"{owner}." for owner in _FEATURE_OWNERS)


@pytest.mark.parametrize("adapter", ["opencode", "cursor"])
def test_composition_has_exact_provider_and_binding_closure(adapter, installed_sources):
    from assurance_product.models import finalize_aliases
    from assurance_product.product import resolve_assurance_composition

    composition = resolve_assurance_composition(request_for(adapter, installed_sources))
    assert composition.lock.engine_api == "2.0"
    entries = composition.registries.capabilities.entries
    bindings = {key: value for key, value in entries.items() if isinstance(value, CapabilityBindingEntry)}
    assert set(bindings) == set(ALL_BINDING_IDS)
    assert len(bindings) == 99
    for binding_id in finalize_aliases():
        assert bindings[binding_id].data is None
        assert bindings[binding_id].secret_handles == ()


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
def test_product_manifest_uses_only_the_modular_assembly_form(adapter, installed_sources):
    from assurance_product.agent_contracts import FEATURE_AGENT_JOB_CATALOGS, expand_agent_job_slots
    from assurance_product.product import resolve_assurance_composition

    resolved = resolve_assurance_composition(request_for(adapter, installed_sources))
    product_manifest = resolved.manifest
    assert product_manifest.workflow is None
    assert product_manifest.workflow_resource_id is None
    assert product_manifest.workflow_module is not None
    assert product_manifest.workflow_module.module_id == "assurance.product.workflow"
    assert product_manifest.workflow_module.owner_id == "assurance.product"
    assert product_manifest.workflow_module.module_version == "0.2.0"
    assert product_manifest.workflow_module.name == "assurance"
    assert product_manifest.workflow_module.role == "product"

    requirements = product_manifest.workflow_module_resources
    assert len(requirements) == 6
    assert {(item.owner_id, item.module_id, item.resource_id) for item in requirements} == {
        (
            owner,
            f"{owner}.workflow",
            f"{owner}.workflow.module.v1",
        )
        for owner in _FEATURE_OWNERS
    }

    assert set(product_manifest.workflow_module.imports) == _PUBLIC_IMPORT_ALIASES
    assert len(product_manifest.workflow_module.imports) == 19
    for alias, spec in product_manifest.workflow_module.imports.items():
        feature, export = alias.split(".", 1)
        assert spec.owner_id == f"assurance.{feature}"
        assert spec.module_id == f"assurance.{feature}.workflow"
        assert spec.export == export

    expanded = expand_agent_job_slots(FEATURE_AGENT_JOB_CATALOGS)
    assert len(expanded) == 99
    expected_bindings = {
        (
            f"assurance.{feature}.workflow",
            f"{base}.{phase}",
            alias,
            contract.contract_id,
        )
        for alias, contract in expanded.items()
        for feature, base, phase in (_slot_parts(alias),)
    }
    actual_bindings = {
        (item.module_id, item.slot, item.capability_id, item.contract_id)
        for item in product_manifest.workflow_slot_bindings
    }
    assert len(product_manifest.workflow_slot_bindings) == 99
    assert actual_bindings == expected_bindings

    assert set(resolved.workflow.entrypoints) == {
        "intake",
        "case",
        "full",
        "execute",
        "archive",
        "retro",
        "issue-review",
        "issue-analyze",
        "issue-reconcile",
        "improvement-review",
        "improvement-evaluate",
        "improvement-export",
        "improvement-apply",
        "improvement-rollback",
    }
    root = product_manifest.workflow_module
    assert set(root.graphs) == {f"product-{name}" for name in resolved.workflow.entrypoints}
    assert all(node.kind != "task" for graph in root.graphs.values() for node in graph.nodes.values())
    assert all(
        f"assurance.product.workflow.graph.product-{name}" in resolved.workflow.graphs
        for name in resolved.workflow.entrypoints
    )
    assert all(
        node.capability is None or not node.capability.startswith(_FEATURE_PREFIXES)
        for graph in root.graphs.values()
        for node in graph.nodes.values()
    )
    for graph in root.graphs.values():
        for node in graph.nodes.values():
            if node.kind != "subgraph":
                continue
            local = node.graph is not None and node.graph in root.graphs
            imported = node.graph_import in _PUBLIC_IMPORT_ALIASES
            assert local or imported
            assert node.graph is None or node.graph_import is None


def _slot_parts(alias: str) -> tuple[str, str, str]:
    rest = alias.removeprefix("assurance.product.agent.")
    feature, _, remainder = rest.partition(".")
    base, _, phase = remainder.rpartition(".")
    return feature, base, phase
