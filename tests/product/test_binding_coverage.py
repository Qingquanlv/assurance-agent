from __future__ import annotations

import pytest

from graph_engine.composition import CapabilityBindingEntry

from tests.product.composition_harness import (
    SHADOW_VALIDATOR_CLONE_ID,
    evict_generated_binding_modules,
    project_binding_coverage,
    request_for,
)
from tests.product.conformance import ALL_BINDING_IDS


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
