from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("installed_sources")


@pytest.mark.parametrize("adapter", ["opencode", "cursor"])
def test_full_graph_has_no_orphans_or_forbidden_targets(adapter, compiled_for):
    from assurance_product.product import audit_full_graph

    audit = audit_full_graph(compiled_for(adapter).workflow, compiled_for(adapter).composition)
    assert audit.unreachable_nodes == ()
    assert audit.dead_ends == ()
    assert audit.forbidden_direct_targets == ()
    assert audit.missing_bindings == ()
    assert audit.uninventoried_nodes == ()


@pytest.mark.parametrize("adapter", ["opencode", "cursor"])
def test_audit_result_fields_are_tuples(adapter, compiled_for):
    from assurance_product.product import audit_full_graph

    audit = audit_full_graph(compiled_for(adapter).workflow, compiled_for(adapter).composition)
    assert audit.unreachable_nodes == tuple(audit.unreachable_nodes)
    assert audit.dead_ends == tuple(audit.dead_ends)
    assert audit.forbidden_direct_targets == tuple(audit.forbidden_direct_targets)
    assert audit.missing_bindings == tuple(audit.missing_bindings)
    assert audit.uninventoried_nodes == tuple(audit.uninventoried_nodes)
