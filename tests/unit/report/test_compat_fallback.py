from pathlib import Path

import pytest

from assurance_agent.artifacts.models import CoverageThreshold, SelectedTargets
from assurance_agent.workflow.execution.evidence import EvidenceError, publish_execution_evidence
from assurance_agent.workflow.execution.results import CaseResult, CoverageResult, ResultSource, TargetResult
from assurance_agent.workflow.report.inspector import inspect_change
from assurance_agent.workflow.report.quality_gate import build_quality_gate


def _publish(root: Path, batch_id: str, *, failed: bool) -> None:
    cases = [CaseResult(case_id="TC_API_001", status="passed", file="f.py",
                        test_name="test_tc_api_001__ok", duration_ms=1, message="")]
    n_failed = 0
    if failed:
        n_failed = 1
        cases.append(CaseResult(case_id="TC_API_002", status="failed", file="f.py",
                                test_name="test_tc_api_002__x", duration_ms=1,
                                message="500 internal server error"))
    api = TargetResult(
        change_id="CH-1", batch_id=batch_id, target="api",
        status="failed" if failed else "passed", command="cmd",
        source=ResultSource(framework="pytest", raw_log="raw/api.log"),
        total=len(cases), passed=len(cases) - n_failed, failed=n_failed, skipped=0,
        cases=cases, unmapped_tests=[],
    )
    cov = CoverageResult(change_id="CH-1", batch_id=batch_id, available=True,
                         line_coverage=90.0, branch_coverage=80.0,
                         threshold=CoverageThreshold(line=70, branch=60), status="PASS")
    gate = build_quality_gate(change_id="CH-1", batch_id=batch_id, api=api, e2e=None,
                              coverage=cov, coverage_gate_mode="warn")
    publish_execution_evidence(
        execution_dir=root / "qa" / "changes" / "CH-1" / "execution", change_id="CH-1",
        batch_id=batch_id,
        selected_targets=SelectedTargets(api=True, e2e=False, fuzz=False, performance=False),
        api=api, e2e=None, fuzz=None, coverage=cov, performance=None, quality_gate=gate,
        summary="# summary\n",
    )


def test_primary_batch_remains_primary_when_complete(tmp_path: Path) -> None:
    _publish(tmp_path, "20260715-000001", failed=False)
    result = inspect_change(tmp_path, "CH-1")
    assert result.analysis.inspect_mode == "primary"
    assert result.analysis.compat_fallback_reason is None
    assert result.analysis.source_batch_id == "20260715-000001"


def test_corrupt_latest_pointer_falls_back_to_newest_complete_batch(tmp_path: Path) -> None:
    _publish(tmp_path, "20260715-000001", failed=True)
    _publish(tmp_path, "20260715-000002", failed=False)
    latest = tmp_path / "qa/changes/CH-1/execution/runs/20260715-000002/api-result.json"
    latest.unlink()
    result = inspect_change(tmp_path, "CH-1")
    assert result.analysis.inspect_mode == "compat_fallback"
    assert result.analysis.compat_fallback_reason
    assert result.analysis.source_batch_id == "20260715-000001"


def test_explicit_bad_batch_fails_without_fallback(tmp_path: Path) -> None:
    _publish(tmp_path, "20260715-000001", failed=False)
    with pytest.raises(EvidenceError):
        inspect_change(tmp_path, "CH-1", batch_id="20260715-999999")
