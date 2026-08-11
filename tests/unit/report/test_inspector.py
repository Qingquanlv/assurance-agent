import json
from pathlib import Path

import pytest

from assurance_agent.artifacts.models import (
    CoverageThreshold,
    EvidenceCoverageErrorV2,
    QualityGateResultV1,
    QualityGateResultV2,
    SelectedTargets,
)
from assurance_agent.workflow.execution.evidence import publish_execution_evidence
from assurance_agent.workflow.execution.results import CaseResult, CoverageResult, ResultSource, TargetResult
from assurance_agent.workflow.report.inspector import inspect_change
from assurance_agent.workflow.report.quality_gate import build_quality_gate
from tests.helpers_aa import make_report_v2, sufficient_evidence_coverage, write_aa_config
from tests.unit.artifacts.test_models_inspect_report import make_coverage, make_functional


def _seed_change(tmp_path: Path, api: TargetResult, cov: CoverageResult) -> str:
    write_aa_config(tmp_path)
    change_dir = tmp_path / "qa" / "changes" / "CH-1"
    change_dir.mkdir(parents=True)
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="20260715-000000",
        api=api,
        e2e=None,
        coverage=cov,
        evidence_coverage=sufficient_evidence_coverage(),
    )
    publish_execution_evidence(
        execution_dir=change_dir / "execution",
        change_id="CH-1",
        batch_id="20260715-000000",
        selected_targets=SelectedTargets(api=True, e2e=False, fuzz=False, performance=False),
        api=api,
        e2e=None,
        fuzz=None,
        coverage=cov,
        performance=None,
        quality_gate=gate,
        summary="# summary\n",
    )
    return "CH-1"


def _overwrite_quality(tmp_path: Path, *, version: str, error: bool = False) -> None:
    gate_path = (
        tmp_path
        / "qa"
        / "changes"
        / "CH-1"
        / "execution"
        / "runs"
        / "20260715-000000"
        / "quality-gate-result.json"
    )
    if version == "1.0":
        coverage = make_coverage()
        if error:
            coverage = {
                **coverage,
                "status": "FAIL",
                "evidence": {"error_code": "evidence_projection_missing"},
            }
        doc = {
            "schema_version": "1.0",
            "change_id": "CH-1",
            "batch_id": "20260715-000000",
            "dimensions": {"functional": make_functional(), "coverage": coverage},
            "final_status": "FAIL" if error else "PASS",
        }
    else:
        if error:
            evidence: dict = {"kind": "error", "error_code": "evidence_projection_missing"}
            status = "FAIL"
            final = "FAIL"
        else:
            evidence = {"kind": "sufficiency", "report": make_report_v2(verdicts=[])}
            status = "PASS"
            final = "PASS"
        coverage = {**make_coverage(), "status": status, "evidence": evidence}
        doc = {
            "schema_version": "2.0",
            "change_id": "CH-1",
            "batch_id": "20260715-000000",
            "dimensions": {"functional": make_functional(), "coverage": coverage},
            "final_status": final,
        }
    gate_path.write_text(json.dumps(doc), encoding="utf-8")


def _api(failed_message: str | None) -> TargetResult:
    cases = [
        CaseResult(
            case_id="TC_API_001",
            status="passed",
            file="tests/api/t.py",
            test_name="test_tc_api_001__ok",
            duration_ms=1,
            message="",
        )
    ]
    failed = 0
    if failed_message is not None:
        failed = 1
        cases.append(
            CaseResult(
                case_id="TC_API_002",
                status="failed",
                file="tests/api/t.py",
                test_name="test_tc_api_002__x",
                duration_ms=1,
                message=failed_message,
            )
        )
    return TargetResult(
        change_id="CH-1",
        batch_id="20260715-000000",
        target="api",
        status="failed" if failed else "passed",
        command="cmd",
        source=ResultSource(framework="pytest", raw_log="raw/api.log"),
        total=len(cases),
        passed=len(cases) - failed,
        failed=failed,
        skipped=0,
        cases=cases,
        unmapped_tests=[],
    )


