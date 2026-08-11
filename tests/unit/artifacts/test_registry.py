from collections import Counter
from typing import get_args

from assurance_agent.artifacts.models.coverage_repair import (
    COVERAGE_REPAIR_APPLY_SUMMARY_REL,
    COVERAGE_REPAIR_BASELINE_REL,
    COVERAGE_REPAIR_BRIEF_REL,
    COVERAGE_REPAIR_SAFETY_REL,
    COVERAGE_REPAIR_STATUS_REL,
    CoverageRepairApplySummary,
    CoverageRepairBaseline,
    CoverageRepairBrief,
    CoverageRepairSafetyCheck,
    CoverageRepairStatus,
)
from assurance_agent.artifacts.models.metrics import MetricsDocument
from assurance_agent.artifacts.models import (
    CaseYaml,
    CaseYamlAuthoring,
    ImprovementAutoReviewAssessment,
    ImprovementAutoReviewAssessmentAuthoring,
)
from assurance_agent.artifacts.registry import REGISTRY, match_artifact


def test_registry_covers_every_expected_artifact_type() -> None:
    expected = {
        "advisory",
        "apply_summary",
        "case_yaml",
        "change_issue_snapshot",
        "codegen_generated_files",
        "coverage_repair_apply_summary",
        "coverage_repair_baseline",
        "coverage_repair_brief",
        "coverage_repair_safety_check",
        "coverage_repair_status",
        "data_knowledge_proposal",
        "discovery_campaign_result",
        "discovery_campaign_spec",
        "discovery_counterexample",
        "discovery_generated_manifest",
        "discovery_oracle_set",
        "discovery_promotion_manifest",
        "discovery_promotion_receipt",
        "discovery_regression_candidate",
        "discovery_replay_attempt_receipt",
        "discovery_round_decision",
        "trace_projection",
        "quarantine_projection",
        "trace_sufficiency_facts",
        "coverage_gaps",
        "c_layer_metrics",
        "declaration_proposal",
        "execution_manifest",
        "eval_run_projection_v1",
        "fact_baseline",
        "failure_analysis",
        "fix_proposal",
        "issue_analysis_status",
        "issue_candidate_document",
        "issue_evidence_manifest",
        "issue_reconcile_status",
        "metrics_document",
        "metrics_nightly_document",
        "minimum_coverage_matrix",
        "minimum_coverage_result",
        "improvement_candidate_document",
        "improvement_auto_review_assessment_v1",
        "improvement_auto_review_batch_summary_v1",
        "improvement_auto_review_status_v1",
        "improvement_review_subject_v1",
        "improvement_reconcile_outbox_v1",
        "observation_document",
        "plan_check",
        "qa_yaml",
        "quality_gate_result",
        "quality_report",
        "retro_window_v3",
        "retro_context_v3",
        "retro_issue_evidence_slice_v3",
        "retro_workflow_evidence_slice_v3",
        "retro_eval_evidence_slice_v3",
        "retro_issue_signal_v3",
        "retro_pipeline_failure_v1",
        "retro_run_status_v1",
        "retro_workflow_signal_v3",
        "retro_eval_signal_v3",
        "review",
        "safety_check",
        "workflow_state",
    }
    counts = Counter(spec.artifact_type for spec in REGISTRY)
    assert set(counts) == expected
    # Review and the four exact codegen-layer paths intentionally share models.
    assert counts["review"] == 4
    assert counts["codegen_generated_files"] == 4
    for artifact_type, count in counts.items():
        if artifact_type in {"review", "codegen_generated_files"}:
            continue
        assert count == 1, f"{artifact_type} registered {count} times, expected exactly 1"


def test_retro_closure_artifacts_match_only_their_run_paths() -> None:
    failure = match_artifact("qa/retro/RETRO-1/pipeline-failure.json")
    status = match_artifact("qa/retro/RETRO-1/retro-status.json")
    assert failure is not None and failure.artifact_type == "retro_pipeline_failure_v1"
    assert status is not None and status.artifact_type == "retro_run_status_v1"
    assert match_artifact("qa/retro/RETRO-1/nested/retro-status.json") is None


