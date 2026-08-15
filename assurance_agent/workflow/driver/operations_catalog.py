"""Composition-root operation registry (Capability assembly).

Kernel ``OperationHandler`` only dispatches; this module is the sole place that
imports domain callables and binds them to ``operation:*`` targets. Callers
(``runtime_factory``, eval, tests) pass the catalog into
``build_default_node_runner``.
"""

from __future__ import annotations

from assurance_agent.workflow.execution.graph_ops import run_tests, run_tests_and_collect_pr_metrics
from assurance_agent.workflow.graph.handlers.operation import (
    OperationFn,
    no_op,
    stop_operation,
)
from assurance_agent.workflow.graph.handlers.plan_checks import (
    derive_plan_layer_applicability,
    verify_plan_mechanical,
)
from assurance_agent.workflow.graph.handlers.trace_projection import materialize_trace_projection
from assurance_agent.workflow.healing.graph_ops import (
    operation_allocate_healing_attempt,
    operation_record_healing_status,
    skill_registry_check,
)
from assurance_agent.workflow.healing.operations import (
    operation_combine_fixer_safety,
    operation_fixer_authority_ready,
    operation_fixer_dispatch,
    operation_record_codegen_fix_apply,
    operation_record_fixer_approval,
)
from assurance_agent.workflow.improvements.change_delivery import (
    export_change_improvement_operation,
    record_change_improvement_applied_operation,
)
from assurance_agent.workflow.improvements.knowledge_delivery import (
    export_knowledge_improvement_operation,
    record_knowledge_improvement_applied_operation,
)
from assurance_agent.workflow.improvements.memory_delivery import (
    apply_memory_improvement_operation,
    evaluate_memory_improvement_operation,
    load_improvement_delivery_operation,
    rollback_memory_improvement_operation,
)
from assurance_agent.workflow.improvements.review import (
    apply_improvement_review_operation,
    load_improvement_review_context_operation,
)
from assurance_agent.workflow.issues.operations import (
    apply_problem_review_operation,
    collect_observations_operation,
    load_problem_review_context_operation,
    record_empty_issue_analysis_operation,
    record_issue_analysis_failure_operation,
    record_project_sync_pending_operation,
    reconcile_issues_operation,
)
from assurance_agent.workflow.metrics.adversarial_yield import collect_adversarial_yield_operation
from assurance_agent.workflow.metrics.auth_matrix import compute_auth_matrix_operation
from assurance_agent.workflow.metrics.c_layer import materialize_c_layer_metrics_operation
from assurance_agent.workflow.metrics.constraint_coverage import (
    compute_constraint_coverage_operation,
)
from assurance_agent.workflow.metrics.coverage_gaps import build_coverage_gap_signals_operation
from assurance_agent.workflow.metrics.coverage_repair import (
    allocate_coverage_repair_attempt_operation,
    compute_coverage_repair_safety_operation,
    probe_coverage_repair_need_operation,
    record_coverage_repair_status_operation,
)
from assurance_agent.workflow.metrics.diff_coverage import collect_diff_coverage_operation
from assurance_agent.workflow.metrics.journey_coverage import (
    compute_journey_coverage_operation,
)
from assurance_agent.workflow.metrics.minimum_coverage import materialize_minimum_coverage_operation
from assurance_agent.workflow.metrics.nightly import (
    aggregate_nightly_metrics_operation,
    compute_assertion_strength_operation,
    compute_baseline_drift_operation,
    evaluate_retrospective_shortboards_operation,
    load_latest_pr_metrics_operation,
    run_mutation_sample_operation,
    run_nightly_metrics_pipeline_operation,
)
from assurance_agent.workflow.metrics.pr_metrics import (
    collect_pr_metrics_batch_operation,
    materialize_pr_metrics_operation,
)
from assurance_agent.workflow.metrics.quarantine import materialize_quarantine_projection_operation
from assurance_agent.workflow.metrics.threshold_slack import compute_threshold_slack_operation
from assurance_agent.workflow.report.graph_ops import (
    generate_report_operation,
    inspect_operation,
)
from assurance_agent.workflow.retro_ops import (
    apply_improvement_auto_review,
    assemble_retro_context_v3,
    drain_improvement_outbox,
    evidence_gap_fallback,
    finalize_retro_run_status,
    load_review_subject,
    materialize_empty_retro_analysis,
    record_analysis_failed,
    record_auto_review_orchestration_error,
    record_improvement_auto_review_error,
    record_retro_pipeline_failure,
    reconcile_improvements,
    retro_accept,
    retro_collect_v3,
    select_current_retro_auto_review_items,
    summarize_auto_review_batch,
    validate_improvement_review_assessment,
)


