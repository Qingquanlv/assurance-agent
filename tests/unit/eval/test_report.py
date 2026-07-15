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
    assert (tmp_path / "report.md").exists()
