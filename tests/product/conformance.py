from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping, Sequence

import yaml


@dataclass(frozen=True, slots=True)
class AssemblyFaultEvidence:
    evidence_kind: Literal["direct", "superseded", "gap"]
    node_id: str | None = None
    reason: str | None = None
    replacement_node_id: str | None = None


@dataclass(frozen=True, slots=True)
class AssemblyFaultCoverageState:
    local_gate_disposition: Literal["run"]
    release_complete: bool
    direct_count: int
    superseded_count: int
    gap_count: int
    superseded_fault_ids: tuple[str, ...]
    gap_fault_ids: tuple[str, ...]
    detail: str


EVIDENCE_ROOT = Path(__file__).resolve().parent / "fixtures" / "assembly"

HISTORICAL_PREPARE_IDS = (
    "assurance.intake.case-design.prepare",
    "assurance.intake.case-repair.prepare",
    "assurance.intake.case-review.prepare",
    "assurance.intake.explore.prepare",
    "assurance.intake.intake.prepare",
    "assurance.generation.api.codegen.prepare",
    "assurance.generation.api.codegen-review.prepare",
    "assurance.generation.e2e.codegen.prepare",
    "assurance.generation.e2e.codegen-review.prepare",
    "assurance.generation.fuzz.codegen.prepare",
    "assurance.generation.fuzz.codegen-review.prepare",
    "assurance.generation.performance.codegen.prepare",
    "assurance.generation.performance.codegen-review.prepare",
    "assurance.execution.execute.prepare",
    "assurance.execution.run.prepare",
    "assurance.healing.coverage-repair.prepare",
    "assurance.healing.fix-proposal.prepare",
    "assurance.quality.fact-baseline.prepare",
    "assurance.quality.inspect.prepare",
    "assurance.quality.issue-analysis.prepare",
    "assurance.quality.issue-triage.prepare",
    "assurance.quality.report.prepare",
    "assurance.improvement.archive.prepare",
    "assurance.improvement.improvement-review.prepare",
    "assurance.improvement.retro-eval-analysis.prepare",
    "assurance.improvement.retro-issue-analysis.prepare",
    "assurance.improvement.retro-workflow-analysis.prepare",
    "assurance.improvement.retro.prepare",
)


def _semantic_contract_id(prepare_id: str) -> str:
    rest = prepare_id.removeprefix("assurance.").removesuffix(".prepare")
    feature, _, base = rest.partition(".")
    return f"assurance.{feature}.agent.{base}.v1"


PREPARE_IDS = tuple(
    contract_id
    for contract_id in (
        tuple(_semantic_contract_id(item) for item in HISTORICAL_PREPARE_IDS)
        + ("assurance.healing.agent.apply-test-repair.v1",)
    )
    if contract_id not in {"assurance.execution.agent.execute.v1", "assurance.execution.agent.run.v1"}
)
HISTORICAL_BINDING_IDS = tuple(
    f"assurance.product.agent.{item.removeprefix('assurance.').removesuffix('.prepare')}.{phase}"
    for item in HISTORICAL_PREPARE_IDS
    for phase in ("prepare", "execute", "finalize")
)

PURE_DECISION_IDS = (
    "assurance.generation.complete",
    "assurance.generation.review-round.advance",
    "assurance.healing.repair-round.advance",
)
SEMANTIC_TRACE_IGNORED_FIELDS = frozenset(
    {
        "alias_id",
        "activation_id",
        "token_id",
        "checkpoint_id",
        "node_name",
        "graph_count",
    }
)

ALL_BINDING_IDS = PREPARE_IDS

EXPECTED_25_CASE_IDS = (
    "full-api-only-success",
    "full-e2e-only-success",
    "full-fuzz-only-success",
    "full-performance-only-success",
    "full-all-four-family-success",
    "intake-review-needs-fix-then-pass",
    "plan-review-invalid-output-bounded-retry",
    "codegen-validation-failure-bounded-fix",
    "execution-closed-mapping-no-stale-test",
    "coverage-insufficient-repair-reexecution-pass",
    "coverage-repair-no-progress-exhausted",
    "healing-disallowed-business-stop",
    "report-generation-required-outputs",
    "issue-analysis-reconcile-path",
    "archive-durable-effect-replay",
    "retro-collect-analyze-propose-reconcile",
    "improvement-review-evaluate-export-apply",
    "improvement-rollback",
    "human-interrupt-exact-resume",
    "transient-local-retry",
    "opencode-ambiguous-create-recovery",
    "cursor-unknown-process-indeterminate",
    "engine-crash-after-provider-terminal-receipt",
    "config-model-graph-source-drift-rejection",
    "replay-after-provider-state-removal",
)