def default_operations() -> dict[str, OperationFn]:
    """Canonical operation registry: exact target → in-process callable."""
    return {
        "operation:no-op": no_op,
        "operation:skill-registry-check": skill_registry_check,
        "operation:verify-plan-mechanical": verify_plan_mechanical,
        "operation:derive-plan-layer-applicability": derive_plan_layer_applicability,
        "operation:run-tests": run_tests,
        "operation:run-tests-and-collect-pr-metrics": run_tests_and_collect_pr_metrics,
        "operation:inspect": inspect_operation,
        "operation:generate-report": generate_report_operation,
        "operation:allocate-healing-attempt": operation_allocate_healing_attempt,
        "operation:fixer-authority-ready": operation_fixer_authority_ready,
        "operation:record-fixer-approval": operation_record_fixer_approval,
        "operation:fixer-dispatch": operation_fixer_dispatch,
        "operation:record-codegen-fix-apply": operation_record_codegen_fix_apply,
        "operation:combine-fixer-safety": operation_combine_fixer_safety,
        "operation:record-healing-status": operation_record_healing_status,
        "operation:stop": stop_operation,
        "operation:retro-collect-v3": retro_collect_v3,
        "operation:assemble-retro-context-v3": assemble_retro_context_v3,
        "operation:drain-improvement-outbox": drain_improvement_outbox,
        "operation:finalize-retro-status": finalize_retro_run_status,
        "operation:record-retro-pipeline-failure": record_retro_pipeline_failure,
        "operation:retro-evidence-gap-fallback": evidence_gap_fallback,
        "operation:record-analysis-failed": record_analysis_failed,
        "operation:materialize-empty-retro-analysis": materialize_empty_retro_analysis,
        "operation:materialize-trace-projection": materialize_trace_projection,
        # Dual-source Lane B gap signals (graph wiring deferred; callable + contract).
        "operation:build-coverage-gap-signals": build_coverage_gap_signals_operation,
        "operation:materialize-minimum-coverage": materialize_minimum_coverage_operation,
        "operation:collect-diff-coverage": collect_diff_coverage_operation,
        "operation:compute-constraint-coverage": compute_constraint_coverage_operation,
        "operation:compute-auth-matrix": compute_auth_matrix_operation,
        "operation:compute-journey-coverage": compute_journey_coverage_operation,
        "operation:compute-threshold-slack": compute_threshold_slack_operation,
        "operation:materialize-quarantine-projection": materialize_quarantine_projection_operation,
        # Report-only C1/C2/C3 aggregate (M4); not on metrics-sufficiency path.
        "operation:materialize-c-layer-metrics": materialize_c_layer_metrics_operation,
        "operation:collect-pr-metrics-batch": collect_pr_metrics_batch_operation,
        "operation:probe-coverage-repair-need": probe_coverage_repair_need_operation,
        "operation:compute-coverage-repair-safety": compute_coverage_repair_safety_operation,
        "operation:allocate-coverage-repair-attempt": allocate_coverage_repair_attempt_operation,
        "operation:record-coverage-repair-status": record_coverage_repair_status_operation,
        "operation:materialize-pr-metrics": materialize_pr_metrics_operation,
        "operation:load-latest-pr-metrics": load_latest_pr_metrics_operation,
        "operation:run-mutation-sample": run_mutation_sample_operation,
        "operation:compute-assertion-strength": compute_assertion_strength_operation,
        "operation:compute-baseline-drift": compute_baseline_drift_operation,
        "operation:collect-adversarial-yield": collect_adversarial_yield_operation,
        "operation:aggregate-nightly-metrics": aggregate_nightly_metrics_operation,
        "operation:evaluate-retrospective-shortboards": evaluate_retrospective_shortboards_operation,
        "operation:run-nightly-metrics-pipeline": run_nightly_metrics_pipeline_operation,
        "operation:reconcile-improvements": reconcile_improvements,
        "operation:load-review-subject": load_review_subject,
        "operation:validate-improvement-review-assessment": validate_improvement_review_assessment,
        "operation:apply-improvement-auto-review": apply_improvement_auto_review,
        "operation:record-improvement-auto-review-error": record_improvement_auto_review_error,
        "operation:record-auto-review-orchestration-error": record_auto_review_orchestration_error,
        "operation:select-current-retro-auto-review-items": select_current_retro_auto_review_items,
        "operation:summarize-auto-review-batch": summarize_auto_review_batch,
        # Compatibility alias for older unit tests (not in execution contracts).
        "operation:retro-accept": retro_accept,
        "operation:collect-observations": collect_observations_operation,
        "operation:record-empty-issue-analysis": record_empty_issue_analysis_operation,
        "operation:record-issue-analysis-failure": record_issue_analysis_failure_operation,
        "operation:record-project-sync-pending": record_project_sync_pending_operation,
        "operation:reconcile-issues": reconcile_issues_operation,
        "operation:load-problem-review-context": load_problem_review_context_operation,
        "operation:apply-problem-review": apply_problem_review_operation,
        "operation:load-improvement-review-context": load_improvement_review_context_operation,
        "operation:apply-improvement-review": apply_improvement_review_operation,
        "operation:load-improvement-delivery": load_improvement_delivery_operation,
        "operation:evaluate-memory-improvement": evaluate_memory_improvement_operation,
        "operation:apply-memory-improvement": apply_memory_improvement_operation,
        "operation:rollback-memory-improvement": rollback_memory_improvement_operation,
        "operation:export-change-improvement": export_change_improvement_operation,
        "operation:record-change-improvement-applied": record_change_improvement_applied_operation,
        "operation:export-knowledge-improvement": export_knowledge_improvement_operation,
        "operation:record-knowledge-improvement-applied": record_knowledge_improvement_applied_operation,
    }


__all__ = ["default_operations"]
