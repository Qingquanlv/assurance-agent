from assurance_execution.execution_view import (
    collect_test_support_files,
    execution_view_relative,
)


def test_execution_view_relative_is_qa_staging() -> None:
    assert execution_view_relative("batch-1") == "qa/.staging/execution/batch-1"


def test_collect_test_support_files_reads_qa_fixtures(tmp_path) -> None:
    fixture = tmp_path / "qa" / "fixtures" / "api" / "conftest.py"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("import pytest\n")
    (tmp_path / "tests" / "api").mkdir(parents=True)
    (tmp_path / "tests" / "api" / "conftest.py").write_text("# ignored project-root tests\n")
    collected = collect_test_support_files(tmp_path)
    assert "tests/api/conftest.py" in collected
    assert collected["tests/api/conftest.py"][0] == b"import pytest\n"