ASSEMBLY_FAULT_IDS = (
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

SECURITY_GATE_NODE_IDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "source_authentication": (
            "tests/product/test_composition_authority.py::test_forged_deployment_declaration_fails_closed",
            "tests/product/test_cli_compile.py::test_compile_emits_authenticated_v3_lock_without_secrets_or_invocation",
            "tests/product/test_product_input.py::test_product_input_authenticates_resource_refs_against_composition",
            "tests/capabilities/test_six_wheel_composition.py::test_binding_digests_recompute_from_checked_in_bytes",
            "packages/framework/graph-engine/tests/boot/test_source_authentication.py::test_sut_and_cross_owner_symbols_fail_before_import",
        ),
        "adapter_confinement": (
            "tests/capabilities/test_path_confinement.py::test_path_cases_fail_before_spawn_and_stay_inside_workspace[absolute-intake]",
            "tests/capabilities/test_path_confinement.py::test_path_cases_fail_before_spawn_and_stay_inside_workspace[parent-dotdot-generation]",
            "tests/capabilities/test_path_confinement.py::test_path_cases_fail_before_spawn_and_stay_inside_workspace[windows-drive-execution]",
            "tests/capabilities/test_path_confinement.py::test_path_cases_fail_before_spawn_and_stay_inside_workspace[symlink-file-healing]",
            "tests/capabilities/test_path_confinement.py::test_path_cases_fail_before_spawn_and_stay_inside_workspace[symlink-parent-intake]",
            "tests/capabilities/test_path_confinement.py::test_path_cases_fail_before_spawn_and_stay_inside_workspace[hard-link-generation]",
            "tests/capabilities/test_path_confinement.py::test_path_cases_fail_before_spawn_and_stay_inside_workspace[path-swap-execution]",
            "tests/capabilities/test_path_confinement.py::test_path_cases_fail_before_spawn_and_stay_inside_workspace[undeclared-write-root-execution]",
            "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_dual_root_workspace_identity_drift_is_fail_closed",
            "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_bound_cancel_reconcile_rejects_live_root_drift_with_same_identity",
        ),
        "secret_redaction": (
            "packages/adapters/agent-runtime-opencode/tests/test_credentials.py::test_success_durable_outputs_and_workspace_have_no_canary",
            "packages/adapters/agent-runtime-opencode/tests/test_credentials.py::test_provider_error_redacts_canary_from_typed_failure",
            "packages/adapters/agent-runtime-contracts/tests/test_schema.py::test_bound_redacted_diagnostics_redact_before_limiting",
            "tests/product/test_binding_builder_security.py::test_built_wheel_contains_no_secret_bytes",
        ),
    }
)

