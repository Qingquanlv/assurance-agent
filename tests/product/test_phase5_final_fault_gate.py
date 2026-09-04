from __future__ import annotations

from pathlib import Path
from typing import Mapping, MutableMapping, cast

import pytest

from tests.product import conformance

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_PHASE5_FAULT_IDS = (
    "deployment-route-invalid",
    "deployment-route-unknown",
    "deployment-route-missing",
    "deployment-route-duplicate",
    "deployment-route-fallback",
    "deployment-raw-secret",
    "deployment-arbitrary-code",
    "builder-before-file-publication",
    "builder-after-file-publication",
    "builder-before-wheel-publication",
    "builder-after-wheel-publication",
    "generated-declaration-contribution-mismatch",
    "wrong-adapter-dependency",
    "unselected-adapter-source",
    "deployment-wheel-drift",
    "binding-through-project-config",
    "project-config-executable-shape",
    "project-config-runtime-authority",
    "alias-missing",
    "alias-extra",
    "alias-forged",
    "seed-capture-cut",
    "seed-captured-before-start",
    "initial-tree-publication-cut",
    "initial-tree-durable-before-intent",
    "start-intent-publication-cut",
    "bootstrap-before-append",
    "bootstrap-after-append",
    "bootstrap-before-directory-fsync",
    "repeated-start-root-input-drift",
    "repeated-start-seed-drift",
    "projection-missing-root-pointer",
    "projection-missing-predecessor",
    "projection-duplicate-predecessor-token",
    "projection-wrong-join-cardinality",
    "projection-token-schema-mismatch",
    "projection-noncanonical-pointer",
    "projection-replay-drift",
    "host-before-worker-spawn",
    "host-after-spawn-before-dispatch",
    "host-during-activity-rpc",
    "host-after-reference-bind",
    "host-response-before-quiescence",
    "host-terminal-receipt-publication",
    "host-receipt-durable-before-ack",
    "host-parent-crash-live-descendants",
    "host-secret-channel-disconnect",
    "host-secret-channel-revocation",
    "host-worker-source-drift",
    "host-duplicate-terminal-receipt",
    "host-foreign-terminal-receipt",
    "opencode-ambiguous-session-create",
    "opencode-empty-ambiguous-discovery",
    "opencode-duplicate-metadata",
    "opencode-prompt-admission-lost-response",
    "opencode-sse-disconnect",
    "opencode-transient-idle",
    "opencode-cancel-result-race",
    "opencode-terminal-before-restart",
    "opencode-provider-state-deleted-after-receipt",
    "effect-before-intent",
    "effect-after-intent",
    "effect-receipt-publication",
    "effect-reconcile-lost-ack",
    "export-file-write",
    "export-rename",
    "export-directory-fsync",
    "export-destination-race",
    "export-destination-symlink",
    "export-destination-hardlink",
    "comparison-input-drift",
    "comparison-one-side-running",
    "comparison-partial-report",
)

EXPECTED_SUPERSEDED_FAULT_IDS = (
    "seed-capture-cut",
    "seed-captured-before-start",
    "initial-tree-publication-cut",
    "initial-tree-durable-before-intent",
    "repeated-start-seed-drift",
    "comparison-input-drift",
    "comparison-one-side-running",
    "comparison-partial-report",
)

EXPECTED_GAP_FAULT_IDS = (
    "opencode-terminal-before-restart",
    "opencode-provider-state-deleted-after-receipt",
)

EXPECTED_FAULT_GATE_NODE_IDS = {
    "provider_state_loss_replay": (
        "packages/framework/graph-engine/tests/runtime/test_staged_promotion_recovery.py::test_recovery_consumes_durable_promotion_without_reexecuting_handler",
        "tests/product/test_replay_properties.py::test_publish_replay_matches_uninterrupted_projection_for_every_ordered_crash_subset",
    ),
    "fault_crash_recovery": (
        "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_create_cuts_never_issue_a_second_post[before_create]",
        "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_create_cuts_never_issue_a_second_post[after_create_before_response]",
        "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_create_cuts_never_issue_a_second_post[invalid_success_body]",
        "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_create_cuts_never_issue_a_second_post[proxy_reset]",
        "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_prompt_cuts_converge_to_one_admission[before_prompt_post]",
        "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_prompt_cuts_converge_to_one_admission[after_admission_before_response]",
        "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_prompt_cuts_converge_to_one_admission[after_lost_success_response]",
        "tests/product/test_publish_recovery.py::test_crash_after_journal_phase_then_resume[prepared]",
        "tests/product/test_publish_recovery.py::test_crash_after_journal_phase_then_resume[replacing]",
        "tests/product/test_publish_recovery.py::test_crash_after_journal_phase_then_resume[committed]",
    ),
    "stop_interrupt": (
        "tests/product/test_stop_and_interrupts.py::test_revision_mismatch_rejects_drifted_resume",
        "tests/product/test_stop_and_interrupts.py::test_invalid_resume_input_fails",
        "packages/framework/graph-engine/tests/application/test_application_interrupts.py::test_disallowed_human_action_raises_with_actual_value",
        "tests/product/test_stop_and_interrupts.py::test_interrupt_runtime_lives_under_the_change_without_tree_store",
    ),
    "coverage_healing": (
        "tests/product/test_coverage_loop.py::test_low_coverage_reenters_generation_until_policy_passes",
        "tests/product/test_coverage_loop.py::test_coverage_repair_exhaustion_stops_with_a_report",
        "tests/product/test_issue_healing_flow.py::test_issue_path_runs_analysis_and_fix_before_rerun",
        "tests/product/test_issue_healing_flow.py::test_product_issue_never_runs_the_healing_chain",
    ),
}


