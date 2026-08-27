from __future__ import annotations

from pathlib import Path
from typing import Mapping, MutableMapping, cast

import pytest

from tests.product import conformance

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_SECURITY_GATE_NODE_IDS = {
    "source_authentication": (
        "tests/product/test_composition_authority.py::test_forged_deployment_declaration_fails_closed",
        "tests/product/test_composition_authority.py::test_mutated_config_tree_changes_lock",
        "tests/product/test_product_input.py::test_product_input_authenticates_resource_refs_against_composition",
        "tests/phase4/test_six_wheel_composition.py::test_binding_digests_recompute_from_checked_in_bytes",
        "packages/graph-engine/tests/runtime/test_engine.py::test_open_rejects_a_distinct_authenticated_handler_source",
    ),
    "adapter_confinement": (
        "tests/phase4/test_path_confinement.py::test_path_cases_fail_before_spawn_and_stay_inside_workspace[absolute-intake]",
        "tests/phase4/test_path_confinement.py::test_path_cases_fail_before_spawn_and_stay_inside_workspace[parent-dotdot-generation]",
        "tests/phase4/test_path_confinement.py::test_path_cases_fail_before_spawn_and_stay_inside_workspace[windows-drive-execution]",
        "tests/phase4/test_path_confinement.py::test_path_cases_fail_before_spawn_and_stay_inside_workspace[symlink-file-healing]",
        "tests/phase4/test_path_confinement.py::test_path_cases_fail_before_spawn_and_stay_inside_workspace[symlink-parent-intake]",
        "tests/phase4/test_path_confinement.py::test_path_cases_fail_before_spawn_and_stay_inside_workspace[hard-link-generation]",
        "tests/phase4/test_path_confinement.py::test_path_cases_fail_before_spawn_and_stay_inside_workspace[path-swap-execution]",
        "tests/phase4/test_path_confinement.py::test_path_cases_fail_before_spawn_and_stay_inside_workspace[undeclared-write-root-execution]",
        "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_dual_root_workspace_identity_drift_is_fail_closed",
        "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_bound_cancel_reconcile_rejects_live_root_drift_with_same_identity",
        "packages/agent-runtime-cursor/tests/test_process_host_security.py::test_spawn_rejects_workspace_identity_drift",
    ),
    "secret_redaction": (
        "packages/agent-runtime-opencode/tests/test_credentials.py::test_success_durable_outputs_and_workspace_have_no_canary",
        "packages/agent-runtime-opencode/tests/test_credentials.py::test_provider_error_redacts_canary_from_typed_failure",
        "packages/agent-runtime-contracts/tests/test_schema.py::test_bound_redacted_diagnostics_redact_before_limiting",
        "tests/product/test_binding_builder_security.py::test_built_wheel_contains_no_secret_bytes",
    ),
}


def _plain_manifest(value: Mapping[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    return {category: tuple(node_ids) for category, node_ids in value.items()}


def test_security_gate_manifest_is_exact_closed_and_immutable() -> None:
    manifest = conformance.SECURITY_GATE_NODE_IDS

    assert _plain_manifest(manifest) == EXPECTED_SECURITY_GATE_NODE_IDS
    with pytest.raises(TypeError):
        cast(MutableMapping[str, tuple[str, ...]], manifest)["source_authentication"] = ()


def test_security_gate_nodes_are_auditable_and_cannot_be_skipped() -> None:
    audit_gate_nodes = conformance.audit_gate_nodes
    manifest = conformance.SECURITY_GATE_NODE_IDS

    for node_ids in manifest.values():
        evidence = audit_gate_nodes(REPO_ROOT, node_ids)
        assert tuple(item.node_id for item in evidence) == node_ids
        assert all(item.line_number > 0 and len(item.source_sha256) == 64 for item in evidence)


def test_security_gate_executes_the_exact_lower_level_suite() -> None:
    run_gate_nodes = conformance.run_gate_nodes
    manifest = conformance.SECURITY_GATE_NODE_IDS

    node_ids = tuple(node_id for category in manifest.values() for node_id in category)
    result = run_gate_nodes(REPO_ROOT, node_ids)

    assert result.passed_count == len(node_ids)
    assert result.skipped_count == 0
