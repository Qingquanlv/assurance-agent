from assurance_execution.execution_view import (
    collect_test_support_files,
    execution_view_relative,
)


def test_execution_view_relative_is_qa_staging() -> None:
    assert execution_view_relative("batch-1") == "qa/.staging/execution/batch-1"


def test_collect_test_support_files_reads_qa_tests_and_ignores_sut_tests(tmp_path) -> None:
    durable = tmp_path / "qa" / "tests" / "api" / "conftest.py"
    durable.parent.mkdir(parents=True)
    durable.write_text("import pytest\n")
    fixture = tmp_path / "qa" / "fixtures" / "api" / "conftest.py"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("from qa.fixtures import leftover\n")
    (tmp_path / "tests" / "api").mkdir(parents=True)
    (tmp_path / "tests" / "api" / "conftest.py").write_text("# ignored project-root tests\n")
    collected = collect_test_support_files(tmp_path)
    assert collected["qa/tests/api/conftest.py"][0] == b"import pytest\n"
