from agent_runtime_contracts.qa_paths import qa_join, qa_route


def test_qa_join_dispatches_suffix_classes() -> None:
    assert qa_join("cases/system/dept/case.yaml") == "qa/cases/system/dept/case.yaml"
    assert qa_join("tests/api/test_dept.py") == "qa/tests/api/test_dept.py"
    assert qa_join("fixtures/api/conftest.py") == "qa/fixtures/api/conftest.py"
    assert qa_join("codegen/api-generated-files.json") == "qa/results/codegen/api-generated-files.json"
    assert qa_join("plans/api-plan.md") == "qa/results/plans/api-plan.md"
    assert qa_join("plan/api/reviews/epochs/{coverage_epoch}/rounds/{review_round}.json") == (
        "qa/results/plan/api/reviews/epochs/{coverage_epoch}/rounds/{review_round}.json"
    )
    assert qa_join("review/api-plan-review.json") == "qa/results/review/api-plan-review.json"
    assert qa_join("execution/api-result.json") == "qa/results/execution/api-result.json"
    assert qa_join("inspect/issue-analysis.json") == "qa/results/inspect/issue-analysis.json"
    assert qa_join("healing/fix-proposal.json") == "qa/results/healing/fix-proposal.json"
    assert qa_join("explore/context.json") == "qa/results/explore/context.json"
    assert qa_join("report/report.md") == "qa/results/report/report.md"
    assert qa_join("facts/fact-baseline.json") == "qa/results/facts/fact-baseline.json"
    assert qa_join("trace/minimum-coverage-matrix.json") == "qa/results/trace/minimum-coverage-matrix.json"
    assert qa_join("requirement.md") == "qa/requirement.md"
    assert qa_join("proposal.md") == "qa/proposal.md"
    assert qa_join(".qa.yaml") == "qa/.qa.yaml"
    assert qa_join(".staging/execution/{batch_id}") == "qa/.staging/execution/{batch_id}"
    assert qa_join(".runtime/receipts/x.json") == "qa/.runtime/receipts/x.json"


def test_qa_join_rejects_legacy_and_project_tests_writes() -> None:
    import pytest

    for suffix in (
        "/".join(("qa", "changes", "CH-1", "cases", "x.yaml")),
        "/".join(("changes", "CH-1", "cases", "x.yaml")),
        "/".join(("archive", "CH-1", "summary.md")),
        "/abs/path",
    ):
        with pytest.raises(ValueError):
            qa_join(suffix)


def test_qa_join_rejects_escaping_path_segments() -> None:
    import pytest

    for suffix in (
        "../../outside",
        "cases/../archive/x",
        "plans/./api-plan.md",
        "tests/api/../outside.py",
    ):
        with pytest.raises(ValueError):
            qa_join(suffix)


def test_qa_route_sorts_unique_results() -> None:
    assert qa_route("review/a.json", "cases/x.yaml", "review/a.json") == (
        "qa/cases/x.yaml",
        "qa/results/review/a.json",
    )