def test_case_yaml_matches_nested_and_direct_paths() -> None:
    for rel in ("cases/menus/case.yaml", "cases/a/b/case.yaml", "cases/case.yaml"):
        spec = match_artifact(rel)
        assert spec is not None and spec.artifact_type == "case_yaml", rel
    assert match_artifact("cases/menus/notes.yaml") is None


def test_case_yaml_uses_strict_authoring_contract_without_breaking_read_compatibility() -> None:
    spec = match_artifact("cases/menus/case.yaml")
    assert spec is not None
    assert spec.model is CaseYaml
    assert spec.authoring_model is CaseYamlAuthoring


def test_qa_yaml_exact_match_only() -> None:
    spec = match_artifact(".qa.yaml")
    assert spec is not None and spec.artifact_type == "qa_yaml"
    assert match_artifact("sub/.qa.yaml") is None


def test_codegen_generated_files_use_runtime_completed_contract() -> None:
    for layer in ("api", "e2e", "fuzz", "performance"):
        spec = match_artifact(f"codegen/{layer}-generated-files.json")
        assert spec is not None
        assert spec.artifact_type == "codegen_generated_files"
        assert spec.compat == "must_compat"
        assert spec.authoring_model is not None
        assert spec.runtime_completes_authoring is True

        authoring_entry = get_args(spec.authoring_model.model_fields["files"].annotation)[0]
        runtime_entry = get_args(spec.model.model_fields["files"].annotation)[0]
        assert "content_sha256" not in authoring_entry.model_fields
        assert runtime_entry.model_fields["content_sha256"].is_required()

    assert match_artifact("codegen/unknown-generated-files.json") is None


def test_improvement_review_assessment_uses_runtime_completed_identity_contract() -> None:
    spec = match_artifact("qa/improvements/reviews/REV-1/assessment.json")
    assert spec is not None
    assert spec.model is ImprovementAutoReviewAssessment
    assert spec.authoring_model is ImprovementAutoReviewAssessmentAuthoring
    assert spec.runtime_completes_authoring is True
    assert {
        "review_id",
        "improvement_id",
        "expected_improvement_version",
        "subject_sha256",
    }.isdisjoint(spec.authoring_model.model_fields)


def test_review_glob_matches_any_review_json_in_review_dir() -> None:
    spec = match_artifact("review/api-plan-review.json")
    assert spec is not None and spec.artifact_type == "review"
    assert match_artifact("review/case-review-apply-summary.md") is None


def test_star_does_not_cross_directory_boundaries() -> None:
    assert match_artifact("review/nested/deep-review.json") is None


def test_apply_summary_wildcard_and_fixed_healing_paths() -> None:
    api = match_artifact("healing/api-apply-summary.json")
    assert api is not None and api.artifact_type == "apply_summary"
    fp = match_artifact("healing/fix-proposal.json")
    assert fp is not None and fp.artifact_type == "fix_proposal"
    sc = match_artifact("healing/fixer-safety-check.json")
    assert sc is not None and sc.artifact_type == "safety_check"


def test_unregistered_path_returns_none() -> None:
    assert match_artifact("proposal.md") is None
    assert match_artifact("explore/context.json") is None


def test_backslash_paths_normalized() -> None:
    spec = match_artifact("inspect\\failure-analysis.json")
    assert spec is not None and spec.artifact_type == "failure_analysis"


