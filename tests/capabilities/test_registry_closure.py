from __future__ import annotations

from graph_engine import ENGINE_API_VERSION
from tests.capabilities.registry import (
    all_six_provider_values,
    contribution_ids,
    every_schema_resource_and_effect_reference_resolves,
)


def test_every_registry_reference_resolves_exactly_once() -> None:
    descriptors, contributions = all_six_provider_values()
    ids = contribution_ids(contributions)
    assert ENGINE_API_VERSION == "2.0"
    assert len(descriptors) == 6
    assert len(ids) == len(set(ids))
    assert every_schema_resource_and_effect_reference_resolves(descriptors, contributions)
    assert all(not descriptor.bindings for descriptor in descriptors)
    assert all(not contribution.bindings for contribution in contributions)
