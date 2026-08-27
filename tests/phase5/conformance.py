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
class Phase5FaultEvidence:
    evidence_kind: Literal["direct", "superseded", "gap"]
    node_id: str | None = None
    reason: str | None = None
    replacement_node_id: str | None = None


@dataclass(frozen=True, slots=True)
class Phase5FaultCoverageState:
    local_gate_disposition: Literal["run"]
    release_complete: bool
    direct_count: int
    superseded_count: int
    gap_count: int
    superseded_fault_ids: tuple[str, ...]
    gap_fault_ids: tuple[str, ...]
    detail: str


EVIDENCE_ROOT = Path(__file__).resolve().parents[2] / (
    ".superpowers/sdd/2026-08-22-pure-graph-engine-phase5-assurance-product-assembly"
)

PREPARE_IDS = (
    "assurance.intake.case-design.prepare",
    "assurance.intake.case-review.prepare",
    "assurance.intake.explore.prepare",
    "assurance.intake.intake.prepare",
    "assurance.generation.api.codegen-fix.prepare",
    "assurance.generation.api.codegen.prepare",
    "assurance.generation.api.plan-review.prepare",
    "assurance.generation.api.plan.prepare",
    "assurance.generation.e2e.codegen-fix.prepare",
    "assurance.generation.e2e.codegen.prepare",
    "assurance.generation.e2e.plan-review.prepare",
    "assurance.generation.e2e.plan.prepare",
    "assurance.generation.fuzz.codegen.prepare",
    "assurance.generation.fuzz.plan-review.prepare",
    "assurance.generation.fuzz.plan.prepare",
    "assurance.generation.performance.codegen.prepare",
    "assurance.generation.performance.plan-review.prepare",
    "assurance.generation.performance.plan.prepare",
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

ALL_BINDING_IDS = tuple(
    alias
    for prepare_id in PREPARE_IDS
    for alias in (
        "assurance.product.agent." + prepare_id.removeprefix("assurance.").removesuffix(".prepare") + phase
        for phase in (".prepare", ".execute", ".finalize")
    )
)

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

PHASE5_FAULT_IDS = (
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
    "cursor-confinement-unavailable",
    "cursor-executable-drift",
    "cursor-version-drift",
    "cursor-before-spawn",
    "cursor-after-spawn",
    "cursor-partial-ndjson",
    "cursor-output-overflow",
    "cursor-terminal-exit-mismatch",
    "cursor-unknown-process-ownership",
    "cursor-host-boot-change",
    "cursor-cancel-race",
    "cursor-descendant-cleanup-failure",
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
            "tests/phase5/test_composition_authority.py::test_forged_deployment_declaration_fails_closed",
            "tests/phase5/test_composition_authority.py::test_mutated_config_tree_changes_lock",
            "tests/phase5/test_product_input.py::test_product_input_authenticates_resource_refs_against_composition",
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
            "tests/phase5/test_binding_builder_security.py::test_built_wheel_contains_no_secret_bytes",
        ),
    }
)