def _cov() -> CoverageResult:
    return CoverageResult(
        change_id="CH-1",
        batch_id="20260715-000000",
        available=True,
        line_coverage=90.0,
        branch_coverage=80.0,
        threshold=CoverageThreshold(line=70, branch=60),
        status="PASS",
    )


def test_inspect_no_failures_writes_analysis(tmp_path: Path) -> None:
    change_id = _seed_change(tmp_path, _api(None), _cov())
    result = inspect_change(tmp_path, change_id)
    assert result.analysis.status == "no_failures"
    assert result.analysis.final_status == "PASS"
    assert (tmp_path / "qa" / "changes" / "CH-1" / "inspect" / "failure-analysis.json").is_file()
    assert (tmp_path / "qa" / "changes" / "CH-1" / "inspect" / "quality-gate-result.json").is_file()


def test_inspect_skipped_execution_writes_no_failures(tmp_path: Path) -> None:
    skipped = TargetResult(
        change_id="CH-1",
        batch_id="20260715-000000",
        target="api",
        status="skipped",
        command="uv run pytest tests/api/test_user_api.py",
        source=ResultSource(framework="pytest", raw_log="raw/api.log"),
        total=0,
        passed=0,
        failed=0,
        skipped=0,
        cases=[],
        unmapped_tests=[
            CaseResult(
                case_id="",
                status="skipped",
                file="",
                test_name="pytest json report not found — pytest may not have run.",
                duration_ms=0,
                message="pytest json report not found — pytest may not have run.",
                raw_log_ref="raw/api.log",
            )
        ],
    )
    skipped_cov = CoverageResult(
        change_id="CH-1",
        batch_id="20260715-000000",
        available=False,
        line_coverage=0.0,
        branch_coverage=0.0,
        threshold=CoverageThreshold(line=70, branch=60),
        status="SKIPPED",
        skip_reason="coverage.json not produced",
    )
    change_id = _seed_change(tmp_path, skipped, skipped_cov)
    result = inspect_change(tmp_path, change_id)
    assert result.analysis.status == "no_failures"
    assert result.analysis.final_status == "PASS"
    assert result.analysis.failures == []
    assert (tmp_path / "qa" / "changes" / "CH-1" / "inspect" / "failure-analysis.json").is_file()


def test_inspect_classifies_locator_failure_as_fixable(tmp_path: Path) -> None:
    change_id = _seed_change(tmp_path, _api("AssertionError: expected 200 received 500"), _cov())
    result = inspect_change(tmp_path, change_id)
    assert result.analysis.status == "analyzed"
    assert result.analysis.final_status == "FAIL"
    assert len(result.analysis.failures) == 1
    assert result.analysis.failures[0].category == "assertion_failure"
    assert result.analysis.failures[0].id == "FAIL-001"


def test_inspect_missing_manifest_raises(tmp_path: Path) -> None:
    write_aa_config(tmp_path)
    (tmp_path / "qa" / "changes" / "CH-9").mkdir(parents=True)

    from assurance_agent.workflow.execution.evidence import EvidenceError

    with pytest.raises(EvidenceError):
        inspect_change(tmp_path, "CH-9")


@pytest.mark.parametrize("version", ["1.0", "2.0"])
def test_inspect_loads_concrete_quality_variant(tmp_path: Path, version: str) -> None:
    change_id = _seed_change(tmp_path, _api(None), _cov())
    _overwrite_quality(tmp_path, version=version)
    result = inspect_change(tmp_path, change_id)
    expected = QualityGateResultV1 if version == "1.0" else QualityGateResultV2
    assert isinstance(result.quality_gate, expected)
    assert result.analysis.inspect_mode == "primary"


def test_inspect_v2_error_evidence_without_traceback(tmp_path: Path) -> None:
    change_id = _seed_change(tmp_path, _api(None), _cov())
    _overwrite_quality(tmp_path, version="2.0", error=True)
    result = inspect_change(tmp_path, change_id)
    assert isinstance(result.quality_gate, QualityGateResultV2)
    assert isinstance(result.quality_gate.dimensions.coverage.evidence, EvidenceCoverageErrorV2)
    assert result.analysis.final_status == "FAIL"
