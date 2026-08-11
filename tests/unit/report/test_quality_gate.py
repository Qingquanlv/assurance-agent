from datetime import UTC, datetime

import pytest

from assurance_agent.artifacts.models import (
    CoverageThreshold,
    EvidenceCoverageErrorV2,
    EvidenceCoverageSuccessV2,
    PerformanceScenarioVerdict,
    QualityGateResultV2,
)
from assurance_agent.artifacts.models.sufficiency import SufficiencyReportV2
from assurance_agent.evidence.sufficiency import (
    EvidenceCoverageEvaluation,
    SufficiencyReport,
)
from assurance_agent.workflow.execution.results import (
    CaseResult,
    CoverageResult,
    PerformanceResult,
    ResultSource,
    TargetResult,
)
from assurance_agent.workflow.report.quality_gate import build_quality_gate, worst_status
from tests.helpers_aa import make_report_v2, make_verdict

AS_OF = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)


def gate_inputs() -> dict:
    return {
        "change_id": "CH-1",
        "batch_id": "b1",
        "api": make_target("api", 3, 3, 0),
        "e2e": None,
        "coverage": make_coverage("PASS"),
    }


def successful_v2_evidence(*, action: str = "require_human") -> EvidenceCoverageEvaluation:
    return EvidenceCoverageEvaluation(
        report=SufficiencyReportV2.model_validate(make_report_v2(verdicts=[])),
        action=action,  # type: ignore[arg-type]
        error_code=None,
    )


def missing_projection_evidence() -> EvidenceCoverageEvaluation:
    return EvidenceCoverageEvaluation(
        report=None,
        action=None,
        error_code="evidence_projection_missing",
    )


def _sufficient_eval(*, action: str = "require_human") -> EvidenceCoverageEvaluation:
    return successful_v2_evidence(action=action)


def _insufficient_eval(*, action: str) -> EvidenceCoverageEvaluation:
    return EvidenceCoverageEvaluation(
        report=SufficiencyReportV2.model_validate(
            make_report_v2(
                verdicts=[
                    make_verdict(
                        case_id="TC_API_001",
                        sufficient=False,
                        missing_kinds=["execution_recent"],
                        reason_codes=["never_run"],
                        execution_state="never_run",
                    )
                ]
            )
        ),
        action=action,  # type: ignore[arg-type]
        error_code=None,
    )


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


def test_quality_gate_v2_embeds_typed_current_sufficiency() -> None:
    gate = build_quality_gate(evidence_coverage=successful_v2_evidence(), **gate_inputs())
    assert gate.schema_version == "2.0"
    evidence = gate.dimensions.coverage.evidence
    assert isinstance(evidence, EvidenceCoverageSuccessV2)
    assert evidence.report.require_current_batch is True


def test_quality_gate_v2_embeds_closed_error() -> None:
    gate = build_quality_gate(evidence_coverage=missing_projection_evidence(), **gate_inputs())
    assert isinstance(gate.dimensions.coverage.evidence, EvidenceCoverageErrorV2)


def test_quality_gate_writer_rejects_legacy_sufficiency_report() -> None:
    with pytest.raises(TypeError, match="SufficiencyReportV2"):
        build_quality_gate(
            evidence_coverage=EvidenceCoverageEvaluation(
                report=SufficiencyReport(as_of=AS_OF, recency_hours=72, verdicts=()),
                action="warn",
                error_code=None,
            ),
            **gate_inputs(),
        )


def test_all_pass_gate_is_pass() -> None:
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=make_target("api", 5, 5, 0),
        e2e=make_target("e2e", 2, 2, 0),
        coverage=make_coverage("PASS"),
        evidence_coverage=_sufficient_eval(),
    )
    assert isinstance(gate, QualityGateResultV2)
    assert gate.dimensions.functional.status == "PASS"
    assert gate.dimensions.coverage.status == "PASS"
    assert gate.final_status == "PASS"
    assert gate.warnings is None


def test_any_functional_fail_gate_is_fail() -> None:
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=make_target("api", 5, 4, 1),
        e2e=make_target("e2e", 2, 2, 0),
        coverage=make_coverage("PASS"),
        evidence_coverage=_sufficient_eval(),
    )
    assert gate.final_status == "FAIL"


def test_nothing_ran_gate_is_skipped() -> None:
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=None,
        e2e=None,
        coverage=make_coverage("SKIPPED", available=False),
        evidence_coverage=_sufficient_eval(),
    )
    assert gate.dimensions.functional.status == "SKIPPED"
    assert gate.dimensions.coverage.status == "PASS"
    assert gate.final_status == "PASS"


@pytest.mark.parametrize(
    "action,expected_status,expected_final",
    [
        ("warn", "PASS_WITH_WARNINGS", "PASS_WITH_WARNINGS"),
        ("block", "FAIL", "FAIL"),
        ("require_human", "FAIL", "FAIL"),
    ],
)
def test_insufficient_evidence_maps_action_to_coverage_status(
    action: str,
    expected_status: str,
    expected_final: str,
) -> None:
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=make_target("api", 3, 3, 0),
        e2e=None,
        coverage=make_coverage("PASS"),
        evidence_coverage=_insufficient_eval(action=action),
    )
    assert gate.dimensions.coverage.status == expected_status
    assert gate.final_status == expected_final
    evidence = gate.dimensions.coverage.evidence
    assert isinstance(evidence, EvidenceCoverageSuccessV2)
    assert evidence.report.verdicts


@pytest.mark.parametrize(
    "error_code",
    ["evidence_projection_missing", "policy_error"],
)
def test_fail_closed_coverage_errors(error_code: str) -> None:
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=make_target("api", 3, 3, 0),
        e2e=None,
        coverage=make_coverage("PASS"),
        evidence_coverage=EvidenceCoverageEvaluation(
            report=None,
            action=None,
            error_code=error_code,  # type: ignore[arg-type]
        ),
    )
    assert gate.dimensions.coverage.status == "FAIL"
    evidence = gate.dimensions.coverage.evidence
    assert isinstance(evidence, EvidenceCoverageErrorV2)
    assert evidence.error_code == error_code
    assert gate.final_status == "FAIL"


def test_legacy_line_coverage_does_not_affect_status() -> None:
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=make_target("api", 3, 3, 0),
        e2e=None,
        coverage=make_coverage("PASS_WITH_WARNINGS", line=50.0),
        evidence_coverage=_insufficient_eval(action="require_human"),
    )
    assert gate.dimensions.coverage.line_coverage == 50.0
    assert gate.dimensions.coverage.available is True
    assert gate.dimensions.coverage.status == "FAIL"


def test_unmapped_tests_downgrade_functional_and_add_warning() -> None:
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="b1",
        api=make_target("api", 3, 3, 0, unmapped=2),
        e2e=None,
        coverage=make_coverage("PASS"),
        evidence_coverage=_sufficient_eval(),
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
        evidence_coverage=_sufficient_eval(),
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
        evidence_coverage=_sufficient_eval(),
        performance=perf,
    )
    assert gate.dimensions.non_functional is not None
    assert gate.dimensions.non_functional.status == "FAIL"
    assert gate.final_status == "FAIL"


def test_evidence_coverage_evaluation_rejects_invalid_union() -> None:
    with pytest.raises(ValueError, match="EvidenceCoverageEvaluation"):
        EvidenceCoverageEvaluation(report=None, action="warn", error_code=None)  # type: ignore[arg-type]
