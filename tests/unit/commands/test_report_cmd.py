import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from assurance_agent.artifacts.models import CoverageThreshold, SelectedTargets
from assurance_agent.cli import main
from assurance_agent.workflow.execution.evidence import publish_execution_evidence
from assurance_agent.workflow.execution.results import CaseResult, CoverageResult, ResultSource, TargetResult
from assurance_agent.workflow.report.quality_gate import build_quality_gate
from tests.helpers_aa import make_report_v2, sufficient_evidence_coverage, write_aa_config
from tests.unit.artifacts.test_models_inspect_report import make_coverage, make_functional


def _seed(root: Path, failed: bool) -> None:
    write_aa_config(root)
    (root / "qa" / "changes" / "CH-1").mkdir(parents=True, exist_ok=True)
    cases = [
        CaseResult(
            case_id="TC_API_001",
            status="passed",
            file="f.py",
            test_name="test_tc_api_001__ok",
            duration_ms=1,
            message="",
        )
    ]
    n_failed = 0
    if failed:
        n_failed = 1
        cases.append(
            CaseResult(
                case_id="TC_API_002",
                status="failed",
                file="f.py",
                test_name="test_tc_api_002__x",
                duration_ms=1,
                message="500 internal server error",
            )
        )
    api = TargetResult(
        change_id="CH-1",
        batch_id="20260715-000000",
        target="api",
        status="failed" if failed else "passed",
        command="cmd",
        source=ResultSource(framework="pytest", raw_log="raw/api.log"),
        total=len(cases),
        passed=len(cases) - n_failed,
        failed=n_failed,
        skipped=0,
        cases=cases,
        unmapped_tests=[],
    )
    cov = CoverageResult(
        change_id="CH-1",
        batch_id="20260715-000000",
        available=True,
        line_coverage=90.0,
        branch_coverage=80.0,
        threshold=CoverageThreshold(line=70, branch=60),
        status="PASS",
    )
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="20260715-000000",
        api=api,
        e2e=None,
        coverage=cov,
        evidence_coverage=sufficient_evidence_coverage(),
    )
    publish_execution_evidence(
        execution_dir=root / "qa" / "changes" / "CH-1" / "execution",
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


def test_report_inspect_then_generate_pass(tmp_path: Path, monkeypatch) -> None:
    _seed(tmp_path, failed=False)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    inspect = runner.invoke(main, ["report", "inspect", "--change", "CH-1"])
    assert inspect.exit_code == 0
    assert "PASS" in inspect.output
    generate = runner.invoke(main, ["report", "generate", "--change", "CH-1"])
    assert generate.exit_code == 0
    assert "100" in generate.output
    assert (tmp_path / "qa" / "changes" / "CH-1" / "report" / "quality-report.json").is_file()


def test_report_inspect_fail_exit_one(tmp_path: Path, monkeypatch) -> None:
    _seed(tmp_path, failed=True)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["report", "inspect", "--change", "CH-1"])
    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_report_inspect_missing_evidence_exit_one(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "qa" / "changes" / "CH-2").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["report", "inspect", "--change", "CH-2"])
    assert result.exit_code == 1
    assert "run" in result.output.lower()


@pytest.mark.parametrize("version", ["1.0", "2.0"])
def test_report_cli_accepts_persisted_quality_versions(
    tmp_path: Path,
    monkeypatch,
    version: str,
) -> None:
    """V1-only and V2-only quality artifacts dispatch concretely; QualityReport stays 1.1."""
    _seed(tmp_path, failed=False)
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
        doc = {
            "schema_version": "1.0",
            "change_id": "CH-1",
            "batch_id": "20260715-000000",
            "dimensions": {"functional": make_functional(), "coverage": make_coverage()},
            "final_status": "PASS",
        }
        assert "evidence" not in doc["dimensions"]["coverage"]
    else:
        coverage = {
            **make_coverage(),
            "evidence": {"kind": "sufficiency", "report": make_report_v2(verdicts=[])},
        }
        doc = {
            "schema_version": "2.0",
            "change_id": "CH-1",
            "batch_id": "20260715-000000",
            "dimensions": {"functional": make_functional(), "coverage": coverage},
            "final_status": "PASS",
        }
        assert doc["dimensions"]["coverage"]["evidence"]["kind"] == "sufficiency"
    gate_path.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    inspect = runner.invoke(main, ["report", "inspect", "--change", "CH-1"])
    assert inspect.exit_code == 0, inspect.output
    generate = runner.invoke(main, ["report", "generate", "--change", "CH-1"])
    assert generate.exit_code == 0, generate.output
    report = json.loads(
        (tmp_path / "qa" / "changes" / "CH-1" / "report" / "quality-report.json").read_text(encoding="utf-8")
    )
    assert report["schema_version"] == "1.1"
    # Concrete dispatch must not upgrade the on-disk quality gate artifact.
    persisted = json.loads(gate_path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == version
