from __future__ import annotations

import pytest

from graph_engine.composition import CapabilityBindingEntry
from graph_engine.composition.dependencies import DependencyConflict
from graph_engine.composition.resolver import ResolutionError

from tests.phase5.composition_harness import request_for
from tests.phase5.conformance import ALL_BINDING_IDS


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