_FAULT_GATE_SUPPORT_NODE_IDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "provider_state_loss_replay": (
            "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_receipt_before_engine_ack_replays_without_provider",
            "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_replay_without_provider",
            "packages/framework/graph-engine/tests/runtime/test_staged_promotion_recovery.py::test_recovery_consumes_durable_promotion_without_reexecuting_handler",
            "tests/product/test_replay_properties.py::test_modular_resume_against_legacy_lock_leaves_ledger_bytes_unchanged",
        ),
        "fault_crash_recovery": (
            "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_create_cuts_never_issue_a_second_post[before_create]",
            "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_create_cuts_never_issue_a_second_post[after_create_before_response]",
            "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_create_cuts_never_issue_a_second_post[invalid_success_body]",
            "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_create_cuts_never_issue_a_second_post[proxy_reset]",
            "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_prompt_cuts_converge_to_one_admission[before_prompt_post]",
            "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_prompt_cuts_converge_to_one_admission[after_admission_before_response]",
            "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_prompt_cuts_converge_to_one_admission[after_lost_success_response]",
            "tests/product/test_cli_fail_closed.py::test_cli_has_no_export_or_archive_commands",
        ),
        "stop_interrupt": (
            "tests/product/test_stop_and_interrupts.py::test_revision_mismatch_rejects_drifted_resume",
            "tests/product/test_stop_and_interrupts.py::test_invalid_resume_input_fails",
            "packages/framework/graph-engine/tests/application/test_application_interrupts.py::test_disallowed_human_action_raises_with_actual_value",
            "tests/product/test_stop_and_interrupts.py::test_interrupt_runtime_lives_under_the_change_without_tree_store",
        ),
        "coverage_healing": (
            "tests/product/test_coverage_loop.py::test_coverage_insufficient_reenters_the_shared_case_flow",
            "tests/product/test_coverage_loop.py::test_exhausted_coverage_budget_stops_without_report_or_retro",
            "tests/product/test_issue_healing_flow.py::test_applied_test_repair_is_the_only_path_to_rerun",
            "tests/product/test_issue_healing_flow.py::test_fix_proposal_output_cannot_parse_as_applied_repair",
        ),
        "task26_fault_coverage": (
            "tests/product/test_binding_builder.py::test_unknown_prepare_assignment_is_rejected",
            "tests/product/test_composition_authority.py::test_unknown_product_entrypoint_is_rejected",
            "tests/product/test_binding_builder.py::test_missing_prepare_assignment_is_rejected",
            "tests/product/test_composition_authority.py::test_duplicate_binding_id_fails_closed",
            "tests/product/test_composition_authority.py::test_wrong_runtime_is_rejected_before_fallback",
            "tests/product/test_binding_builder_security.py::test_secret_handle_rejects_embedded_values",
            "tests/product/test_project_configuration_security.py::test_python_file_is_rejected",
            "tests/product/test_binding_builder.py::test_interrupted_publication_is_retryable[builder-before-file-publication]",
            "tests/product/test_binding_builder.py::test_interrupted_publication_is_retryable[builder-after-file-publication]",
            "tests/product/test_binding_builder.py::test_interrupted_publication_is_retryable[builder-before-wheel-publication]",
            "tests/product/test_binding_builder.py::test_interrupted_publication_is_retryable[builder-after-wheel-publication]",
            "tests/product/test_generated_declaration_mismatch.py::test_generated_provider_rejects_declaration_contribution_mismatch",
            "tests/product/test_product_composition.py::test_wrong_runtime_deployment_fails_closed",
            "tests/product/test_binding_builder_security.py::test_builder_rejects_capability_source_drift",
            "tests/product/test_project_configuration_security.py::test_bindings_are_rejected_even_when_phase2_would_accept_them",
            "tests/product/test_project_configuration_security.py::test_runtime_adapter_dependency_is_rejected",
            "tests/product/test_binding_builder.py::test_extra_prepare_assignment_is_rejected",
            "packages/framework/graph-engine/tests/application/test_application_lifecycle.py::test_start_does_not_execute_business_node_and_status_is_running",
            "packages/framework/graph-engine/tests/runtime/test_task_workspace.py::test_begin_accepts_an_empty_write_claim_set",
            "packages/framework/graph-engine/tests/boot/test_boot.py::test_offline_compile_uses_no_saver_and_runtime_boot_matches_manifest",
            "packages/framework/graph-engine/tests/persistence/test_checkpoint_recovery.py::test_arecover_completes_the_missing_idempotent_handshake_step[after_journal_append]",
            "packages/framework/graph-engine/tests/persistence/test_journal_contract.py::test_identical_journal_records_are_idempotent_and_divergent_identity_fails_closed",
            "packages/framework/graph-engine/tests/persistence/test_journal_contract.py::test_invocation_started_projects_checkpoint_anchor_state",
            "packages/framework/graph-engine/tests/persistence/test_journal_contract.py::test_journal_port_exposes_idempotent_append_read_and_cas_without_workflow_api",
            "packages/framework/graph-engine/tests/boot/test_boot_manifest_parity.py::test_runtime_boot_rejects_revision_mismatch",
            "packages/framework/graph-engine/tests/attempts/test_kernel_recovery.py::test_replay_rejects_input_contract_and_revision_drift",
            "packages/capabilities/assurance-generation/tests/test_graph_routes.py::test_generation_send_is_only_in_route_families_and_has_no_fanout_shim",
            "packages/capabilities/assurance-generation/tests/test_graph_join_any.py::test_replay_of_the_same_arrival_id_is_deduplicated",
            "packages/capabilities/assurance-generation/tests/test_graph_join_any.py::test_late_second_arrival_in_same_epoch_is_retained_and_dispatched_once",
            "packages/framework/graph-engine/tests/stategraph/test_routing.py::test_select_exclusive_route_raises_on_two_simultaneous_matches",
            "packages/framework/graph-engine/tests/persistence/test_checkpoint_recovery.py::test_integrity_mismatch_on_read_raises_checkpoint_integrity_error[digest_drift]",
            "packages/framework/graph-engine/tests/runtime/test_production_host_faults.py::test_host_before_worker_spawn_fault_has_no_child_dispatch_or_receipt",
            "packages/framework/graph-engine/tests/runtime/test_production_host_faults.py::test_host_after_spawn_before_dispatch_fault_cleans_child_without_receipt",
            "packages/framework/graph-engine/tests/runtime/test_production_host_faults.py::test_host_during_activity_rpc_fault_cleans_child_and_reconciles_safely",
            "packages/framework/graph-engine/tests/runtime/test_host_receipts.py::test_quiescence_rejects_live_writers",
            "packages/framework/graph-engine/tests/runtime/test_host_receipts.py::test_terminal_receipt_sink_is_host_call_scoped",
            "packages/framework/graph-engine/tests/runtime/test_production_host_faults.py::test_production_host_crash_after_receipt_leaves_durable_receipt",
            "packages/framework/graph-engine/tests/runtime/test_production_host_faults.py::test_leftover_process_group_child_still_fails_quiescence",
            "packages/framework/graph-engine/tests/runtime/test_production_host_security.py::test_production_host_revokes_parent_secrets_after_call",
            "packages/framework/graph-engine/tests/runtime/test_production_host_faults.py::test_worker_rejects_substituted_project_root_before_handler_execution",
            "packages/framework/graph-engine/tests/runtime/test_host_receipts.py::test_receipt_store_rejects_partial_symlink_linked_changed_multiple_foreign_and_nonmonotonic",
            "packages/framework/graph-engine/tests/runtime/test_production_host_faults.py::test_production_host_rejects_forged_terminal_receipt",
            "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_temporary_empty_discovery_stays_indeterminate",
            "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_multiple_matches_fail_closed",
            "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_lost_sse_authenticates_with_get",
            "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_idle_with_open_tools_is_not_terminal",
            "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_completion_cancel_race_provider_terminal_wins",
            "tests/capabilities/test_effect_fault_matrix.py::test_effect_crash_cuts_are_at_most_once_and_typed[before_mutation-assurance.improvement.effect.archive.v1]",
            "tests/capabilities/test_effect_fault_matrix.py::test_effect_crash_cuts_are_at_most_once_and_typed[after_mutation-assurance.improvement.effect.archive.v1]",
            "tests/capabilities/test_effect_fault_matrix.py::test_effect_crash_cuts_are_at_most_once_and_typed[before_receipt-assurance.improvement.effect.archive.v1]",
            "tests/capabilities/test_effect_fault_matrix.py::test_effect_crash_cuts_are_at_most_once_and_typed[reconcile_error-assurance.improvement.effect.archive.v1]",
            "tests/product/test_report_flow.py::test_report_is_mandatory_on_success",
        ),
    }
)

