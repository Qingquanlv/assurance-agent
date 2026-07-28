from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from assurance_agent.eval.report import EvalProjectionConflict, write_run_report
from assurance_agent.eval.types import EvalGateResult, RunManifest, SuiteMetrics


def test_write_run_report_json_html_md(tmp_path: Path) -> None:
    manifest = RunManifest(
        run_id="run-1",
        suite="workflow-case",
        scorer="workflow_case",
        selected_sample_ids=["WC-001"],
        total_samples=1,
        executed_samples=1,
        target_model="m",
        change_ids=("CH-1", "CH-2"),
        started_at="2026-07-15T00:00:00Z",
        completed_at="2026-07-15T00:01:00Z",
    )
    metrics = SuiteMetrics(
        run_id="run-1",
        suite="workflow-case",
        sample_count=1,
        metrics={"case_review_gate_pass_rate": 1.0},
        per_sample={"WC-001": {"case_review_gate_pass_rate": 1.0}},
    )
    gate = EvalGateResult(run_id="run-1", suite="workflow-case", verdict="pass")
    write_run_report(tmp_path, manifest, metrics, gate)
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["run_id"] == "run-1"
    assert report["schema_version"] == "2"
    assert report["source_change_ids"] == ["CH-1", "CH-2"]
    assert report["verdict"] == "pass"
    assert report["metrics"]["case_review_gate_pass_rate"] == 1.0
    html = (tmp_path / "report.html").read_text()
    assert "workflow-case" in html and "pass" in html
    # Rich template: styled sections + verdict pill + per-sample table present.
    assert "<style>" in html
    assert "verdict-pill" in html and "PASS" in html
    assert "Per-Sample Metrics" in html
    assert "case_review_gate_pass_rate" in html
    assert "WC-001" in html
    assert (tmp_path / "report.md").exists()


def test_run_report_html_shows_failures(tmp_path: Path) -> None:
    manifest = RunManifest(
        run_id="run-2",
        suite="workflow-run",
        scorer="workflow_run",
        selected_sample_ids=["WR-001"],
        total_samples=1,
        executed_samples=1,
        target_model="m",
        started_at="2026-07-15T00:00:00Z",
        completed_at="2026-07-15T00:00:30Z",
    )
    metrics = SuiteMetrics(
        run_id="run-2",
        suite="workflow-run",
        sample_count=1,
        metrics={"secret_leak_count": 1.0},
        per_sample={"WR-001": {"secret_leak_count": 1.0}},
    )
    gate = EvalGateResult(
        run_id="run-2",
        suite="workflow-run",
        verdict="fail",
        hard_gate_failures=["secret_leak_count"],
        threshold_failures=["secret_leak_count: 1.0 !eq 0.0"],
    )
    write_run_report(tmp_path, manifest, metrics, gate)
    html = (tmp_path / "report.html").read_text()
    assert "verdict-fail" in html and "FAIL" in html
    assert "Hard Gate Failures" in html
    assert "Threshold Failures" in html
    assert "secret_leak_count: 1.0 !eq 0.0" in html


def test_write_run_report_publishes_compact_immutable_projection(tmp_path: Path) -> None:
    run_dir = tmp_path / "raw" / "run-1"
    sut = tmp_path / "sut"
    manifest = RunManifest(
        run_id="run-1",
        suite="workflow-case",
        scorer="workflow_case",
        selected_sample_ids=["S-2", "S-1"],
        total_samples=2,
        executed_samples=2,
        target_model="secret-model",
        change_ids=("CH-1",),
        started_at="2026-07-15T00:00:00Z",
        completed_at="2026-07-15T00:01:00Z",
    )
    metrics = SuiteMetrics(
        run_id="run-1",
        suite="workflow-case",
        sample_count=2,
        metrics={"token_count": 999.0},
        per_sample={"S-1": {"token_count": 500.0}},
    )
    gate = EvalGateResult(
        run_id="run-1",
        suite="workflow-case",
        verdict="fail",
        threshold_failures=["coverage: 0.2 !gte 0.9"],
    )

    write_run_report(run_dir, manifest, metrics, gate, projection_root=sut)

    raw = (run_dir / "report.json").read_bytes()
    projection_path = sut / "qa/eval/runs/run-1/report.json"
    projection = json.loads(projection_path.read_text())
    assert projection["schema_version"] == "1"
    assert projection["source_change_ids"] == ["CH-1"]
    assert projection["sample_ids"] == ["S-1", "S-2"]
    assert projection["raw_report_sha256"] == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert not ({"metrics", "per_sample", "target_model", "samples", "token_count"} & projection.keys())

    before = projection_path.read_bytes()
    write_run_report(run_dir, manifest, metrics, gate, projection_root=sut)
    assert projection_path.read_bytes() == before
    with pytest.raises(EvalProjectionConflict):
        write_run_report(
            run_dir,
            manifest,
            metrics,
            gate.model_copy(update={"verdict": "pass", "threshold_failures": []}),
            projection_root=sut,
        )
