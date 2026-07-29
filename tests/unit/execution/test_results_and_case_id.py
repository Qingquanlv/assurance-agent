from assurance_agent.artifacts.models import CoverageThreshold, PerformanceScenarioVerdict
from assurance_agent.evidence.case_id import canonicalize_case_id, extract_case_id
from assurance_agent.workflow.execution.results import (
    CaseResult,
    CoverageResult,
    PerformanceResult,
    ResultSource,
    TargetResult,
)


def test_canonicalize_uppercases_and_underscores() -> None:
    assert canonicalize_case_id("tc-menu-api-001") == "TC_MENU_API_001"
    assert canonicalize_case_id("TC_MENU_001") == "TC_MENU_001"


def test_extract_case_id_from_pytest_nodeid() -> None:
    nodeid = "tests/api/test_menu.py::test_tc_menu_api_001__create_menu_happy_path"
    assert extract_case_id(nodeid) == "TC_MENU_API_001"


def test_extract_case_id_accepts_legacy_hyphen_form() -> None:
    assert extract_case_id("TC-ROLE-002 role list") == "TC_ROLE_002"


def test_extract_case_id_returns_empty_when_absent() -> None:
    assert extract_case_id("test_plain_smoke_check") == ""


def test_target_result_defaults_and_roundtrip() -> None:
    result = TargetResult(
        change_id="CH-1",
        batch_id="20260715-101500",
        target="api",
        status="failed",
        command="uv run pytest tests/api",
        source=ResultSource(framework="pytest", raw_log="raw/api.log"),
        total=2,
        passed=1,
        failed=1,
        skipped=0,
        cases=[
            CaseResult(
                case_id="TC_MENU_001",
                status="failed",
                file="tests/api/test_menu.py",
                test_name="test_tc_menu_001__create",
                duration_ms=12,
                message="AssertionError",
            )
        ],
        unmapped_tests=[],
    )
    assert result.schema_version == "1.0"
    assert result.cases[0].trace == ""
    assert result.source.report_json == ""


def test_coverage_result_uses_shared_threshold() -> None:
    cov = CoverageResult(
        change_id="CH-1",
        batch_id="b1",
        available=True,
        line_coverage=85.0,
        branch_coverage=70.0,
        threshold=CoverageThreshold(line=70, branch=60),
        status="PASS",
    )
    assert cov.kind == "coverage"
    assert cov.threshold.line == 70


def test_performance_result_holds_scenario_verdicts() -> None:
    perf = PerformanceResult(
        change_id="CH-1",
        batch_id="b1",
        available=True,
        status="FAIL",
        scenarios=[
            PerformanceScenarioVerdict(
                capability="list_menus",
                endpoint="/api/v1/menus",
                measured_p95_ms=900.0,
                threshold_p95_ms=500.0,
                measured_error_rate=0.0,
                threshold_error_rate_max=0.01,
                verdict="FAIL",
            )
        ],
    )
    assert perf.scenarios[0].verdict == "FAIL"
