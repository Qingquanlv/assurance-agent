from __future__ import annotations

import html
import json
from pathlib import Path

from assurance_agent.eval.paths import reports_dir, runs_dir
from assurance_agent.eval.types import EvalGateResult, RunManifest, SuiteMetrics


def _build_report(manifest: RunManifest, metrics: SuiteMetrics,
                  gate: EvalGateResult) -> dict:
    return {
        "run_id": manifest.run_id,
        "suite": manifest.suite,
        "verdict": gate.verdict,
        "started_at": manifest.started_at,
        "completed_at": manifest.completed_at,
        "sample_ids": manifest.selected_sample_ids,
        "metrics": metrics.metrics,
        "per_sample": metrics.per_sample,
        "hard_gate_failures": gate.hard_gate_failures,
        "warnings": gate.warnings,
    }


def _render_html(report: dict) -> str:
    rows = "".join(
        f"<tr><td>{html.escape(k)}</td><td>{v}</td></tr>"
        for k, v in sorted(report["metrics"].items())
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>eval {html.escape(report['run_id'])}</title></head><body>"
        f"<h1>{html.escape(report['suite'])}</h1>"
        f"<p>run_id: {html.escape(report['run_id'])}</p>"
        f"<p>verdict: <strong>{html.escape(report['verdict'])}</strong></p>"
        f"<table border='1'><tr><th>metric</th><th>value</th></tr>{rows}</table>"
        "</body></html>"
    )


def _render_md(report: dict) -> str:
    lines = [f"# eval report — {report['suite']}", "",
             f"- run_id: `{report['run_id']}`",
             f"- verdict: **{report['verdict']}**", "", "## Metrics", ""]
    for name, value in sorted(report["metrics"].items()):
        lines.append(f"- `{name}`: {value}")
    return "\n".join(lines) + "\n"


def write_run_report(run_dir: Path, manifest: RunManifest, metrics: SuiteMetrics,
                     gate: EvalGateResult) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    report = _build_report(manifest, metrics, gate)
    (run_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (run_dir / "report.html").write_text(_render_html(report), encoding="utf-8")
    (run_dir / "report.md").write_text(_render_md(report), encoding="utf-8")


def generate_trend_report(project_root: Path, suite: str, *, date_from: str | None = None,
                          date_to: str | None = None, html_out: Path | None = None) -> Path:
    points: list[dict] = []
    root = runs_dir(project_root)
    if root.is_dir():
        for run in sorted(root.iterdir()):
            report_path = run / "report.json"
            if not report_path.exists():
                continue
            data = json.loads(report_path.read_text(encoding="utf-8"))
            if data.get("suite") != suite:
                continue
            started = data.get("started_at") or ""
            if date_from and started < date_from:
                continue
            if date_to and started > date_to:
                continue
            points.append(data)
    out = html_out or (reports_dir(project_root) / f"trend-{suite}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = "".join(
        f"<tr><td>{html.escape(p['run_id'])}</td><td>{html.escape(p['verdict'])}</td></tr>"
        for p in points
    )
    out.write_text(
        f"<!doctype html><html><body><h1>trend — {html.escape(suite)}</h1>"
        f"<table border='1'><tr><th>run</th><th>verdict</th></tr>{rows}</table>"
        "</body></html>",
        encoding="utf-8",
    )
    return out
