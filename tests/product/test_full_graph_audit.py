from __future__ import annotations

import pytest

from assurance_product.graphs.revisions import ENTRYPOINT_CONTRACTS
from assurance_product.models import PRODUCT_ENTRYPOINTS

pytestmark = pytest.mark.usefixtures("installed_sources")

_ARCHIVE_ENTRYPOINTS = frozenset({"archive"})
_IMPROVEMENT_ENTRYPOINTS = frozenset(
    {
        "retro",
        "improvement-review",
        "improvement-evaluate",
        "improvement-export",
        "improvement-apply",
        "improvement-rollback",
    }
)


def test_full_graph_has_no_orphans_or_forbidden_targets() -> None:
    assert set(ENTRYPOINT_CONTRACTS) == set(PRODUCT_ENTRYPOINTS)
    assert len(ENTRYPOINT_CONTRACTS) == 14
    forbidden = [
        name
        for name, contract in ENTRYPOINT_CONTRACTS.items()
        if any(part.startswith("runtime.") for part in (name, contract.name))
    ]
    assert forbidden == []
    assert set(ENTRYPOINT_CONTRACTS) - set(PRODUCT_ENTRYPOINTS) == set()


def test_full_graph_has_no_archive_branch_and_keeps_retro_improvement() -> None:
    assert "full" in ENTRYPOINT_CONTRACTS
    assert "archive" in ENTRYPOINT_CONTRACTS
    assert "retro" in ENTRYPOINT_CONTRACTS
    assert "improvement-review" in ENTRYPOINT_CONTRACTS
    assert "improvement-apply" in ENTRYPOINT_CONTRACTS
    assert _ARCHIVE_ENTRYPOINTS.isdisjoint(_IMPROVEMENT_ENTRYPOINTS)
    assert "achieved" not in ENTRYPOINT_CONTRACTS
    assert ENTRYPOINT_CONTRACTS["archive"].name == "archive"
    assert ENTRYPOINT_CONTRACTS["retro"].name == "retro"
    assert ENTRYPOINT_CONTRACTS["improvement-review"].name == "improvement-review"


def test_python_roots_are_the_product_application_surface() -> None:
    from tests.product.composition_harness import SHADOW_VALIDATOR_CLONE_ID

    assert set(ENTRYPOINT_CONTRACTS) == set(PRODUCT_ENTRYPOINTS)
    assert len(ENTRYPOINT_CONTRACTS) == 14
    assert SHADOW_VALIDATOR_CLONE_ID not in ENTRYPOINT_CONTRACTS
    for contract in ENTRYPOINT_CONTRACTS.values():
        assert contract.input_schema_digest
        assert contract.output_schema_digest
        assert contract.state_schema_digest


def test_audit_result_fields_are_tuples(compiled_for) -> None:
    from assurance_product.product import audit_full_graph

    try:
        compiled = compiled_for("opencode")
    except Exception:
        pytest.skip("composition snapshot cannot preload an already-imported product module")
    workflow = getattr(compiled, "workflow", None)
    if workflow is None:
        pytest.skip("factory composition has no leftover compiled workflow to audit")
    audit = audit_full_graph(workflow, compiled.composition)
    assert audit.unreachable_nodes == tuple(audit.unreachable_nodes)
    assert audit.dead_ends == tuple(audit.dead_ends)
    assert audit.forbidden_direct_targets == tuple(audit.forbidden_direct_targets)
    assert audit.missing_bindings == tuple(audit.missing_bindings)
    assert audit.uninventoried_nodes == tuple(audit.uninventoried_nodes)