_ASSEMBLY_DIRECT_FAULT_NODE_IDS: Mapping[str, str] = MappingProxyType(
    {
        "deployment-route-invalid": (
            "tests/product/test_binding_builder_security.py::test_manifest_rejects_templates_globs_and_secret_values[{{model}}]"
        ),
        "deployment-route-unknown": (
            "tests/product/test_binding_builder.py::test_unknown_prepare_assignment_is_rejected"
        ),
        "deployment-route-missing": (
            "tests/product/test_binding_builder.py::test_missing_prepare_assignment_is_rejected"
        ),
        "deployment-route-duplicate": (
            "tests/product/test_binding_builder.py::test_duplicate_route_assignment_is_rejected_before_build"
        ),
        "deployment-route-fallback": (
            "tests/product/test_binding_builder_security.py::test_manifest_rejects_fallback_lists_in_provider_model"
        ),
        "deployment-raw-secret": (
            "tests/product/test_binding_builder_security.py::test_secret_handle_rejects_embedded_values"
        ),
        "deployment-arbitrary-code": (
            "tests/product/test_binding_builder_security.py::test_manifest_rejects_executable_or_fallback_fields[python]"
        ),
        "builder-before-file-publication": (
            "tests/product/test_binding_builder.py::test_interrupted_publication_is_retryable[builder-before-file-publication]"
        ),
        "builder-after-file-publication": (
            "tests/product/test_binding_builder.py::test_interrupted_publication_is_retryable[builder-after-file-publication]"
        ),
        "builder-before-wheel-publication": (
            "tests/product/test_binding_builder.py::test_interrupted_publication_is_retryable[builder-before-wheel-publication]"
        ),
        "builder-after-wheel-publication": (
            "tests/product/test_binding_builder.py::test_interrupted_publication_is_retryable[builder-after-wheel-publication]"
        ),
        "generated-declaration-contribution-mismatch": (
            "tests/product/test_generated_declaration_mismatch.py::test_generated_provider_rejects_declaration_contribution_mismatch"
        ),
        "wrong-adapter-dependency": (
            "tests/product/test_product_composition.py::test_wrong_runtime_deployment_fails_closed"
        ),
        "unselected-adapter-source": (
            "tests/product/test_product_composition.py::test_unselected_adapter_source_is_rejected_before_provider_import"
        ),
        "deployment-wheel-drift": (
            "tests/product/test_composition_authority.py::test_deployment_wheel_drift_after_resolution_fails_before_provider_import"
        ),
        "binding-through-project-config": (
            "tests/product/test_project_configuration_security.py::test_bindings_are_rejected_even_when_phase2_would_accept_them"
        ),
        "project-config-executable-shape": (
            "tests/product/test_project_configuration_security.py::test_project_config_rejects_ports_entry_points_and_handlers[python]"
        ),
        "project-config-runtime-authority": (
            "tests/product/test_project_configuration_security.py::test_project_config_rejects_runtime_authority[adapter]"
        ),
        "alias-missing": ("tests/product/test_composition_authority.py::test_missing_binding_fails_closed"),
        "alias-extra": ("tests/product/test_composition_authority.py::test_extra_owned_binding_fails_closed"),
        "alias-forged": (
            "tests/product/test_composition_authority.py::test_forged_alias_target_fails_closed"
        ),
        "start-intent-publication-cut": (
            "packages/framework/graph-engine/tests/persistence/test_checkpoint_recovery.py::test_arecover_completes_the_missing_idempotent_handshake_step[after_journal_append]"
        ),
        "bootstrap-before-append": (
            "packages/framework/graph-engine/tests/persistence/test_journal_contract.py::test_identical_journal_records_are_idempotent_and_divergent_identity_fails_closed"
        ),
        "bootstrap-after-append": (
            "packages/framework/graph-engine/tests/persistence/test_attempt_journal.py::test_identical_append_replay_is_idempotent"
        ),
        "bootstrap-before-directory-fsync": (
            "packages/framework/graph-engine/tests/attempts/test_kernel_recovery.py::test_crash_windows_replay_same_receipt_without_repeating_mutation[before_durable_prepare]"
        ),
        "repeated-start-root-input-drift": (
            "packages/framework/graph-engine/tests/attempts/test_kernel_recovery.py::test_replay_rejects_input_contract_and_revision_drift"
        ),
        "projection-missing-root-pointer": (
            "packages/capabilities/assurance-generation/tests/test_graph_join_any.py::test_first_arrival_becomes_exact_current_trigger"
        ),
        "projection-missing-predecessor": (
            "packages/capabilities/assurance-generation/tests/test_graph_join_any.py::test_two_reducer_merge_orders_produce_identical_inbox_state"
        ),
        "projection-duplicate-predecessor-token": (
            "packages/capabilities/assurance-generation/tests/test_graph_join_any.py::test_dispatch_cursor_never_reclaims_a_consumed_arrival"
        ),
        "projection-wrong-join-cardinality": (
            "packages/capabilities/assurance-generation/tests/test_graph_join_any.py::test_join_predecessors_are_the_two_plan_advance_sites"
        ),
        "projection-token-schema-mismatch": (
            "packages/framework/graph-engine/tests/persistence/test_attempt_journal.py::test_corrupt_record_digest_is_rejected"
        ),
        "projection-noncanonical-pointer": (
            "packages/framework/graph-engine/tests/stategraph/test_routing.py::test_select_exclusive_route_rejects_empty_otherwise"
        ),
        "projection-replay-drift": (
            "packages/framework/graph-engine/tests/persistence/test_checkpoint_recovery.py::test_integrity_mismatch_on_read_raises_checkpoint_integrity_error[digest_drift]"
        ),
        "host-before-worker-spawn": (
            "packages/framework/graph-engine/tests/runtime/test_production_host_faults.py::test_host_before_worker_spawn_fault_has_no_child_dispatch_or_receipt"
        ),
        "host-after-spawn-before-dispatch": (
            "packages/framework/graph-engine/tests/runtime/test_production_host_faults.py::test_host_after_spawn_before_dispatch_fault_cleans_child_without_receipt"
        ),
        "host-during-activity-rpc": (
            "packages/framework/graph-engine/tests/runtime/test_production_host_faults.py::test_host_during_activity_rpc_fault_cleans_child_and_reconciles_safely"
        ),
        "host-after-reference-bind": (
            "packages/framework/graph-engine/tests/runtime/test_production_host_faults.py::test_host_after_reference_bind_fault_preserves_bound_activity_for_reconcile"
        ),
        "host-response-before-quiescence": (
            "packages/framework/graph-engine/tests/runtime/test_host_receipts.py::test_quiescence_rejects_live_writers"
        ),
        "host-terminal-receipt-publication": (
            "packages/framework/graph-engine/tests/runtime/test_host_receipts.py::test_terminal_receipt_sink_is_host_call_scoped"
        ),
        "host-receipt-durable-before-ack": (
            "packages/framework/graph-engine/tests/runtime/test_host_receipts.py::test_prepare_receipt_is_not_promotable"
        ),
        "host-parent-crash-live-descendants": (
            "packages/framework/graph-engine/tests/runtime/test_production_host_faults.py::test_parent_process_crash_with_live_descendants_cleans_group_and_leaves_no_receipt"
        ),
        "host-secret-channel-disconnect": (
            "packages/framework/graph-engine/tests/runtime/test_production_host_security.py::test_secret_channel_disconnect_revokes_parent_material_and_cleans_worker"
        ),
        "host-secret-channel-revocation": (
            "packages/framework/graph-engine/tests/runtime/test_production_host_security.py::test_production_host_revokes_parent_secrets_after_call"
        ),
        "host-worker-source-drift": (
            "packages/framework/graph-engine/tests/runtime/test_production_host_faults.py::test_host_rejects_worker_source_drift_before_spawn"
        ),
        "host-duplicate-terminal-receipt": (
            "packages/framework/graph-engine/tests/runtime/test_host_receipts.py::test_receipt_store_rejects_partial_symlink_linked_changed_multiple_foreign_and_nonmonotonic"
        ),
        "host-foreign-terminal-receipt": (
            "packages/framework/graph-engine/tests/runtime/test_production_host_faults.py::test_production_host_rejects_forged_terminal_receipt"
        ),
        "opencode-ambiguous-session-create": (
            "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_create_cuts_never_issue_a_second_post[after_create_before_response]"
        ),
        "opencode-empty-ambiguous-discovery": (
            "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_temporary_empty_discovery_stays_indeterminate"
        ),
        "opencode-duplicate-metadata": (
            "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_multiple_matches_fail_closed"
        ),
        "opencode-prompt-admission-lost-response": (
            "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_prompt_cuts_converge_to_one_admission[after_lost_success_response]"
        ),
        "opencode-sse-disconnect": (
            "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_lost_sse_authenticates_with_get"
        ),
        "opencode-transient-idle": (
            "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_idle_with_open_tools_is_not_terminal"
        ),
        "opencode-cancel-result-race": (
            "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_completion_cancel_race_provider_terminal_wins"
        ),
        "effect-before-intent": (
            "packages/framework/graph-engine/tests/attempts/test_kernel_effect_recovery.py::test_crash_after_intent_applies_once_without_repeating_promotion[assurance.healing.effect.allocation.v2]"
        ),
        "effect-after-intent": (
            "packages/framework/graph-engine/tests/attempts/test_kernel_effects.py::test_apply_reconcile_outcomes_for_every_kind[applied-assurance.improvement.effect.archive.v1]"
        ),
        "effect-receipt-publication": (
            "packages/framework/graph-engine/tests/attempts/test_kernel_effect_recovery.py::test_crash_after_intent_applies_once_without_repeating_promotion[assurance.improvement.effect.archive.v1]"
        ),
        "effect-reconcile-lost-ack": (
            "tests/capabilities/test_effect_fault_matrix.py::test_effect_crash_cuts_are_at_most_once_and_typed[after_receipt-assurance.improvement.effect.archive.v1]"
        ),
    }
)

