from assurance_agent.artifacts.registry import REGISTRY, match_artifact


def test_registry_covers_every_expected_artifact_type() -> None:
    expected = {
        "advisory",
        "apply_summary",
        "case_yaml",
        "execution_manifest",
        "fact_baseline",
        "failure_analysis",
        "fix_proposal",
        "qa_yaml",
        "quality_gate_result",
        "quality_report",
        "review",
        "safety_check",
        "workflow_state",
    }
    assert {spec.artifact_type for spec in REGISTRY} == expected
    assert len(REGISTRY) == 13


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