_FAULT_GATE_SUPPORT_NODE_IDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "provider_state_loss_replay": (
            "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_receipt_before_engine_ack_replays_without_provider",
            "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_replay_without_provider",
            "packages/agent-runtime-cursor/tests/test_process_host.py::test_durable_terminal_survives_new_host_instance",
            "packages/graph-engine/tests/runtime/test_staged_promotion_recovery.py::test_recovery_consumes_durable_promotion_without_reexecuting_handler",
            "tests/phase5/test_replay_properties.py::test_publish_replay_matches_uninterrupted_projection_for_every_ordered_crash_subset",
        ),
        "fault_crash_recovery": (
            "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_create_cuts_never_issue_a_second_post[before_create]",
            "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_create_cuts_never_issue_a_second_post[after_create_before_response]",
            "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_create_cuts_never_issue_a_second_post[invalid_success_body]",
            "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_create_cuts_never_issue_a_second_post[proxy_reset]",
            "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_prompt_cuts_converge_to_one_admission[before_prompt_post]",
            "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_prompt_cuts_converge_to_one_admission[after_admission_before_response]",
            "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_prompt_cuts_converge_to_one_admission[after_lost_success_response]",
            "tests/phase5/test_publish_recovery.py::test_crash_after_journal_phase_then_resume[prepared]",
            "tests/phase5/test_publish_recovery.py::test_crash_after_journal_phase_then_resume[replacing]",
            "tests/phase5/test_publish_recovery.py::test_crash_after_journal_phase_then_resume[committed]",
            "packages/agent-runtime-cursor/tests/test_process_host_faults.py::test_wait_before_durable_write_is_not_visible",
        ),
        "stop_interrupt": (
            "tests/phase5/test_stop_and_interrupts.py::test_business_stop_is_resumable_only_at_declared_interrupt",
            "tests/phase5/test_stop_and_interrupts.py::test_invalid_resume_input_fails",
            "tests/phase5/test_stop_and_interrupts.py::test_healing_disallowed_is_business_stop_not_completion",
            "tests/phase5/test_stop_and_interrupts.py::test_nested_stop_does_not_become_normal_completion",
        ),
        "coverage_healing": (
            "tests/phase5/test_coverage_loop.py::test_low_coverage_reenters_generation_until_policy_passes",
            "tests/phase5/test_coverage_loop.py::test_coverage_repair_exhaustion_stops_with_a_report",
            "tests/phase5/test_issue_healing_flow.py::test_issue_path_runs_triage_analysis_and_fix_before_rerun",
            "tests/phase5/test_issue_healing_flow.py::test_product_issue_runs_the_same_healing_chain",
        ),
        "task26_fault_coverage": (
            "tests/phase5/test_binding_builder.py::test_unknown_prepare_assignment_is_rejected",
            "tests/phase5/test_composition_authority.py::test_unknown_product_entrypoint_is_rejected",
            "tests/phase5/test_binding_builder.py::test_missing_prepare_assignment_is_rejected",
            "tests/phase5/test_composition_authority.py::test_duplicate_binding_id_fails_closed",
            "tests/phase5/test_composition_authority.py::test_wrong_runtime_is_rejected_before_fallback",
            "tests/phase5/test_binding_builder_security.py::test_secret_handle_rejects_embedded_values",
            "tests/phase5/test_project_configuration_security.py::test_python_file_is_rejected",
            "tests/phase5/test_binding_builder.py::test_interrupted_publication_is_retryable[builder-before-file-publication]",
            "tests/phase5/test_binding_builder.py::test_interrupted_publication_is_retryable[builder-after-file-publication]",
            "tests/phase5/test_binding_builder.py::test_interrupted_publication_is_retryable[builder-before-wheel-publication]",
            "tests/phase5/test_binding_builder.py::test_interrupted_publication_is_retryable[builder-after-wheel-publication]",
            "tests/phase5/test_binding_builder.py::test_generated_provider_contributes_exactly_99_aliases",
            "tests/phase5/test_product_composition.py::test_wrong_runtime_deployment_fails_closed",
            "tests/phase5/test_binding_builder_security.py::test_builder_rejects_capability_source_drift",
            "tests/phase5/test_project_configuration_security.py::test_bindings_are_rejected_even_when_phase2_would_accept_them",
            "tests/phase5/test_project_configuration_security.py::test_runtime_adapter_dependency_is_rejected",
            "tests/phase5/test_binding_builder.py::test_extra_prepare_assignment_is_rejected",
            "packages/graph-engine/tests/runtime/test_engine.py::test_initialization_failure_before_ledger_leaves_exactly_recoverable_identity",
            "packages/graph-engine/tests/runtime/test_invocation_workspace_binding.py::test_start_requires_binding_and_does_not_capture_the_project_tree",
            "packages/graph-engine/tests/runtime/test_invocation_lock.py::test_engine_persists_lock_before_bootstrap",
            "packages/graph-engine/tests/runtime/test_invocation_lock.py::test_start_intent_fault_cuts_are_exactly_recoverable[after_rename-True]",
            "packages/graph-engine/tests/runtime/test_bootstrap_v2.py::test_start_recovers_to_one_authenticated_bootstrap_prefix[0]",
            "packages/graph-engine/tests/runtime/test_bootstrap_v2.py::test_start_recovers_to_one_authenticated_bootstrap_prefix[2]",
            "packages/graph-engine/tests/runtime/test_bootstrap_v2.py::test_start_recovers_to_one_authenticated_bootstrap_prefix[3]",
            "packages/graph-engine/tests/runtime/test_engine.py::test_duplicate_start_rejects_digest_mismatch_without_replacing_invocation",
            "packages/graph-engine/tests/runtime/test_engine.py::test_open_rejects_forged_root_input_even_with_matching_start_token",
            "packages/graph-engine/tests/runtime/test_engine.py::test_open_rejects_terminal_history_with_omitted_fanout_branch",
            "packages/graph-engine/tests/runtime/test_engine.py::test_open_rejects_extra_duplicate_edge_token",
            "packages/graph-engine/tests/runtime/test_engine.py::test_open_rejects_non_earliest_token_for_all_join_predecessor",
            "packages/graph-engine/tests/runtime/test_engine.py::test_open_rejects_changed_canonical_edge_token[token_id]",
            "packages/graph-engine/tests/runtime/test_ledger_and_checkpoint.py::test_checkpoint_rejects_impossible_or_mismatched_projection",
            "packages/graph-engine/tests/runtime/test_production_host_faults.py::test_host_before_worker_spawn_fault_has_no_child_dispatch_or_receipt",
            "packages/graph-engine/tests/runtime/test_production_host_faults.py::test_host_after_spawn_before_dispatch_fault_cleans_child_without_receipt",
            "packages/graph-engine/tests/runtime/test_production_host_faults.py::test_host_during_activity_rpc_fault_cleans_child_and_reconciles_safely",
            "packages/graph-engine/tests/runtime/test_host_receipts.py::test_recovery_checks_receipt_before_reconcile",
            "packages/graph-engine/tests/runtime/test_host_receipts.py::test_quiescence_rejects_live_writers",
            "packages/graph-engine/tests/runtime/test_host_receipts.py::test_terminal_receipt_cut_recovery_is_exactly_once[after_receipt_rename]",
            "packages/graph-engine/tests/runtime/test_production_host_faults.py::test_production_host_crash_after_receipt_leaves_durable_receipt",
            "packages/graph-engine/tests/runtime/test_production_host_faults.py::test_leftover_process_group_child_still_fails_quiescence",
            "packages/graph-engine/tests/runtime/test_production_host_security.py::test_production_host_revokes_parent_secrets_after_call",
            "packages/graph-engine/tests/runtime/test_production_host_faults.py::test_worker_rejects_substituted_project_root_before_handler_execution",
            "packages/graph-engine/tests/runtime/test_host_receipts.py::test_receipt_store_rejects_partial_symlink_linked_changed_multiple_foreign_and_nonmonotonic",
            "packages/graph-engine/tests/runtime/test_production_host_faults.py::test_production_host_rejects_forged_terminal_receipt",
            "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_temporary_empty_discovery_stays_indeterminate",
            "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_multiple_matches_fail_closed",
            "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_lost_sse_authenticates_with_get",
            "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_idle_with_open_tools_is_not_terminal",
            "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_completion_cancel_race_provider_terminal_wins",
            "packages/agent-runtime-cursor/tests/test_fault_matrix.py::test_confinement_unavailable_after_bind_is_indeterminate",
            "packages/agent-runtime-cursor/tests/test_process_launch.py::test_launch_rejects_digest_mismatch_before_spawn",
            "packages/agent-runtime-cursor/tests/test_fault_matrix.py::test_init_version_mismatch_is_indeterminate",
            "packages/agent-runtime-cursor/tests/test_fault_matrix.py::test_spawn_failure_proven_to_create_no_child",
            "packages/agent-runtime-cursor/tests/test_recovery.py::test_cursor_recovery_never_blindly_retries[after_spawn_before_bind-indeterminate]",
            "packages/agent-runtime-cursor/tests/test_stream_parser.py::test_stream_parser_enforces_remaining_bounds[truncated_line-truncated]",
            "packages/agent-runtime-cursor/tests/test_stream_parser.py::test_stream_parser_fails_closed[oversized_line-line byte limit]",
            "packages/agent-runtime-cursor/tests/test_stream_parser.py::test_stream_parser_fails_closed[exit_mismatch-exit status]",
            "packages/agent-runtime-cursor/tests/test_fault_matrix.py::test_unknown_exit_is_indeterminate_never_absent",
            "packages/agent-runtime-cursor/tests/test_fault_matrix.py::test_host_boot_change_is_indeterminate_never_absent",
            "packages/agent-runtime-cursor/tests/test_fault_matrix.py::test_completion_cancel_race_provider_terminal_wins",
            "packages/agent-runtime-cursor/tests/test_fault_matrix.py::test_cleanup_ambiguity_is_indeterminate",
            "tests/phase4/test_effect_fault_matrix.py::test_effect_crash_cuts_are_at_most_once_and_typed[before_mutation-assurance.improvement.effect.archive.v1]",
            "tests/phase4/test_effect_fault_matrix.py::test_effect_crash_cuts_are_at_most_once_and_typed[after_mutation-assurance.improvement.effect.archive.v1]",
            "tests/phase4/test_effect_fault_matrix.py::test_effect_crash_cuts_are_at_most_once_and_typed[before_receipt-assurance.improvement.effect.archive.v1]",
            "tests/phase4/test_effect_fault_matrix.py::test_effect_crash_cuts_are_at_most_once_and_typed[reconcile_error-assurance.improvement.effect.archive.v1]",
            "tests/phase5/test_publish_recovery.py::test_failed_replace_rolls_back",
            "tests/phase5/test_publish_recovery.py::test_export_directory_fsync_failure_rolls_back_and_retry_matches_uninterrupted",
            "tests/phase5/test_result_export.py::test_publish_rejects_target_baseline_drift",
            "tests/phase5/test_export_security.py::test_publish_rejects_symlink_target",
            "tests/phase5/test_export_security.py::test_publish_rejects_hardlink_target",
            "tests/phase5/test_comparison_matrix.py::test_committed_export_digests_are_authenticated",
            "tests/phase5/test_comparison_matrix.py::test_all_twenty_five_cases_are_governed_passes",
            "tests/phase5/test_comparison_matrix.py::test_report_case_keeps_required_outputs_in_the_result_tree",
        ),
    }
)

