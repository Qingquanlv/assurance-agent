from __future__ import annotations

import pytest

from graph_engine.composition import CapabilityBindingEntry

from tests.product.composition_harness import (
    SHADOW_VALIDATOR_CLONE_ID,
    coverage_bytes,
    evict_generated_binding_modules,
    project_binding_coverage,
    request_for,
)
from tests.product.conformance import ALL_BINDING_IDS


def test_repeated_resolution_matches_session_composition(installed_sources, opencode_composition):
    evict_generated_binding_modules()
    from assurance_product.product import resolve_assurance_composition

    fresh = resolve_assurance_composition(request_for("opencode", installed_sources))
    cached = project_binding_coverage(opencode_composition)
    resolved = project_binding_coverage(fresh)
    assert coverage_bytes(cached) == coverage_bytes(resolved)
    assert opencode_composition.lock.canonical_bytes == fresh.lock.canonical_bytes
    assert opencode_composition.digest == fresh.digest


def test_binding_coverage_is_the_authenticated_opencode_projection(opencode_composition):
    projection = project_binding_coverage(opencode_composition)
    assert set(projection) == set(ALL_BINDING_IDS)
    assert len(projection) == 26
    assert not any(item.endswith(".finalize") for item in projection)
    assert not any(item.startswith("assurance.product.agent.") for item in projection)
    assert SHADOW_VALIDATOR_CLONE_ID not in projection
    assert SHADOW_VALIDATOR_CLONE_ID not in ALL_BINDING_IDS
    bindings = {
        key: value
        for key, value in opencode_composition.registries.capabilities.entries.items()
        if isinstance(value, CapabilityBindingEntry)
    }
    assert set(bindings) == set(ALL_BINDING_IDS)
    for binding_id, entry in bindings.items():
        assert entry.contract_id == binding_id
        assert projection[binding_id]["contract_id"] == binding_id
        assert projection[binding_id]["data"] is not None


def test_cursor_adapter_is_rejected(installed_sources):
    evict_generated_binding_modules()
    with pytest.raises(ValueError, match="unsupported adapter"):
        request_for("cursor", installed_sources)
