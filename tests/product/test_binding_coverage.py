from __future__ import annotations

from tests.product.composition_harness import (
    COVERAGE_PATH,
    SHADOW_VALIDATOR_CLONE_ID,
    coverage_bytes,
    evict_generated_binding_modules,
    project_binding_coverage,
    request_for,
)
from tests.product.conformance import ALL_BINDING_IDS, load_json


def _contract_id_for_alias(binding_id: str) -> str:
    rest = binding_id.removeprefix("assurance.product.agent.")
    phase = rest.rsplit(".", 1)[-1]
    body = rest.removesuffix(f".{phase}")
    feature, _, base = body.partition(".")
    return f"assurance.{feature}.agent.{base}.v1"


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
    recorded = load_json(COVERAGE_PATH)
    assert set(recorded) == set(ALL_BINDING_IDS) == set(projection)
    assert all(recorded[item]["data"] is None for item in ALL_BINDING_IDS if item.endswith(".finalize"))
    assert projection == recorded
    assert COVERAGE_PATH.read_bytes() == coverage_bytes(projection)


def test_resolved_aliases_carry_the_base_job_contract_id(installed_sources):
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
    for binding_id, entry in bindings.items():
        assert entry.contract_id == _contract_id_for_alias(binding_id)


def test_cursor_resolution_repeats_and_keeps_finalize_null(installed_sources):
    evict_generated_binding_modules()
    from assurance_product.product import resolve_assurance_composition

    first = project_binding_coverage(resolve_assurance_composition(request_for("cursor", installed_sources)))
    second = project_binding_coverage(resolve_assurance_composition(request_for("cursor", installed_sources)))
    assert coverage_bytes(first) == coverage_bytes(second)
    assert all(first[item]["data"] is None for item in ALL_BINDING_IDS if item.endswith(".finalize"))
    assert all(first[item]["secret_handles"] == [] for item in ALL_BINDING_IDS if item.endswith(".finalize"))


def test_shadow_validator_clone_is_absent_from_binding_coverage(installed_sources):
    evict_generated_binding_modules()
    from assurance_product.product import resolve_assurance_composition

    projection = project_binding_coverage(resolve_assurance_composition(request_for("opencode", installed_sources)))
    assert SHADOW_VALIDATOR_CLONE_ID not in projection
    assert SHADOW_VALIDATOR_CLONE_ID not in ALL_BINDING_IDS