ASSEMBLY_FAULT_NODE_IDS: Mapping[str, str] = _ASSEMBLY_DIRECT_FAULT_NODE_IDS

_ASSEMBLY_SUPERSEDED_FAULTS: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "seed-capture-cut": (
            "Change-local InvocationSeed has no project-tree capture or WorkspaceSeed authority.",
            "packages/framework/graph-engine/tests/runtime/test_task_workspace.py::test_begin_captures_only_declared_output_baselines",
        ),
        "seed-captured-before-start": (
            "Engine.start requires a process-local descriptor-bound workspace binding and never captures a seed tree.",
            "packages/framework/graph-engine/tests/runtime/test_task_workspace.py::test_begin_accepts_an_empty_write_claim_set",
        ),
        "initial-tree-publication-cut": (
            "SnapshotStore initial-tree publication was removed; each attempt now starts with an empty isolated write root.",
            "packages/framework/graph-engine/tests/runtime/test_task_workspace.py::test_begin_creates_empty_attempt_root_with_stable_path_free_identity",
        ),
        "initial-tree-durable-before-intent": (
            "There is no initial tree durability boundary; the authenticated invocation lock precedes bootstrap.",
            "packages/framework/graph-engine/tests/boot/test_boot.py::test_offline_compile_uses_no_saver_and_runtime_boot_matches_manifest",
        ),
        "repeated-start-seed-drift": (
            "WorkspaceSeed/tree identity was removed; repeated start now authenticates the process-local workspace binding.",
            "packages/framework/graph-engine/tests/runtime/test_production_host_faults.py::test_worker_rejects_substituted_project_root_before_handler_execution",
        ),
        "export-file-write": (
            "aa export / publish_achieved was removed; delivery stops at achieved.",
            "tests/product/test_cli_fail_closed.py::test_cli_has_no_export_or_archive_commands",
        ),
        "export-rename": (
            "aa export / publish_achieved was removed; delivery stops at achieved.",
            "tests/product/test_cli_fail_closed.py::test_cli_has_no_export_or_archive_commands",
        ),
        "export-directory-fsync": (
            "aa export / publish_achieved was removed; delivery stops at achieved.",
            "tests/product/test_cli_fail_closed.py::test_cli_has_no_export_or_archive_commands",
        ),
        "export-destination-race": (
            "aa export / publish_achieved was removed; delivery stops at achieved.",
            "tests/product/test_cli_fail_closed.py::test_cli_has_no_export_or_archive_commands",
        ),
        "export-destination-symlink": (
            "aa export / publish_achieved was removed; delivery stops at achieved.",
            "tests/product/test_cli_fail_closed.py::test_cli_has_no_export_or_archive_commands",
        ),
        "export-destination-hardlink": (
            "aa export / publish_achieved was removed; delivery stops at achieved.",
            "tests/product/test_cli_fail_closed.py::test_cli_has_no_export_or_archive_commands",
        ),
        "comparison-input-drift": (
            "Legacy-vs-current comparison input authentication was removed; replay authentication remains in product resume tests.",
            "tests/product/test_replay_properties.py::test_modular_resume_against_legacy_lock_leaves_ledger_bytes_unchanged",
        ),
        "comparison-one-side-running": (
            "Legacy-vs-current comparison of running terminals was removed; STOP vs completion remains in product interrupt tests.",
            "packages/framework/graph-engine/tests/application/test_application_interrupts.py::test_disallowed_human_action_raises_with_actual_value",
        ),
        "comparison-partial-report": (
            "Comparison-report publication was removed; required report outputs remain in product coverage/report tests.",
            "tests/product/test_product_stategraph_flow.py::test_failed_report_never_enters_retro_or_achieved",
        ),
    }
)

