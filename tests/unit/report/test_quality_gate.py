import pytest

from assurance_agent.artifacts.models import CoverageThreshold, PerformanceScenarioVerdict
from assurance_agent.workflow.execution.results import (
    CaseResult,
    CoverageResult,
    PerformanceResult,
    ResultSource,
    TargetResult,
)
from assurance_agent.workflow.report.quality_gate import build_quality_gate, worst_status


@pytest.mark.parametrize(
    "statuses,expected",
    [
        ([], "SKIPPED"),
        (["SKIPPED", "SKIPPED"], "SKIPPED"),
        (["PASS", "SKIPPED"], "PASS"),
        (["PASS", "PASS_WITH_WARNINGS"], "PASS_WITH_WARNINGS"),
        (["PASS_WITH_WARNINGS", "FAIL"], "FAIL"),
        (["FAIL", "PASS"], "FAIL"),
    ],
)
def test_worst_status_matrix(statuses, expected) -> None:
    assert worst_status(statuses) == expected


def make_target(target: str, total: int, passed: int, failed: int, unmapped: int = 0) -> TargetResult:
    cases = [
        CaseResult(
            case_id=f"TC_{target.upper()}_{i:03d}",
            status="passed",
            file="f.py",
            test_name="t",
            duration_ms=1,
            message="",
        )
        for i in range(passed)
    ]
    unmapped_cases = [
        CaseResult(case_id="", status="passed", file="f.py", test_name="t", duration_ms=1, message="")
        for _ in range(unmapped)
    ]
    return TargetResult(
        change_id="CH-1",
        batch_id="b1",
        target=target,  # type: ignore[arg-type]
        status="failed" if failed else ("passed" if total else "skipped"),
        command="cmd",
        source=ResultSource(framework="pytest", raw_log="raw/x.log"),
        total=total,
        passed=passed,
        failed=failed,
        skipped=0,
        cases=cases,
        unmapped_tests=unmapped_cases,
    )


def make_coverage(status: str, available: bool = True, line: float = 85.0) -> CoverageResult:
    return CoverageResult(
        change_id="CH-1",
        batch_id="b1",
        available=available,
        line_coverage=line,
        branch_coverage=70.0,
        threshold=CoverageThreshold(line=70, branch=60),
        status=status,  # type: ignore[arg-type]
    )


def test_all_pass_gate_is_pass() -> None:
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=make_target("api", 5, 5, 0),
        e2e=make_target("e2e", 2, 2, 0),
        coverage=make_coverage("PASS"),
        coverage_gate_mode="warn",
    )
    assert gate.dimensions.functional.status == "PASS"
    assert gate.final_status == "PASS"
    assert gate.warnings is None


def test_any_functional_fail_gate_is_fail() -> None:
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=make_target("api", 5, 4, 1),
        e2e=make_target("e2e", 2, 2, 0),
        coverage=make_coverage("PASS"),
        coverage_gate_mode="warn",
    )
    assert gate.final_status == "FAIL"


def test_nothing_ran_gate_is_skipped() -> None:
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=None,
        e2e=None,
        coverage=make_coverage("SKIPPED", available=False),
        coverage_gate_mode="warn",
    )
    assert gate.final_status == "SKIPPED"


def test_coverage_below_threshold_warn_is_pass_with_warnings() -> None:
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=make_target("api", 3, 3, 0),
        e2e=None,
        coverage=make_coverage("PASS_WITH_WARNINGS", line=50.0),
        coverage_gate_mode="warn",
    )
    assert gate.dimensions.coverage.status == "PASS_WITH_WARNINGS"
    assert gate.final_status == "PASS_WITH_WARNINGS"


def test_coverage_below_threshold_block_is_fail() -> None:
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=make_target("api", 3, 3, 0),
        e2e=None,
        coverage=make_coverage("PASS_WITH_WARNINGS", line=50.0),
        coverage_gate_mode="block",
    )
    assert gate.dimensions.coverage.status == "FAIL"
    assert gate.final_status == "FAIL"


def test_unmapped_tests_downgrade_functional_and_add_warning() -> None:
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=make_target("api", 3, 3, 0, unmapped=2),
        e2e=None,
        coverage=make_coverage("PASS"),
        coverage_gate_mode="warn",
    )
    assert gate.dimensions.functional.status == "PASS_WITH_WARNINGS"
    assert gate.dimensions.functional.unmapped_tests == 2
    assert gate.final_status == "PASS_WITH_WARNINGS"
    assert gate.warnings is not None
    assert any("TRACEABILITY-BROKEN" in w for w in gate.warnings)


def test_fuzz_folds_into_functional() -> None:
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=make_target("api", 2, 2, 0),
        e2e=None,
        coverage=make_coverage("PASS"),
        coverage_gate_mode="warn",
        fuzz=make_target("fuzz", 3, 2, 1),
    )
    assert gate.dimensions.functional.status == "FAIL"
    assert gate.dimensions.functional.fuzz is not None
    assert gate.dimensions.functional.fuzz.failed == 1
    assert gate.final_status == "FAIL"


def test_performance_fail_forms_non_functional_and_fails_gate() -> None:
    perf = PerformanceResult(
        change_id="CH-1",
        batch_id="b1",
        available=True,
        status="FAIL",
        scenarios=[
            PerformanceScenarioVerdict(
                capability="c",
                endpoint="/e",
                measured_p95_ms=900.0,
                threshold_p95_ms=500.0,
                measured_error_rate=0.0,
                threshold_error_rate_max=0.01,
                verdict="FAIL",
            )
        ],
    )
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=make_target("api", 2, 2, 0),
        e2e=None,
        coverage=make_coverage("PASS"),
        coverage_gate_mode="warn",
        performance=perf,
    )
    assert gate.dimensions.non_functional is not None
    assert gate.dimensions.non_functional.status == "FAIL"
    assert gate.final_status == "FAIL"
