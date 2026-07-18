from __future__ import annotations

import json
from pathlib import Path

from assurance_agent.eval.report import write_run_report
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