def test_compat_grades_match_spec_4a() -> None:
    grades = {spec.artifact_type: spec.compat for spec in REGISTRY}
    assert grades["review"] == "must_compat"
    assert grades["failure_analysis"] == "must_compat"
    assert grades["fix_proposal"] == "must_compat"
    assert grades["safety_check"] == "must_compat"
    assert grades["workflow_state"] == "versioned"
    assert grades["execution_manifest"] == "versioned"
    assert grades["quality_report"] == "versioned"
    assert grades["observation_document"] == "versioned"
    assert grades["issue_evidence_manifest"] == "versioned"
    assert grades["issue_candidate_document"] == "must_compat"
    assert grades["issue_analysis_status"] == "must_compat"
    assert grades["issue_reconcile_status"] == "versioned"
    assert grades["change_issue_snapshot"] == "versioned"
    assert grades["trace_projection"] == "versioned"
    # must_compat, not versioned: metrics-sufficiency-gate routes on these
    # fields, so a document this release cannot fully validate must be refused
    # rather than read partially and routed as a pass.
    assert grades["metrics_document"] == "must_compat"
    # must_compat: brief / status / safety-check steer gate routing, and
    # apply-summary / baseline steer the safety verdict that feeds a gate, so a
    # document this release cannot fully validate must be refused rather than
    # read partially and routed as a pass — same rationale already recorded for
    # metrics_document.
    assert grades["coverage_repair_brief"] == "must_compat"
    assert grades["coverage_repair_status"] == "must_compat"
    assert grades["coverage_repair_safety_check"] == "must_compat"
    assert grades["coverage_repair_apply_summary"] == "must_compat"
    assert grades["coverage_repair_baseline"] == "must_compat"


def test_coverage_repair_artifacts_resolve_to_their_models_with_must_compat() -> None:
    # must_compat: brief / status / safety-check steer gate routing, and
    # apply-summary / baseline steer the safety verdict that feeds a gate, so a
    # document this release cannot fully validate must be refused rather than
    # read partially and routed as a pass — same rationale already recorded for
    # metrics_document.
    specs = (
        (COVERAGE_REPAIR_BRIEF_REL, "coverage_repair_brief", CoverageRepairBrief),
        (COVERAGE_REPAIR_STATUS_REL, "coverage_repair_status", CoverageRepairStatus),
        (COVERAGE_REPAIR_SAFETY_REL, "coverage_repair_safety_check", CoverageRepairSafetyCheck),
        (
            COVERAGE_REPAIR_APPLY_SUMMARY_REL,
            "coverage_repair_apply_summary",
            CoverageRepairApplySummary,
        ),
        (COVERAGE_REPAIR_BASELINE_REL, "coverage_repair_baseline", CoverageRepairBaseline),
    )
    for path, artifact_type, model in specs:
        spec = match_artifact(path)
        assert spec is not None, path
        assert spec.artifact_type == artifact_type
        assert spec.model is model
        assert spec.compat == "must_compat"


def test_the_authoritative_metrics_document_is_registered_at_the_inspect_path() -> None:
    spec = match_artifact("inspect/metrics.json")
    assert spec is not None
    assert spec.artifact_type == "metrics_document"
    assert spec.model is MetricsDocument
    assert spec.authoring_model is None


def test_minimum_coverage_artifacts_are_registered() -> None:
    from assurance_agent.artifacts.models.minimum_coverage import (
        MinimumCoverageMatrix,
        MinimumCoverageResult,
    )

    result = match_artifact("report/minimum-coverage-result.json")
    assert result is not None
    assert result.artifact_type == "minimum_coverage_result"
    assert result.model is MinimumCoverageResult
    assert result.compat == "must_compat"

    matrix = match_artifact("trace/minimum-coverage-matrix.yaml")
    assert matrix is not None
    assert matrix.artifact_type == "minimum_coverage_matrix"
    assert matrix.model is MinimumCoverageMatrix
    assert matrix.compat == "must_compat"


def test_the_batch_scoped_metrics_evidence_stays_unregistered() -> None:
    """Spec §9: `execution/runs/<batch>/metrics.json` is a derived pre-copy.

    Registering it too would put two authoritative writers on one artifact type
    and make the PR-phase immutability conflict unresolvable. Task 6 PR cadence
    collectors write the five evidence files below; they stay unregistered so
    Task 7's ``materialize-pr-metrics`` remains the sole authoritative writer of
    ``inspect/metrics.json``.
    """
    batch = "execution/runs/20260804-093000"
    for name in (
        "metrics.json",
        "coverage-diff.json",
        "constraint-coverage.json",
        "auth-matrix.json",
        "journey-coverage.json",
        "perf-slack.json",
    ):
        assert match_artifact(f"{batch}/{name}") is None


