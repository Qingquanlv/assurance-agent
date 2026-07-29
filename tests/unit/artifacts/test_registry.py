from assurance_agent.artifacts.registry import REGISTRY, match_artifact


def test_registry_covers_every_expected_artifact_type() -> None:
    expected = {
        "advisory",
        "apply_summary",
        "case_yaml",
        "change_issue_snapshot",
        "data_knowledge_proposal",
        "execution_manifest",
        "eval_run_projection_v1",
        "fact_baseline",
        "failure_analysis",
        "fix_proposal",
        "issue_analysis_status",
        "issue_candidate_document",
        "issue_evidence_manifest",
        "issue_reconcile_status",
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
        "trace_projection",
        "workflow_state",
    }
    assert {spec.artifact_type for spec in REGISTRY} == expected
    assert len(REGISTRY) == 41


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


def test_qa_yaml_exact_match_only() -> None:
    spec = match_artifact(".qa.yaml")
    assert spec is not None and spec.artifact_type == "qa_yaml"
    assert match_artifact("sub/.qa.yaml") is None


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