_PHASE5_DIRECT_FAULT_NODE_IDS: Mapping[str, str] = MappingProxyType(
    {
        "deployment-route-invalid": (
            "tests/phase5/test_binding_builder_security.py::test_manifest_rejects_templates_globs_and_secret_values[{{model}}]"
        ),
        "deployment-route-unknown": (
            "tests/phase5/test_binding_builder.py::test_unknown_prepare_assignment_is_rejected"
        ),
        "deployment-route-missing": (
            "tests/phase5/test_binding_builder.py::test_missing_prepare_assignment_is_rejected"
        ),
        "deployment-route-duplicate": (
            "tests/phase5/test_binding_builder.py::test_duplicate_route_assignment_is_rejected_before_build"
        ),
        "deployment-route-fallback": (
            "tests/phase5/test_binding_builder_security.py::test_manifest_rejects_fallback_lists_in_provider_model"
        ),
        "deployment-raw-secret": (
            "tests/phase5/test_binding_builder_security.py::test_secret_handle_rejects_embedded_values"
        ),
        "deployment-arbitrary-code": (
            "tests/phase5/test_binding_builder_security.py::test_manifest_rejects_executable_or_fallback_fields[python]"
        ),
        "builder-before-file-publication": (
            "tests/phase5/test_binding_builder.py::test_interrupted_publication_is_retryable[builder-before-file-publication]"
        ),
        "builder-after-file-publication": (
            "tests/phase5/test_binding_builder.py::test_interrupted_publication_is_retryable[builder-after-file-publication]"
        ),
        "builder-before-wheel-publication": (
            "tests/phase5/test_binding_builder.py::test_interrupted_publication_is_retryable[builder-before-wheel-publication]"
        ),
        "builder-after-wheel-publication": (
            "tests/phase5/test_binding_builder.py::test_interrupted_publication_is_retryable[builder-after-wheel-publication]"
        ),
        "generated-declaration-contribution-mismatch": (
            "tests/phase5/test_binding_builder.py::test_generated_provider_contributes_exactly_99_aliases"
        ),
        "wrong-adapter-dependency": (
            "tests/phase5/test_product_composition.py::test_wrong_runtime_deployment_fails_closed"
        ),
        "unselected-adapter-source": (
            "tests/phase5/test_product_composition.py::test_unselected_adapter_source_is_rejected_before_provider_import"
        ),
        "deployment-wheel-drift": (
            "tests/phase5/test_composition_authority.py::test_deployment_wheel_drift_after_resolution_fails_before_provider_import"
        ),
        "binding-through-project-config": (
            "tests/phase5/test_project_configuration_security.py::test_bindings_are_rejected_even_when_phase2_would_accept_them"
        ),
        "project-config-executable-shape": (
            "tests/phase5/test_project_configuration_security.py::test_project_config_rejects_ports_entry_points_and_handlers[python]"
        ),
        "project-config-runtime-authority": (
            "tests/phase5/test_project_configuration_security.py::test_project_config_rejects_runtime_authority[adapter]"
        ),
        "alias-missing": ("tests/phase5/test_composition_authority.py::test_missing_binding_fails_closed"),
        "alias-extra": ("tests/phase5/test_composition_authority.py::test_extra_owned_binding_fails_closed"),
        "alias-forged": ("tests/phase5/test_composition_authority.py::test_forged_alias_target_fails_closed"),
        "start-intent-publication-cut": (
            "packages/graph-engine/tests/runtime/test_invocation_lock.py::test_start_intent_fault_cuts_are_exactly_recoverable[after_rename-True]"
        ),
        "bootstrap-before-append": (
            "packages/graph-engine/tests/runtime/test_bootstrap_v2.py::test_start_recovers_to_one_authenticated_bootstrap_prefix[0]"
        ),
        "bootstrap-after-append": (
            "packages/graph-engine/tests/runtime/test_bootstrap_v2.py::test_start_recovers_to_one_authenticated_bootstrap_prefix[3]"
        ),
        "bootstrap-before-directory-fsync": (
            "packages/graph-engine/tests/runtime/test_bootstrap_v2.py::test_start_recovers_to_one_authenticated_bootstrap_prefix[2]"
        ),
        "repeated-start-root-input-drift": (
            "packages/graph-engine/tests/runtime/test_engine.py::test_repeated_start_with_changed_root_input_rejects_drift_and_preserves_state"
        ),
        "projection-missing-root-pointer": (
            "packages/graph-engine/tests/graph/test_input_projection.py::test_projection_rejects_missing_pointer"
        ),
        "projection-missing-predecessor": (
            "packages/graph-engine/tests/graph/test_input_projection.py::test_projection_rejects_missing_predecessor_token"
        ),
        "projection-duplicate-predecessor-token": (
            "packages/graph-engine/tests/runtime/test_planner.py::test_all_join_waits_for_each_distinct_predecessor"
        ),
        "projection-wrong-join-cardinality": (
            "packages/graph-engine/tests/graph/test_schema_and_compiler.py::test_compile_requires_two_distinct_sources_for_all_join"
        ),
        "projection-token-schema-mismatch": (
            "packages/graph-engine/tests/runtime/test_engine.py::test_open_rejects_changed_canonical_edge_token[payload]"
        ),
        "projection-noncanonical-pointer": (
            "packages/graph-engine/tests/graph/test_input_projection.py::test_projection_rejects_non_canonical_pointer"
        ),
        "projection-replay-drift": (
            "packages/graph-engine/tests/runtime/test_ledger_and_checkpoint.py::test_checkpoint_rejects_impossible_or_mismatched_projection"
        ),
        "host-before-worker-spawn": (
            "packages/graph-engine/tests/runtime/test_production_host_faults.py::test_host_before_worker_spawn_fault_has_no_child_dispatch_or_receipt"
        ),
        "host-after-spawn-before-dispatch": (
            "packages/graph-engine/tests/runtime/test_production_host_faults.py::test_host_after_spawn_before_dispatch_fault_cleans_child_without_receipt"
        ),
        "host-during-activity-rpc": (
            "packages/graph-engine/tests/runtime/test_production_host_faults.py::test_host_during_activity_rpc_fault_cleans_child_and_reconciles_safely"
        ),
        "host-after-reference-bind": (
            "packages/graph-engine/tests/runtime/test_production_host_faults.py::test_host_after_reference_bind_fault_preserves_bound_activity_for_reconcile"
        ),
        "host-response-before-quiescence": (
            "packages/graph-engine/tests/runtime/test_host_receipts.py::test_terminal_receipt_cut_recovery_is_exactly_once[before_quiescence]"
        ),
        "host-terminal-receipt-publication": (
            "packages/graph-engine/tests/runtime/test_host_receipts.py::test_terminal_receipt_cut_recovery_is_exactly_once[after_receipt_rename]"
        ),
        "host-receipt-durable-before-ack": (
            "packages/graph-engine/tests/runtime/test_host_receipts.py::test_terminal_receipt_cut_recovery_is_exactly_once[after_staged_seal]"
        ),
        "host-parent-crash-live-descendants": (
            "packages/graph-engine/tests/runtime/test_production_host_faults.py::test_parent_process_crash_with_live_descendants_cleans_group_and_leaves_no_receipt"
        ),
        "host-secret-channel-disconnect": (
            "packages/graph-engine/tests/runtime/test_production_host_security.py::test_secret_channel_disconnect_revokes_parent_material_and_cleans_worker"
        ),
        "host-secret-channel-revocation": (
            "packages/graph-engine/tests/runtime/test_production_host_security.py::test_production_host_revokes_parent_secrets_after_call"
        ),
        "host-worker-source-drift": (
            "packages/graph-engine/tests/runtime/test_production_host_faults.py::test_host_rejects_worker_source_drift_before_spawn"
        ),
        "host-duplicate-terminal-receipt": (
            "packages/graph-engine/tests/runtime/test_host_receipts.py::test_receipt_store_rejects_partial_symlink_linked_changed_multiple_foreign_and_nonmonotonic"
        ),
        "host-foreign-terminal-receipt": (
            "packages/graph-engine/tests/runtime/test_production_host_faults.py::test_production_host_rejects_forged_terminal_receipt"
        ),
        "opencode-ambiguous-session-create": (
            "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_create_cuts_never_issue_a_second_post[after_create_before_response]"
        ),
        "opencode-empty-ambiguous-discovery": (
            "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_temporary_empty_discovery_stays_indeterminate"
        ),
        "opencode-duplicate-metadata": (
            "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_multiple_matches_fail_closed"
        ),
        "opencode-prompt-admission-lost-response": (
            "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_prompt_cuts_converge_to_one_admission[after_lost_success_response]"
        ),
        "opencode-sse-disconnect": (
            "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_lost_sse_authenticates_with_get"
        ),
        "opencode-transient-idle": (
            "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_idle_with_open_tools_is_not_terminal"
        ),
        "opencode-cancel-result-race": (
            "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_completion_cancel_race_provider_terminal_wins"
        ),
        "cursor-confinement-unavailable": (
            "packages/agent-runtime-cursor/tests/test_fault_matrix.py::test_confinement_unavailable_after_bind_is_indeterminate"
        ),
        "cursor-executable-drift": (
            "packages/agent-runtime-cursor/tests/test_process_launch.py::test_launch_rejects_digest_mismatch_before_spawn"
        ),
        "cursor-version-drift": (
            "packages/agent-runtime-cursor/tests/test_fault_matrix.py::test_init_version_mismatch_is_indeterminate"
        ),
        "cursor-before-spawn": (
            "packages/agent-runtime-cursor/tests/test_fault_matrix.py::test_spawn_failure_proven_to_create_no_child"
        ),
        "cursor-after-spawn": (
            "packages/agent-runtime-cursor/tests/test_recovery.py::test_cursor_recovery_never_blindly_retries[after_spawn_before_bind-indeterminate]"
        ),
        "cursor-partial-ndjson": (
            "packages/agent-runtime-cursor/tests/test_stream_parser.py::test_stream_parser_enforces_remaining_bounds[truncated_line-truncated]"
        ),
        "cursor-output-overflow": (
            "packages/agent-runtime-cursor/tests/test_stream_parser.py::test_stream_parser_fails_closed[oversized_line-line byte limit]"
        ),
        "cursor-terminal-exit-mismatch": (
            "packages/agent-runtime-cursor/tests/test_stream_parser.py::test_stream_parser_fails_closed[exit_mismatch-exit status]"
        ),
        "cursor-unknown-process-ownership": (
            "packages/agent-runtime-cursor/tests/test_fault_matrix.py::test_unknown_exit_is_indeterminate_never_absent"
        ),
        "cursor-host-boot-change": (
            "packages/agent-runtime-cursor/tests/test_fault_matrix.py::test_host_boot_change_is_indeterminate_never_absent"
        ),
        "cursor-cancel-race": (
            "packages/agent-runtime-cursor/tests/test_fault_matrix.py::test_completion_cancel_race_provider_terminal_wins"
        ),
        "cursor-descendant-cleanup-failure": (
            "packages/agent-runtime-cursor/tests/test_fault_matrix.py::test_cleanup_ambiguity_is_indeterminate"
        ),
        "effect-before-intent": (
            "packages/graph-engine/tests/runtime/test_scheduler.py::test_prepared_intent_cas_conflict_leaves_project_unchanged"
        ),
        "effect-after-intent": (
            "packages/graph-engine/tests/runtime/test_scheduler.py::test_prepared_append_installed_completes[final_installed]"
        ),
        "effect-receipt-publication": (
            "packages/graph-engine/tests/runtime/test_engine.py::test_engine_effect_receipt_publication_crash_replays_without_reapply"
        ),
        "effect-reconcile-lost-ack": (
            "tests/phase4/test_effect_fault_matrix.py::test_effect_crash_cuts_are_at_most_once_and_typed[after_receipt-assurance.improvement.effect.archive.v1]"
        ),
        "export-file-write": (
            "tests/phase5/test_publish_recovery.py::test_export_file_write_or_file_fsync_failure_rolls_back_and_retries[write]"
        ),
        "export-rename": ("tests/phase5/test_publish_recovery.py::test_failed_replace_rolls_back"),
        "export-directory-fsync": (
            "tests/phase5/test_publish_recovery.py::test_export_directory_fsync_failure_rolls_back_and_retry_matches_uninterrupted"
        ),
        "export-destination-race": (
            "tests/phase5/test_export_security.py::test_destination_swap_after_authentication_before_replace_fails_closed_without_clobber[symlink]"
        ),
        "export-destination-symlink": (
            "tests/phase5/test_export_security.py::test_publish_rejects_symlink_target"
        ),
        "export-destination-hardlink": (
            "tests/phase5/test_export_security.py::test_publish_rejects_hardlink_target"
        ),
        "comparison-input-drift": (
            "tests/phase5/test_comparison_matrix.py::test_run_comparison_rejects_export_drift_before_comparison[export-bytes]"
        ),
        "comparison-one-side-running": (
            "tests/phase5/test_comparison_matrix.py::test_comparison_rejects_terminal_input_while_other_side_is_running"
        ),
        "comparison-partial-report": (
            "tests/phase5/test_comparison_matrix.py::test_partial_comparison_report_publication_is_atomic_and_retryable"
        ),
    }
)

