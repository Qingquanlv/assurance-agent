from __future__ import annotations

from tests.phase5.composition_harness import (
    COVERAGE_PATH,
    coverage_bytes,
    project_binding_coverage,
    request_for,
)
from tests.phase5.conformance import ALL_BINDING_IDS, load_json


def test_repeated_resolution_is_byte_identical(installed_sources):
    from assurance_product.product import resolve_assurance_composition

    first = resolve_assurance_composition(request_for("opencode", installed_sources))
    second = resolve_assurance_composition(request_for("opencode", installed_sources))
    first_projection = project_binding_coverage(first)
    second_projection = project_binding_coverage(second)
    assert coverage_bytes(first_projection) == coverage_bytes(second_projection)
    assert first.lock.canonical_bytes == second.lock.canonical_bytes
    assert first.digest == second.digest


def test_binding_coverage_matches_authenticated_opencode_projection(installed_sources):
    from assurance_product.product import resolve_assurance_composition

    composition = resolve_assurance_composition(request_for("opencode", installed_sources))
    projection = project_binding_coverage(composition)
    recorded = load_json(COVERAGE_PATH)
    assert set(recorded) == set(ALL_BINDING_IDS) == set(projection)
    assert all(recorded[item]["data"] is None for item in ALL_BINDING_IDS if item.endswith(".finalize"))
    assert projection == recorded
    assert COVERAGE_PATH.read_bytes() == coverage_bytes(projection)


def test_cursor_resolution_repeats_and_keeps_finalize_null(installed_sources):
    from assurance_product.product import resolve_assurance_composition

    first = project_binding_coverage(resolve_assurance_composition(request_for("cursor", installed_sources)))
    second = project_binding_coverage(resolve_assurance_composition(request_for("cursor", installed_sources)))
    assert coverage_bytes(first) == coverage_bytes(second)
    assert all(first[item]["data"] is None for item in ALL_BINDING_IDS if item.endswith(".finalize"))
    assert all(first[item]["secret_handles"] == [] for item in ALL_BINDING_IDS if item.endswith(".finalize"))
