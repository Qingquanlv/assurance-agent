import json
from pathlib import Path

import pytest
import yaml

from assurance_agent.artifacts.models import CoverageThreshold, SelectedTargets
from assurance_agent.workflow.execution.evidence import (
    EvidenceError,
    load_execution_evidence,
    publish_execution_evidence,
)
from assurance_agent.workflow.execution.results import CoverageResult, ResultSource, TargetResult
from assurance_agent.workflow.report.quality_gate import build_quality_gate
from tests.helpers_aa import sufficient_evidence_coverage


def make_api(passed: int = 2, failed: int = 0) -> TargetResult:
    return TargetResult(
        change_id="CH-1",
        batch_id="20260715-000000",
        target="api",
        status="failed" if failed else "passed",
        command="cmd",
        source=ResultSource(framework="pytest", raw_log="raw/api.log"),
        total=passed + failed,
        passed=passed,
        failed=failed,
        skipped=0,
        cases=[],
        unmapped_tests=[],
    )


def make_cov(available: bool = True) -> CoverageResult:
    return CoverageResult(
        change_id="CH-1",
        batch_id="20260715-000000",
        available=available,
        line_coverage=90.0,
        branch_coverage=80.0,
        threshold=CoverageThreshold(line=70, branch=60),
        status="PASS" if available else "SKIPPED",
    )


def publish(tmp_path: Path, api: TargetResult, cov: CoverageResult):
    execution_dir = tmp_path / "execution"
    api_result = api
    gate = build_quality_gate(
        change_id="CH-1",
        batch_id="20260715-000000",
        api=api_result,
        e2e=None,
        coverage=cov,
        evidence_coverage=sufficient_evidence_coverage(),
    )
    manifest = publish_execution_evidence(
        execution_dir=execution_dir,
        change_id="CH-1",
        batch_id="20260715-000000",
        selected_targets=SelectedTargets(api=True, e2e=False, fuzz=False, performance=False),
        api=api_result,
        e2e=None,
        fuzz=None,
        coverage=cov,
        performance=None,
        quality_gate=gate,
        summary="# summary\n",
    )
    return execution_dir, manifest


def test_publish_writes_batch_dir_and_latest_pointers(tmp_path: Path) -> None:
    execution_dir, manifest = publish(tmp_path, make_api(), make_cov())
    batch_dir = execution_dir / "runs" / "20260715-000000"
    assert (batch_dir / "api-result.json").is_file()
    assert (batch_dir / "coverage-result.json").is_file()
    assert (batch_dir / "quality-gate-result.json").is_file()
    assert (batch_dir / "execution-manifest.yaml").is_file()
    assert (execution_dir / "api-result.json").is_file()
    assert (execution_dir / "execution-manifest.yaml").is_file()
    assert manifest.final_status == "PASS"
    assert manifest.result_files["api"] == "runs/20260715-000000/api-result.json"


def test_load_evidence_round_trips(tmp_path: Path) -> None:
    execution_dir, _ = publish(tmp_path, make_api(failed=1), make_cov())
    evidence = load_execution_evidence(execution_dir)
    assert evidence.batch_id == "20260715-000000"
    assert evidence.api is not None
    assert evidence.api.failed == 1
    assert evidence.coverage is not None
    assert evidence.quality_gate is not None
    assert evidence.quality_gate.final_status == "FAIL"
    assert evidence.integrity_issues == []


def test_load_missing_manifest_raises(tmp_path: Path) -> None:
    with pytest.raises(EvidenceError):
        load_execution_evidence(tmp_path / "execution")


def test_selected_target_with_missing_result_is_integrity_issue(tmp_path: Path) -> None:
    execution_dir, _ = publish(tmp_path, make_api(), make_cov())
    (execution_dir / "runs" / "20260715-000000" / "api-result.json").unlink()
    evidence = load_execution_evidence(execution_dir)
    assert evidence.api is None
    assert len(evidence.integrity_issues) == 1
    assert evidence.integrity_issues[0].target == "api"


def test_load_rejects_result_path_escape(tmp_path: Path) -> None:
    execution_dir, _ = publish(tmp_path, make_api(), make_cov())
    manifest = execution_dir / "execution-manifest.yaml"
    doc = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    doc["result_files"]["api"] = "../../outside.json"
    manifest.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(EvidenceError, match="escapes execution directory"):
        load_execution_evidence(execution_dir)


def test_explicit_batch_rejects_manifest_identity_mismatch(tmp_path: Path) -> None:
    execution_dir, _ = publish(tmp_path, make_api(), make_cov())
    manifest = execution_dir / "runs/20260715-000000/execution-manifest.yaml"
    doc = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    doc["batch_id"] = "20260715-999999"
    manifest.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(EvidenceError, match="batch id mismatch"):
        load_execution_evidence(execution_dir, batch_id="20260715-000000")


def test_result_identity_mismatch_is_integrity_issue(tmp_path: Path) -> None:
    execution_dir, _ = publish(tmp_path, make_api(), make_cov())
    result_path = execution_dir / "runs/20260715-000000/api-result.json"
    doc = json.loads(result_path.read_text(encoding="utf-8"))
    doc["batch_id"] = "20260715-999999"
    result_path.write_text(json.dumps(doc), encoding="utf-8")
    evidence = load_execution_evidence(execution_dir)
    assert any("identity mismatch" in issue.reason for issue in evidence.integrity_issues)