PHASE5_FAULT_NODE_IDS: Mapping[str, str] = _PHASE5_DIRECT_FAULT_NODE_IDS

_PHASE5_SUPERSEDED_FAULTS: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "seed-capture-cut": (
            "Change-local InvocationSeed has no project-tree capture or WorkspaceSeed authority.",
            "packages/graph-engine/tests/runtime/test_invocation_workspace_binding.py::test_invocation_seed_contains_root_input_but_no_project_tree_identity",
        ),
        "seed-captured-before-start": (
            "Engine.start requires a process-local descriptor-bound workspace binding and never captures a seed tree.",
            "packages/graph-engine/tests/runtime/test_invocation_workspace_binding.py::test_start_requires_binding_and_does_not_capture_the_project_tree",
        ),
        "initial-tree-publication-cut": (
            "SnapshotStore initial-tree publication was removed; each attempt now starts with an empty isolated write root.",
            "packages/graph-engine/tests/runtime/test_task_workspace.py::test_begin_creates_empty_attempt_root_with_stable_path_free_identity",
        ),
        "initial-tree-durable-before-intent": (
            "There is no initial tree durability boundary; the authenticated invocation lock precedes bootstrap.",
            "packages/graph-engine/tests/runtime/test_invocation_lock.py::test_engine_persists_lock_before_bootstrap",
        ),
        "repeated-start-seed-drift": (
            "WorkspaceSeed/tree identity was removed; repeated start now authenticates the process-local workspace binding.",
            "packages/graph-engine/tests/runtime/test_engine.py::test_repeated_start_with_changed_workspace_binding_rejects_drift_and_preserves_state",
        ),
    }
)