_ASSEMBLY_GAP_REASONS: Mapping[str, str] = MappingProxyType(
    {
        "opencode-terminal-before-restart": (
            "The existing adapter test caches TaskOutcome in memory and never restarts a real Engine invocation."
        ),
        "opencode-provider-state-deleted-after-receipt": (
            "The existing replay fixture returns an in-memory TaskOutcome and does not delete provider state after a durable host receipt."
        ),
    }
)


def _assembly_fault_evidence(fault_id: str) -> AssemblyFaultEvidence:
    direct_node_id = _ASSEMBLY_DIRECT_FAULT_NODE_IDS.get(fault_id)
    if direct_node_id is not None:
        return AssemblyFaultEvidence(evidence_kind="direct", node_id=direct_node_id)
    superseded = _ASSEMBLY_SUPERSEDED_FAULTS.get(fault_id)
    if superseded is not None:
        reason, replacement_node_id = superseded
        return AssemblyFaultEvidence(
            evidence_kind="superseded",
            reason=reason,
            replacement_node_id=replacement_node_id,
        )
    gap_reason = _ASSEMBLY_GAP_REASONS.get(fault_id)
    if gap_reason is None:
        raise AssertionError(f"fault row has no evidence disposition: {fault_id}")
    return AssemblyFaultEvidence(evidence_kind="gap", reason=gap_reason)


ASSEMBLY_FAULT_EVIDENCE: Mapping[str, AssemblyFaultEvidence] = MappingProxyType(
    {fault_id: _assembly_fault_evidence(fault_id) for fault_id in ASSEMBLY_FAULT_IDS}
)

_MISLEADING_PROVIDER_REPLAY_CHARACTERIZATIONS = frozenset(
    {
        "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_receipt_before_engine_ack_replays_without_provider",
        "packages/adapters/agent-runtime-opencode/tests/test_fault_matrix.py::test_replay_without_provider",
    }
)
_FAULT_SUPPORT_CATEGORIES: dict[str, tuple[str, ...]] = {
    category: tuple(
        node_id for node_id in node_ids if node_id not in _MISLEADING_PROVIDER_REPLAY_CHARACTERIZATIONS
    )
    for category, node_ids in _FAULT_GATE_SUPPORT_NODE_IDS.items()
    if category != "task26_fault_coverage"
}
_FAULT_SUPPORT_NODE_IDS = frozenset(
    node_id for node_ids in _FAULT_SUPPORT_CATEGORIES.values() for node_id in node_ids
)
_TASK26_RUNNABLE_EVIDENCE_NODE_IDS = tuple(
    node_id
    for evidence in ASSEMBLY_FAULT_EVIDENCE.values()
    for node_id in (
        evidence.node_id
        if evidence.evidence_kind == "direct"
        else evidence.replacement_node_id
        if evidence.evidence_kind == "superseded"
        else None,
    )
    if node_id is not None and node_id not in _FAULT_SUPPORT_NODE_IDS
)