def test_the_nightly_metrics_document_is_registered_independently() -> None:
    """Spec §9: nightly lands on its own path so the PR document stays write-once."""
    from assurance_agent.artifacts.models.metrics import MetricsDocument

    nightly = match_artifact("inspect/metrics-nightly.json")
    pr = match_artifact("inspect/metrics.json")
    assert nightly is not None
    assert nightly.artifact_type == "metrics_nightly_document"
    assert nightly.model is MetricsDocument
    assert nightly.compat == "must_compat"
    assert pr is not None
    assert pr.artifact_type == "metrics_document"
    assert pr.artifact_type != nightly.artifact_type


def test_issue_artifact_patterns_match_exact_paths() -> None:
    issue_specs = {
        "observation_document": "inspect/observations.json",
        "issue_evidence_manifest": "inspect/issue-evidence-manifest.json",
        "issue_candidate_document": "inspect/issue-candidates.json",
        "issue_analysis_status": "inspect/issue-analysis-status.json",
        "issue_reconcile_status": "inspect/issue-reconcile-status.json",
        "change_issue_snapshot": "issues/snapshot.json",
    }
    for artifact_type, path in issue_specs.items():
        spec = match_artifact(path)
        assert spec is not None and spec.artifact_type == artifact_type, path
    assert match_artifact("issues/events.jsonl") is None
    assert match_artifact("qa/issues/problems.json") is None

def test_activated_generated_file_and_healing_paths_match_exact_models() -> None:
    from assurance_agent.artifacts.models import (
        ApiCodegenFixApplyIntentV1,
        ApiGeneratedFilesV1,
        E2eGeneratedFilesV1,
        FixerProposalApprovalReceiptV1,
        StrictWireModel,
    )

    assert issubclass(ApiGeneratedFilesV1, StrictWireModel)
    assert issubclass(ApiCodegenFixApplyIntentV1, StrictWireModel)
    assert issubclass(FixerProposalApprovalReceiptV1, StrictWireModel)
    api_files = match_artifact("codegen/api-generated-files.json")
    e2e_files = match_artifact("codegen/e2e-generated-files.json")
    api_intent = match_artifact("healing/api-apply-intent.json")
    approval = match_artifact("healing/fixer-proposal-approval.json")
    assert api_files is not None and api_files.model is ApiGeneratedFilesV1
    assert e2e_files is not None and e2e_files.model is E2eGeneratedFilesV1
    assert api_intent is not None and api_intent.model is ApiCodegenFixApplyIntentV1
    assert approval is not None and approval.model is FixerProposalApprovalReceiptV1

def test_trace_projection_registry_uses_document_wrapper() -> None:
    from assurance_agent.artifacts.models.trace import TraceProjectionDocument

    spec = match_artifact("inspect/trace-projection.json")
    assert spec is not None
    assert spec.model is TraceProjectionDocument
    assert spec.compat == "versioned"

def test_quality_gate_result_registry_uses_document_wrapper() -> None:
    from assurance_agent.artifacts.models.inspect import QualityGateResultDocument

    spec = match_artifact("inspect/quality-gate-result.json")
    assert spec is not None
    assert spec.model is QualityGateResultDocument
    assert spec.compat == "versioned"

def test_issue_reconcile_status_registry_uses_document_wrapper() -> None:
    from assurance_agent.artifacts.models.issues import IssueReconcileStatusDocument

    spec = match_artifact("inspect/issue-reconcile-status.json")
    assert spec is not None
    assert spec.model is IssueReconcileStatusDocument
    assert spec.compat == "versioned"