def _plain_manifest(value: Mapping[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    return {category: tuple(node_ids) for category, node_ids in value.items()}


def test_fault_gate_manifest_is_exact_closed_and_immutable() -> None:
    manifest = conformance.FAULT_GATE_NODE_IDS

    plain_manifest = _plain_manifest(manifest)
    assert set(plain_manifest) == {*EXPECTED_FAULT_GATE_NODE_IDS, "task26_fault_coverage"}
    assert {
        category: node_ids
        for category, node_ids in plain_manifest.items()
        if category != "task26_fault_coverage"
    } == EXPECTED_FAULT_GATE_NODE_IDS
    with pytest.raises(TypeError):
        cast(MutableMapping[str, tuple[str, ...]], manifest)["fault_crash_recovery"] = ()


def test_original_task26_fault_rows_have_an_exact_closed_node_mapping() -> None:
    fault_ids = conformance.PHASE5_FAULT_IDS
    coverage = conformance.PHASE5_FAULT_NODE_IDS
    manifest = conformance.FAULT_GATE_NODE_IDS

    assert fault_ids == EXPECTED_PHASE5_FAULT_IDS
    assert tuple(coverage) == tuple(
        fault_id
        for fault_id in EXPECTED_PHASE5_FAULT_IDS
        if conformance.PHASE5_FAULT_EVIDENCE[fault_id].evidence_kind == "direct"
    )
    assert len(coverage) == len(set(coverage)) == 63
    selected_nodes = {node_id for category in manifest.values() for node_id in category}
    assert set(coverage.values()) <= selected_nodes
    evidence = conformance.audit_gate_nodes(REPO_ROOT, tuple(dict.fromkeys(coverage.values())))
    assert tuple(item.node_id for item in evidence) == tuple(dict.fromkeys(coverage.values()))


def test_fault_evidence_classification_is_truthful_and_release_remains_blocked() -> None:
    coverage = conformance.PHASE5_FAULT_EVIDENCE
    coverage_state = conformance.phase5_fault_coverage_state

    assert tuple(coverage) == EXPECTED_PHASE5_FAULT_IDS
    direct = tuple(fault_id for fault_id, item in coverage.items() if item.evidence_kind == "direct")
    superseded = tuple(fault_id for fault_id, item in coverage.items() if item.evidence_kind == "superseded")
    gaps = tuple(fault_id for fault_id, item in coverage.items() if item.evidence_kind == "gap")
    assert len(direct) == 63
    assert superseded == EXPECTED_SUPERSEDED_FAULT_IDS
    assert gaps == EXPECTED_GAP_FAULT_IDS
    assert all(
        coverage[fault_id].node_id == conformance.PHASE5_FAULT_NODE_IDS[fault_id] for fault_id in direct
    )
    assert all(coverage[fault_id].replacement_node_id for fault_id in superseded)
    assert all(coverage[fault_id].node_id is None for fault_id in gaps)

    state = coverage_state()
    assert state.local_gate_disposition == "run"
    assert state.release_complete is False
    assert state.superseded_fault_ids == EXPECTED_SUPERSEDED_FAULT_IDS
    assert state.gap_fault_ids == EXPECTED_GAP_FAULT_IDS
    assert state.direct_count == 63
    assert state.superseded_count == 8
    assert state.gap_count == 2
    assert "blocked" in state.detail.lower()


def test_fault_gate_nodes_are_auditable_and_cannot_be_skipped() -> None:
    audit_gate_nodes = conformance.audit_gate_nodes
    manifest = conformance.FAULT_GATE_NODE_IDS

    for node_ids in manifest.values():
        evidence = audit_gate_nodes(REPO_ROOT, node_ids)
        assert tuple(item.node_id for item in evidence) == node_ids
        assert all(item.line_number > 0 and len(item.source_sha256) == 64 for item in evidence)