FAULT_GATE_NODE_IDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        **_FAULT_SUPPORT_CATEGORIES,
        "task26_fault_coverage": _TASK26_RUNNABLE_EVIDENCE_NODE_IDS,
    }
)

REPOSITORY_GATE_NODE_IDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
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
)

FINAL_GATE_NODE_IDS: Mapping[str, Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "security": SECURITY_GATE_NODE_IDS,
        "fault": FAULT_GATE_NODE_IDS,
        "repository": REPOSITORY_GATE_NODE_IDS,
    }
)

_ALLOWED_GATE_SOURCE_PREFIXES = ("packages", "tests")
_FORBIDDEN_PYTEST_CONTROL_NAMES = frozenset({"importorskip", "skip", "skipif", "xfail"})
_ADMISSION_RELATIVE_PATH = Path("tests/product/fixtures/assembly/opencode-admission.json")
_ADMISSION_STATUSES = frozenset({"blocked_by_execution_approval", "complete"})


@dataclass(frozen=True, slots=True)
class GateNodeEvidence:
    node_id: str
    source_path: str
    function_name: str
    line_number: int
    source_sha256: str


@dataclass(frozen=True, slots=True)
class GateRunResult:
    node_ids: tuple[str, ...]
    passed_count: int
    skipped_count: int
    stdout: str


@dataclass(frozen=True, slots=True)
class OpenCodeAdmissionState:
    source_status: str
    local_gate_disposition: Literal["run"]
    release_disposition: Literal["blocked", "requires_task3_evidence_validation"]
    detail: str


def all_final_gate_node_ids() -> tuple[str, ...]:
    node_ids = tuple(
        node_id for gate in FINAL_GATE_NODE_IDS.values() for category in gate.values() for node_id in category
    )
    duplicates = sorted({node_id for node_id in node_ids if node_ids.count(node_id) > 1})
    if duplicates:
        raise AssertionError(f"duplicate final-gate node IDs: {duplicates}")
    return node_ids


def assembly_fault_coverage_state() -> AssemblyFaultCoverageState:
    superseded_fault_ids = tuple(
        fault_id
        for fault_id, evidence in ASSEMBLY_FAULT_EVIDENCE.items()
        if evidence.evidence_kind == "superseded"
    )
    gap_fault_ids = tuple(
        fault_id for fault_id, evidence in ASSEMBLY_FAULT_EVIDENCE.items() if evidence.evidence_kind == "gap"
    )
    direct_count = len(ASSEMBLY_FAULT_EVIDENCE) - len(superseded_fault_ids) - len(gap_fault_ids)
    return AssemblyFaultCoverageState(
        local_gate_disposition="run",
        release_complete=not gap_fault_ids,
        direct_count=direct_count,
        superseded_count=len(superseded_fault_ids),
        gap_count=len(gap_fault_ids),
        superseded_fault_ids=superseded_fault_ids,
        gap_fault_ids=gap_fault_ids,
        detail=(
            "Assembly release evidence is complete: every applicable fault row has direct "
            f"executable evidence and {len(superseded_fault_ids)} retired rows have replacement evidence."
            if not gap_fault_ids
            else "Assembly release remains blocked by "
            f"{len(gap_fault_ids)} explicitly modeled direct-evidence gaps; "
            f"{len(superseded_fault_ids)} retired rows have replacement evidence and deterministic "
            "local fault gates remain runnable."
        ),
    )


def _contains_forbidden_pytest_control(node: ast.AST) -> bool:
    return any(
        (isinstance(candidate, ast.Attribute) and candidate.attr in _FORBIDDEN_PYTEST_CONTROL_NAMES)
        or (isinstance(candidate, ast.Name) and candidate.id in _FORBIDDEN_PYTEST_CONTROL_NAMES)
        for candidate in ast.walk(node)
    )


def _module_has_forbidden_pytest_control(module: ast.Module) -> bool:
    for statement in module.body:
        if isinstance(statement, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef)):
            continue
        if _contains_forbidden_pytest_control(statement):
            return True
    return False


def _split_gate_node_id(node_id: str) -> tuple[Path, str]:
    path_text, separator, selector = node_id.partition("::")
    if separator != "::" or not selector or "::" in selector:
        raise AssertionError(f"gate node ID must select one top-level test: {node_id!r}")
    relative_path = Path(path_text)
    if (
        relative_path.is_absolute()
        or relative_path.as_posix() != path_text
        or any(part in {"", ".", ".."} for part in relative_path.parts)
        or not relative_path.parts
        or relative_path.parts[0] not in _ALLOWED_GATE_SOURCE_PREFIXES
        or relative_path.suffix != ".py"
    ):
        raise AssertionError(f"unsafe final-gate source path: {path_text!r}")
    function_name = selector.partition("[")[0]
    if not function_name.startswith("test_"):
        raise AssertionError(f"gate node ID must name a test function: {node_id!r}")
    return relative_path, function_name


