from __future__ import annotations

from tests.product.composition_harness import (
    SHADOW_VALIDATOR_CLONE_ID,
    coverage_bytes,
    evict_generated_binding_modules,
    project_binding_coverage,
    request_for,
)
from tests.product.conformance import ALL_BINDING_IDS


def test_repeated_resolution_is_byte_identical(installed_sources):
    evict_generated_binding_modules()
    from assurance_product.product import resolve_assurance_composition

    first = resolve_assurance_composition(request_for("opencode", installed_sources))
    second = resolve_assurance_composition(request_for("opencode", installed_sources))
    first_projection = project_binding_coverage(first)
    second_projection = project_binding_coverage(second)
    assert coverage_bytes(first_projection) == coverage_bytes(second_projection)
    assert first.lock.canonical_bytes == second.lock.canonical_bytes
    assert first.digest == second.digest


def test_binding_coverage_matches_authenticated_opencode_projection(installed_sources):
    evict_generated_binding_modules()
    from assurance_product.product import resolve_assurance_composition

    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    projection = project_binding_coverage(composition)
    assert set(projection) == set(ALL_BINDING_IDS)
    assert len(projection) == 33
    assert not any(item.endswith(".finalize") for item in projection)
    assert not any(item.startswith("assurance.product.agent.") for item in projection)
    for item in projection.values():
        assert item["contract_id"] in ALL_BINDING_IDS
        assert item["data"] is not None


def test_resolved_bindings_use_semantic_contract_ids(installed_sources):
    evict_generated_binding_modules()
    from graph_engine.composition import CapabilityBindingEntry

    from assurance_product.product import resolve_assurance_composition

    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    bindings = {
        key: value
        for key, value in composition.registries.capabilities.entries.items()
        if isinstance(value, CapabilityBindingEntry)
    }
    assert set(bindings) == set(ALL_BINDING_IDS)
    assert len(bindings) == 33
    for binding_id, entry in bindings.items():
        assert entry.contract_id == binding_id


def test_cursor_resolution_repeats_semantic_bindings(installed_sources):
    evict_generated_binding_modules()
    from assurance_product.product import resolve_assurance_composition

    first = project_binding_coverage(resolve_assurance_composition(request_for("cursor", installed_sources)))
    second = project_binding_coverage(resolve_assurance_composition(request_for("cursor", installed_sources)))
    assert coverage_bytes(first) == coverage_bytes(second)
    assert set(first) == set(ALL_BINDING_IDS)
    assert len(first) == 33
    assert not any(item.endswith(".finalize") for item in first)


def test_shadow_validator_clone_is_absent_from_binding_coverage(installed_sources):
    evict_generated_binding_modules()
    from assurance_product.product import resolve_assurance_composition

    projection = project_binding_coverage(
        resolve_assurance_composition(request_for("opencode", installed_sources))
    )
    assert SHADOW_VALIDATOR_CLONE_ID not in projection
    assert SHADOW_VALIDATOR_CLONE_ID not in ALL_BINDING_IDS
