from __future__ import annotations

import pytest

from graph_engine.composition import CapabilityBindingEntry

from tests.product.conformance import ALL_BINDING_IDS
from tests.product.graph_inventory import graph_capability_ids

pytestmark = pytest.mark.usefixtures("installed_sources")


@pytest.mark.parametrize("adapter", ["opencode", "cursor"])
def test_graph_bindings_are_closed_and_inventoried(adapter, compiled_for):
    from assurance_product.agent_contracts import LEGACY_AGENT_PHASE_ALIASES
    from assurance_product.product import audit_full_graph
    from assurance_product.runtime_bindings import AGENT_RUNTIME_BINDINGS

    compiled = compiled_for(adapter)
    audit = audit_full_graph(compiled.workflow, compiled.composition)
    assert audit.missing_bindings == ()
    assert audit.forbidden_direct_targets == ()
    capabilities = graph_capability_ids(compiled.workflow)
    entries = compiled.composition.registries.capabilities.entries
    assert capabilities.issubset(entries)
    bindings = {key: value for key, value in entries.items() if isinstance(value, CapabilityBindingEntry)}
    agent_ids = {
        capability for capability in capabilities if capability.startswith("assurance.product.agent.")
    }
    execute_aliases = {item for item in ALL_BINDING_IDS if item.endswith(".execute")}
    assert execute_aliases <= agent_ids
    assert agent_ids <= set(ALL_BINDING_IDS)
    assert set(bindings) == set(ALL_BINDING_IDS)
    assert len(AGENT_RUNTIME_BINDINGS) == 33
    assert len(compiled.composition.semantic_attempt_contracts) == 41
    assert len(LEGACY_AGENT_PHASE_ALIASES) == 99


@pytest.mark.parametrize("adapter", ["opencode", "cursor"])
def test_graph_has_no_runtime_or_phase4_agent_targets(adapter, compiled_for):
    from assurance_product.product import audit_full_graph

    compiled = compiled_for(adapter)
    audit = audit_full_graph(compiled.workflow, compiled.composition)
    assert audit.forbidden_direct_targets == ()
    for capability in graph_capability_ids(compiled.workflow):
        assert not capability.startswith("runtime.")
        assert ".test." not in capability
        assert not capability.startswith("test.")
