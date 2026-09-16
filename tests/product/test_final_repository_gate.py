from __future__ import annotations

from pathlib import Path
from typing import Mapping, MutableMapping, cast

import pytest

from tests.product import conformance

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_REPOSITORY_GATE_NODE_IDS = {
    "graph_reachability": (
        "tests/product/test_full_graph_audit.py::test_full_graph_has_no_orphans_or_forbidden_targets",
        "tests/product/test_full_graph_audit.py::test_full_graph_has_no_archive_branch_and_keeps_retro_improvement",
    ),
    "wheel_repository_isolation": (
        "packages/adapters/agent-runtime-contracts/tests/test_models.py::test_isolated_wheel_import_does_not_load_adapters_or_assurance",
        "tests/capabilities/test_six_wheel_composition.py::test_fixture_product_is_absent_from_workspace_dependencies_archives_and_entrypoints",
        "tests/product/test_product_packaging.py::test_wheels_omit_whole_tree_modules_and_result_export_schema",
        "tests/product/test_wheel_smoke_contract.py::test_wheel_smoke_covers_isolated_selection_and_binding_fault_matrix",
        "tests/product/test_product_providers.py::test_source_catalog_is_six_wheels_plus_opencode",
    ),
}


def _plain_manifest(value: Mapping[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    return {category: tuple(node_ids) for category, node_ids in value.items()}


def test_repository_gate_manifest_is_exact_closed_and_immutable() -> None:
    manifest = conformance.REPOSITORY_GATE_NODE_IDS

    assert _plain_manifest(manifest) == EXPECTED_REPOSITORY_GATE_NODE_IDS
    with pytest.raises(TypeError):
        cast(MutableMapping[str, tuple[str, ...]], manifest)["wheel_repository_isolation"] = ()


def test_all_final_gate_nodes_are_unique_auditable_and_collectable() -> None:
    all_final_gate_node_ids = conformance.all_final_gate_node_ids
    audit_gate_nodes = conformance.audit_gate_nodes

    node_ids = all_final_gate_node_ids()
    assert len(node_ids) == len(set(node_ids))
    assert len(node_ids) == 105
    evidence = audit_gate_nodes(REPO_ROOT, node_ids)
    assert tuple(item.node_id for item in evidence) == node_ids


def test_task3_admission_is_reported_without_fabricating_a_local_pass() -> None:
    admission_state = conformance.phase5_opencode_admission_state

    state = admission_state(REPO_ROOT)
    assert state.local_gate_disposition == "run"
    assert state.release_disposition in {"blocked", "requires_task3_evidence_validation"}
    assert state.release_disposition != "admitted"
    assert state.detail