_PHASE5_GAP_REASONS: Mapping[str, str] = MappingProxyType(
    {
        "opencode-terminal-before-restart": (
            "The existing adapter test caches TaskOutcome in memory and never restarts a real Engine invocation."
        ),
        "opencode-provider-state-deleted-after-receipt": (
            "The existing replay fixture returns an in-memory TaskOutcome and does not delete provider state after a durable host receipt."
        ),
    }
)


def _phase5_fault_evidence(fault_id: str) -> Phase5FaultEvidence:
    direct_node_id = _PHASE5_DIRECT_FAULT_NODE_IDS.get(fault_id)
    if direct_node_id is not None:
        return Phase5FaultEvidence(evidence_kind="direct", node_id=direct_node_id)
    superseded = _PHASE5_SUPERSEDED_FAULTS.get(fault_id)
    if superseded is not None:
        reason, replacement_node_id = superseded
        return Phase5FaultEvidence(
            evidence_kind="superseded",
            reason=reason,
            replacement_node_id=replacement_node_id,
        )
    gap_reason = _PHASE5_GAP_REASONS.get(fault_id)
    if gap_reason is None:
        raise AssertionError(f"fault row has no evidence disposition: {fault_id}")
    return Phase5FaultEvidence(evidence_kind="gap", reason=gap_reason)


