import json
from pathlib import Path

from assurance_agent.workflow.execution.pytest_parser import parse_pytest_json
from assurance_agent.workflow.execution.results import PytestTarget


def write_report(tmp_path: Path, tests: list[dict], **extra) -> Path:
    path = tmp_path / "api-report.json"
    path.write_text(json.dumps({"tests": tests, **extra}), encoding="utf-8")
    return path


def parse(tmp_path: Path, tests: list[dict], target: PytestTarget = "api", **extra):
    report = write_report(tmp_path, tests, **extra)
    return parse_pytest_json(
        change_id="CH-1",
        batch_id="20260715-101500",
        target=target,
        report_path=report,
        raw_log_path=str(tmp_path / "raw" / "api.log"),
        command="uv run pytest tests/api",
    )


def test_pass_fail_error_skip_matrix(tmp_path: Path) -> None:
    result = parse(
        tmp_path,
        [
            {
                "nodeid": "tests/api/test_m.py::test_tc_m_001__ok",
                "outcome": "passed",
                "call": {"outcome": "passed", "duration": 0.01},
            },
            {
                "nodeid": "tests/api/test_m.py::test_tc_m_002__bad",
                "outcome": "failed",
                "call": {"outcome": "failed", "duration": 0.02, "longrepr": "AssertionError: 200 != 500"},
            },
            {
                "nodeid": "tests/api/test_m.py::test_tc_m_003__boom",
                "outcome": "error",
                "setup": {"outcome": "error", "duration": 0.0, "longrepr": "ImportError: no module"},
            },
            {
                "nodeid": "tests/api/test_m.py::test_tc_m_004__skip",
                "outcome": "skipped",
                "setup": {"outcome": "skipped", "duration": 0.0, "longrepr": "Skipped: no data"},
            },
        ],
    )
    assert result.total == 4
    assert result.passed == 1
    assert result.failed == 2  # error folds into failed
    assert result.skipped == 1
    assert result.status == "failed"
    by_id = {c.case_id: c for c in result.cases}
    assert by_id["TC_M_002"].message == "AssertionError: 200 != 500"
    assert by_id["TC_M_002"].duration_ms == 20
    assert by_id["TC_M_003"].status == "failed"
    assert by_id["TC_M_003"].message == "ImportError: no module"


def test_unmapped_tests_have_no_case_id(tmp_path: Path) -> None:
    result = parse(
        tmp_path,
        [
            {
                "nodeid": "tests/api/test_x.py::test_plain_smoke",
                "outcome": "passed",
                "call": {"outcome": "passed", "duration": 0.0},
            },
        ],
    )
    assert result.cases == []
    assert len(result.unmapped_tests) == 1
    assert result.unmapped_tests[0].case_id == ""
    assert result.total == 1
    assert result.status == "passed"


def test_all_passed_status_passed(tmp_path: Path) -> None:
    result = parse(
        tmp_path,
        [
            {
                "nodeid": "tests/api/test_m.py::test_tc_m_001__ok",
                "outcome": "passed",
                "call": {"outcome": "passed", "duration": 0.0},
            },
        ],
    )
    assert result.status == "passed"
    assert result.failed == 0


def test_empty_tests_list_is_skipped_status(tmp_path: Path) -> None:
    result = parse(tmp_path, [])
    assert result.status == "skipped"
    assert result.total == 0


def test_all_skipped_tests_are_not_reported_as_passed(tmp_path: Path) -> None:
    result = parse(
        tmp_path,
        [
            {
                "nodeid": "tests/api/test_m.py::test_tc_m_001__requires_token",
                "outcome": "skipped",
                "setup": {
                    "outcome": "skipped",
                    "duration": 0.0,
                    "longrepr": "Skipped: API_ADMIN_TOKEN is required",
                },
            }
        ],
    )

    assert result.status == "skipped"
    assert result.total == 1
    assert result.passed == 0
    assert result.skipped == 1


def test_no_tests_collected_exit5_is_skipped(tmp_path: Path) -> None:
    """exitcode 5 (no tests collected) with an empty tests list → benign skip."""
    result = parse(tmp_path, [], exitcode=5, summary={"total": 0, "collected": 0})
    assert result.status == "skipped"
    assert result.total == 0


def test_aborted_session_with_collected_tests_is_failed(tmp_path: Path) -> None:
    """Regression: a suite that COLLECTED tests but aborted before running any
    (conftest `pytest.exit()` on an unready SUT → exitcode 2) must surface as
    failed, not be masked as a benign skip. This is exactly the E2E 502 case
    that silently zeroed out E2E coverage across the benchmark."""
    result = parse(
        tmp_path,
        [],
        target="e2e",
        exitcode=2,
        summary={"total": 0, "collected": 5},
    )
    assert result.status == "failed"
    assert result.failed == 1
    assert result.total == 1
    msg = result.unmapped_tests[0].message.lower()
    assert "aborted" in msg and "exitcode=2" in msg and "collected=5" in msg


def test_internal_error_exit3_with_no_cases_is_failed(tmp_path: Path) -> None:
    """A collection-time internal/usage error (exitcode 3/4) is a failure, not a skip."""
    result = parse(tmp_path, [], exitcode=3)
    assert result.status == "failed"
    assert result.failed == 1


def test_missing_report_file_is_skipped_not_fabricated(tmp_path: Path) -> None:
    result = parse_pytest_json(
        change_id="CH-1",
        batch_id="b1",
        target="api",
        report_path=tmp_path / "does-not-exist.json",
        raw_log_path=str(tmp_path / "raw" / "api.log"),
        command="uv run pytest tests/api",
    )
    assert result.status == "skipped"
    assert result.total == 0
    assert result.passed == 0
    assert len(result.unmapped_tests) == 1
    assert "not found" in result.unmapped_tests[0].message.lower()


def test_corrupt_report_json_is_skipped(tmp_path: Path) -> None:
    bad = tmp_path / "api-report.json"
    bad.write_text("{not json", encoding="utf-8")
    result = parse_pytest_json(
        change_id="CH-1",
        batch_id="b1",
        target="api",
        report_path=bad,
        raw_log_path="raw/api.log",
        command="cmd",
    )
    assert result.status == "skipped"
    assert "parse" in result.unmapped_tests[0].message.lower()


def test_e2e_target_preserved(tmp_path: Path) -> None:
    result = parse(
        tmp_path,
        [
            {
                "nodeid": "tests/e2e/test_login.py::test_tc_login_e2e_001__happy",
                "outcome": "passed",
                "call": {"outcome": "passed", "duration": 0.5},
            }
        ],
        target="e2e",
    )
    assert result.target == "e2e"
    assert result.cases[0].case_id == "TC_LOGIN_E2E_001"
