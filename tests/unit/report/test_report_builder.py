from pathlib import Path

from assurance_agent.artifacts.models import SelectedTargets
from assurance_agent.workflow.execution.evidence import publish_execution_evidence
from assurance_agent.workflow.execution.results import CaseResult, CoverageResult, TargetResult
from assurance_agent.workflow.report.inspector import inspect_change
from assurance_agent.workflow.report.quality_gate import build_quality_gate
from assurance_agent.workflow.report.report_builder import generate_report


def _api(failed_message: str | None) -> TargetResult:
    cases = [CaseResult(case_id="TC_API_001", status="passed", file="tests/api/t.py",
                        test_name="test_tc_api_001__ok", duration_ms=1, message="")]
    failed = 0
    if failed_message is not None:
        failed = 1
        cases.append(CaseResult(case_id="TC_API_002", status="failed", file="tests/api/t.py",
                                test_name="test_tc_api_002__x", duration_ms=1, message=failed_message))
    return TargetResult(
        change_id="CH-1", batch_id="20260715-000000", target="api",
        status="failed" if failed else "passed", command="cmd",
        source={"framework": "pytest", "raw_log": "raw/api.log"},
        total=len(cases), passed=len(cases) - failed, failed=failed, skipped=0,
        cases=cases, unmapped_tests=[],
    )


def _cov() -> CoverageResult:
    return CoverageResult(
        change_id="CH-1", batch_id="20260715-000000", available=True, line_coverage=90.0,
        branch_coverage=80.0, threshold={"line": 70, "branch": 60}, status="PASS",
    )


def _seed_change(tmp_path: Path, api: TargetResult, cov: CoverageResult) -> str:
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    gate = build_quality_gate(
        change_id="CH-1", batch_id="20260715-000000", api=api, e2e=None,
        coverage=cov, coverage_gate_mode="warn",
    )
    publish_execution_evidence(
        execution_dir=change_dir / "execution", change_id="CH-1", batch_id="20260715-000000",
        selected_targets=SelectedTargets(api=True, e2e=False, fuzz=False, performance=False),
        api=api, e2e=None, fuzz=None, coverage=cov, performance=None,
        quality_gate=gate, summary="# summary\n",
    )
    return "CH-1"


def test_generate_report_all_pass(tmp_path: Path) -> None:
    change_id = _seed_change(tmp_path, _api(None), _cov())
    inspect_change(tmp_path, change_id)
    result = generate_report(tmp_path, change_id)
    assert result.report.final_status == "PASS"
    assert result.report.quality_score == 100
    assert result.report.risk_level == "LOW"
    report_dir = tmp_path / "qa" / "changes" / "CH-1" / "report"
    assert (report_dir / "quality-report.json").is_file()
    assert (report_dir / "quality-report.md").is_file()
    assert (report_dir / "executive-summary.md").is_file()


def test_generate_report_business_defect_is_high_risk(tmp_path: Path) -> None:
    change_id = _seed_change(tmp_path, _api("500 internal server error"), _cov())
    inspect_change(tmp_path, change_id)
    result = generate_report(tmp_path, change_id)
    assert result.report.final_status == "FAIL"
    assert result.report.risk_level == "HIGH"
    assert len(result.report.defects.product) == 1