def audit_gate_node(repo_root: Path, node_id: str) -> GateNodeEvidence:
    relative_path, function_name = _split_gate_node_id(node_id)
    canonical_root = repo_root.resolve(strict=True)
    source_path = (canonical_root / relative_path).resolve(strict=True)
    if not source_path.is_relative_to(canonical_root):
        raise AssertionError(f"gate source escapes the repository: {node_id!r}")

    source_bytes = source_path.read_bytes()
    module = ast.parse(source_bytes, filename=str(source_path))
    if _module_has_forbidden_pytest_control(module):
        raise AssertionError(f"module-level skip/xfail control is forbidden: {node_id!r}")
    matches = [
        statement
        for statement in module.body
        if isinstance(statement, (ast.AsyncFunctionDef, ast.FunctionDef)) and statement.name == function_name
    ]
    if len(matches) != 1:
        raise AssertionError(f"gate node ID does not resolve to exactly one test: {node_id!r}")
    function = matches[0]
    if any(_contains_forbidden_pytest_control(node) for node in (*function.decorator_list, *function.body)):
        raise AssertionError(f"selected gate test can skip or xfail: {node_id!r}")
    return GateNodeEvidence(
        node_id=node_id,
        source_path=relative_path.as_posix(),
        function_name=function_name,
        line_number=function.lineno,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
    )


def audit_gate_nodes(repo_root: Path, node_ids: Sequence[str]) -> tuple[GateNodeEvidence, ...]:
    closed_node_ids = tuple(node_ids)
    if not closed_node_ids:
        raise AssertionError("a final gate must contain at least one node ID")
    duplicates = sorted({node_id for node_id in closed_node_ids if closed_node_ids.count(node_id) > 1})
    if duplicates:
        raise AssertionError(f"duplicate node IDs within final gate: {duplicates}")
    return tuple(audit_gate_node(repo_root, node_id) for node_id in closed_node_ids)


def _junit_counts(path: Path) -> tuple[int, int, int, int]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    return tuple(
        sum(int(suite.attrib.get(attribute, "0")) for suite in suites)
        for attribute in ("tests", "failures", "errors", "skipped")
    )  # type: ignore[return-value]


def _run_gate_node_group(repo_root: Path, node_ids: tuple[str, ...]) -> GateRunResult:
    with tempfile.TemporaryDirectory(prefix="assembly-final-gate-") as temporary_directory:
        junit_path = Path(temporary_directory) / "pytest.xml"
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            (
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                f"--junitxml={junit_path}",
                *node_ids,
            ),
            cwd=repo_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
        output = completed.stdout + completed.stderr
        if completed.returncode != 0:
            raise AssertionError(f"closed final-gate suite failed:\n{output}")
        if not junit_path.is_file():
            raise AssertionError(f"closed final-gate suite produced no JUnit evidence:\n{output}")
        tests, failures, errors, skipped = _junit_counts(junit_path)
    if tests != len(node_ids) or failures or errors or skipped:
        raise AssertionError(
            "closed final-gate suite did not execute every node successfully: "
            f"expected={len(node_ids)}, tests={tests}, failures={failures}, "
            f"errors={errors}, skipped={skipped}\n{output}"
        )
    return GateRunResult(
        node_ids=node_ids,
        passed_count=tests,
        skipped_count=skipped,
        stdout=output,
    )


def run_gate_nodes(repo_root: Path, node_ids: Sequence[str]) -> GateRunResult:
    closed_node_ids = tuple(node_ids)
    audit_gate_nodes(repo_root, closed_node_ids)
    groups: dict[str, list[str]] = {}
    for node_id in closed_node_ids:
        groups.setdefault(node_id.partition("::")[0], []).append(node_id)
    passed_count = 0
    skipped_count = 0
    outputs: list[str] = []
    for group in groups.values():
        result = _run_gate_node_group(repo_root, tuple(group))
        passed_count += result.passed_count
        skipped_count += result.skipped_count
        outputs.append(result.stdout)
    return GateRunResult(
        node_ids=closed_node_ids,
        passed_count=passed_count,
        skipped_count=skipped_count,
        stdout="\n".join(outputs),
    )


def opencode_admission_state(repo_root: Path) -> OpenCodeAdmissionState:
    admission_path = repo_root / _ADMISSION_RELATIVE_PATH
    if not admission_path.is_file():
        return OpenCodeAdmissionState(
            source_status="missing",
            local_gate_disposition="run",
            release_disposition="blocked",
            detail=(
                "Task 3 OpenCode admission is missing. Deterministic local gates remain runnable, "
                "but release remains blocked."
            ),
        )
    admission = load_json(admission_path)
    source_status = admission.get("admission_status")
    if not isinstance(source_status, str) or source_status not in _ADMISSION_STATUSES:
        raise AssertionError(f"unknown Task 3 admission status: {source_status!r}")
    if source_status == "complete":
        return OpenCodeAdmissionState(
            source_status=source_status,
            local_gate_disposition="run",
            release_disposition="requires_task3_evidence_validation",
            detail=(
                "Task 3 declares completion; release still requires the dedicated authenticated "
                "evidence validator. This local gate does not claim live admission."
            ),
        )
    return OpenCodeAdmissionState(
        source_status=source_status,
        local_gate_disposition="run",
        release_disposition="blocked",
        detail=(
            f"Task 3 OpenCode admission is {source_status!r}. Deterministic local gates remain "
            "runnable, but release remains blocked."
        ),
    )


def load_yaml(path: Path) -> dict[str, object]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} must contain a mapping")
    return value


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} must contain a mapping")
    return value