PHASE5_FAULT_EVIDENCE: Mapping[str, Phase5FaultEvidence] = MappingProxyType(
    {fault_id: _phase5_fault_evidence(fault_id) for fault_id in PHASE5_FAULT_IDS}
)

_MISLEADING_PROVIDER_REPLAY_CHARACTERIZATIONS = frozenset(
    {
        "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_receipt_before_engine_ack_replays_without_provider",
        "packages/agent-runtime-opencode/tests/test_fault_matrix.py::test_replay_without_provider",
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
    for evidence in PHASE5_FAULT_EVIDENCE.values()
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
            "tests/phase5/test_full_graph_audit.py::test_full_graph_has_no_orphans_or_forbidden_targets",
            "tests/phase5/test_full_graph_audit.py::test_full_graph_has_no_archive_branch_and_keeps_retro_improvement",
        ),
        "wheel_repository_isolation": (
            "packages/agent-runtime-contracts/tests/test_models.py::test_isolated_wheel_import_does_not_load_adapters_or_assurance",
            "tests/phase4/test_six_wheel_composition.py::test_fixture_product_is_absent_from_workspace_dependencies_archives_and_entrypoints",
            "tests/phase5/test_comparison_isolation.py::test_comparison_modules_do_not_import_runtime_packages",
            "tests/phase5/test_product_packaging.py::test_wheels_omit_whole_tree_modules_and_result_export_schema",
            "tests/phase5/test_wheel_smoke_contract.py::test_wheel_smoke_covers_isolated_selection_and_binding_fault_matrix",
            "tests/phase5/test_product_providers.py::test_source_catalogs_are_six_wheels_plus_selected_adapter",
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
_PHASE5_ADMISSION_RELATIVE_PATH = Path(
    ".superpowers/sdd/2026-08-26-pure-graph-engine-change-local-final-closeout/phase5-opencode-admission.json"
)
_PHASE5_ADMISSION_STATUSES = frozenset({"blocked_by_execution_approval", "complete"})


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
class Phase5OpenCodeAdmissionState:
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


def phase5_fault_coverage_state() -> Phase5FaultCoverageState:
    superseded_fault_ids = tuple(
        fault_id
        for fault_id, evidence in PHASE5_FAULT_EVIDENCE.items()
        if evidence.evidence_kind == "superseded"
    )
    gap_fault_ids = tuple(
        fault_id for fault_id, evidence in PHASE5_FAULT_EVIDENCE.items() if evidence.evidence_kind == "gap"
    )
    direct_count = len(PHASE5_FAULT_EVIDENCE) - len(superseded_fault_ids) - len(gap_fault_ids)
    return Phase5FaultCoverageState(
        local_gate_disposition="run",
        release_complete=not gap_fault_ids,
        direct_count=direct_count,
        superseded_count=len(superseded_fault_ids),
        gap_count=len(gap_fault_ids),
        superseded_fault_ids=superseded_fault_ids,
        gap_fault_ids=gap_fault_ids,
        detail=(
            "Phase 5 Task 26 release evidence is complete: every applicable fault row has direct "
            f"executable evidence and {len(superseded_fault_ids)} retired rows have replacement evidence."
            if not gap_fault_ids
            else "Phase 5 Task 26 release remains blocked by "
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
    with tempfile.TemporaryDirectory(prefix="phase5-final-gate-") as temporary_directory:
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


def phase5_opencode_admission_state(repo_root: Path) -> Phase5OpenCodeAdmissionState:
    admission_path = repo_root / _PHASE5_ADMISSION_RELATIVE_PATH
    if not admission_path.is_file():
        return Phase5OpenCodeAdmissionState(
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
    if not isinstance(source_status, str) or source_status not in _PHASE5_ADMISSION_STATUSES:
        raise AssertionError(f"unknown Task 3 admission status: {source_status!r}")
    if source_status == "complete":
        return Phase5OpenCodeAdmissionState(
            source_status=source_status,
            local_gate_disposition="run",
            release_disposition="requires_task3_evidence_validation",
            detail=(
                "Task 3 declares completion; release still requires the dedicated authenticated "
                "evidence validator. This local gate does not claim live admission."
            ),
        )
    return Phase5OpenCodeAdmissionState(
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
