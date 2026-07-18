from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

from assurance_agent.eval.paths import reports_dir, runs_dir
from assurance_agent.eval.types import EvalGateResult, RunManifest, SuiteMetrics

_VERDICT_CLASS = {
    "pass": "verdict-pass",
    "pass_with_warnings": "verdict-warn",
    "fail": "verdict-fail",
    "inconclusive": "verdict-inconclusive",
    "needs_human_review": "verdict-review",
}
_VERDICT_LABEL = {
    "pass": "PASS",
    "pass_with_warnings": "PASS WITH WARNINGS",
    "fail": "FAIL",
    "inconclusive": "INCONCLUSIVE",
    "needs_human_review": "NEEDS REVIEW",
}


def _build_report(manifest: RunManifest, metrics: SuiteMetrics, gate: EvalGateResult) -> dict:
    return {
        "run_id": manifest.run_id,
        "suite": manifest.suite,
        "verdict": gate.verdict,
        "started_at": manifest.started_at,
        "completed_at": manifest.completed_at,
        "sample_ids": manifest.selected_sample_ids,
        "sample_count": metrics.sample_count,
        "total_samples": manifest.total_samples,
        "executed_samples": manifest.executed_samples,
        "target_model": manifest.target_model,
        "metrics": metrics.metrics,
        "per_sample": metrics.per_sample,
        "hard_gate_failures": gate.hard_gate_failures,
        "threshold_failures": gate.threshold_failures,
        "warnings": gate.warnings,
        "inconclusive_count": gate.inconclusive_count,
    }


def _styles() -> str:
    return """
    :root{--bg:#f3f4f6;--card:#fff;--text:#111827;--muted:#6b7280;--border:#e5e7eb;
      --header-bg:#f9fafb;--pass:#059669;--pass-bg:#ecfdf5;--pass-border:#a7f3d0;
      --warn:#d97706;--warn-bg:#fffbeb;--fail:#dc2626;--fail-bg:#fef2f2;
      --inconclusive:#7c3aed;--review:#0891b2;}
    *{box-sizing:border-box;}
    body{margin:0;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
      background:var(--bg);color:var(--text);line-height:1.5;-webkit-font-smoothing:antialiased;}
    .page{max-width:1180px;margin:0 auto;padding:32px 28px 48px;}
    .page-header{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:24px;}
    .page-title{margin:0;font-size:1.75rem;font-weight:700;letter-spacing:-.02em;}
    .page-subtitle{margin:6px 0 0;color:var(--muted);font-size:.875rem;}
    .verdict-pill{display:inline-flex;align-items:center;gap:6px;padding:6px 14px;border-radius:999px;
      font-size:.8125rem;font-weight:700;letter-spacing:.04em;white-space:nowrap;}
    .verdict-pass{background:var(--pass-bg);color:var(--pass);border:1px solid var(--pass-border);}
    .verdict-warn{background:var(--warn-bg);color:var(--warn);border:1px solid #fcd34d;}
    .verdict-fail{background:var(--fail-bg);color:var(--fail);border:1px solid #fecaca;}
    .verdict-inconclusive{background:#f5f3ff;color:var(--inconclusive);border:1px solid #ddd6fe;}
    .verdict-review{background:#ecfeff;color:var(--review);border:1px solid #a5f3fc;}
    .summary-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:28px;}
    @media (max-width:960px){.summary-grid{grid-template-columns:repeat(2,1fr);}}
    @media (max-width:520px){.summary-grid{grid-template-columns:1fr;}}
    .summary-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:18px 20px;min-height:92px;}
    .summary-card-label{color:var(--muted);font-size:.8125rem;font-weight:500;margin-bottom:10px;}
    .summary-card-value{font-size:1rem;font-weight:600;word-break:break-all;line-height:1.35;}
    .summary-status{font-size:1.0625rem;font-weight:700;}
    .summary-status.verdict-pass{color:var(--pass);}
    .summary-status.verdict-warn{color:var(--warn);}
    .summary-status.verdict-fail{color:var(--fail);}
    .summary-status.verdict-inconclusive{color:var(--inconclusive);}
    .summary-status.verdict-review{color:var(--review);}
    .section{margin-bottom:28px;}
    .section-title{margin:0 0 12px;font-size:1.0625rem;font-weight:700;}
    .execution-card{background:var(--card);border:1px solid var(--border);border-radius:10px;
      padding:22px 24px;display:grid;grid-template-columns:repeat(4,1fr);gap:20px;}
    @media (max-width:900px){.execution-card{grid-template-columns:repeat(2,1fr);}}
    @media (max-width:520px){.execution-card{grid-template-columns:1fr;}}
    .exec-field-label{color:var(--muted);font-size:.8125rem;font-weight:500;margin-bottom:8px;}
    .exec-field-value{font-size:.9375rem;font-weight:600;word-break:break-word;}
    .table-card{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:auto;}
    table{width:100%;border-collapse:collapse;font-size:.875rem;}
    thead th{background:var(--header-bg);color:var(--muted);font-size:.6875rem;font-weight:600;
      letter-spacing:.06em;text-transform:uppercase;text-align:left;padding:12px 16px;border-bottom:1px solid var(--border);white-space:nowrap;}
    tbody td{padding:12px 16px;border-bottom:1px solid var(--border);vertical-align:top;}
    tbody tr:last-child td{border-bottom:none;}
    .num{font-variant-numeric:tabular-nums;}
    td.value-col,th.value-col{text-align:right;}
    .meta-foot{margin-top:10px;color:var(--muted);font-size:.8125rem;}
    .failures{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px 20px;margin-bottom:20px;}
    .failures h3{margin:0 0 10px;font-size:.9375rem;}
    .failures.fail h3{color:var(--fail);}
    .failures.warn h3{color:var(--warn);}
    .failures ul{margin:0;padding-left:20px;}
    footer{margin-top:36px;padding-top:16px;border-top:1px solid var(--border);color:var(--muted);font-size:.8125rem;}
    """


def _esc(value: object) -> str:
    return html.escape(str(value))


def _verdict_pill(verdict: str) -> str:
    cls = _VERDICT_CLASS.get(verdict, "verdict-inconclusive")
    label = _VERDICT_LABEL.get(verdict, verdict.upper())
    return f'<span class="verdict-pill {cls}">{_esc(label)}</span>'


def _fmt_num(value: object) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.4f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)
    return _esc(value)


def _duration(started: str | None, completed: str | None) -> str:
    if not started or not completed:
        return "in progress" if not completed else "—"
    try:
        a = datetime.fromisoformat(started.replace("Z", "+00:00"))
        b = datetime.fromisoformat(completed.replace("Z", "+00:00"))
    except ValueError:
        return "—"
    secs = (b - a).total_seconds()
    if secs < 0:
        return "—"
    if secs < 60:
        return f"{secs:.1f}s"
    m, s = divmod(int(secs), 60)
    return f"{m}m {s}s"


def _failure_block(title: str, items: list[str], css: str) -> str:
    if not items:
        return ""
    rows = "".join(f"<li>{_esc(i)}</li>" for i in items)
    return f'<div class="failures {css}"><h3>{_esc(title)}</h3><ul>{rows}</ul></div>'


def _metrics_table(metrics: dict) -> str:
    rows = "".join(
        f'<tr><td>{_esc(k)}</td><td class="num value-col">{_fmt_num(v)}</td></tr>'
        for k, v in sorted(metrics.items())
    )
    return (
        '<div class="table-card"><table><thead><tr><th>Metric</th>'
        '<th class="value-col">Value</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
    )


def _per_sample_table(per_sample: dict) -> str:
    if not per_sample:
        return '<div class="table-card"><table><tbody><tr><td class="meta-foot">No per-sample metrics</td></tr></tbody></table></div>'
    metric_keys = sorted({k for m in per_sample.values() for k in (m or {})})
    head = "".join(f'<th class="value-col">{_esc(k)}</th>' for k in metric_keys)
    body_rows = []
    for sample_id in sorted(per_sample):
        cells = "".join(
            f'<td class="num value-col">{_fmt_num(per_sample[sample_id].get(k))}</td>'
            if per_sample[sample_id].get(k) is not None
            else '<td class="num value-col">—</td>'
            for k in metric_keys
        )
        body_rows.append(f"<tr><td>{_esc(sample_id)}</td>{cells}</tr>")
    body = "".join(body_rows)
    return (
        f'<div class="table-card"><table><thead><tr><th>Sample</th>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def _render_html(report: dict) -> str:
    verdict = report["verdict"]
    cls = _VERDICT_CLASS.get(verdict, "verdict-inconclusive")
    title = f"Eval Report — {report['suite']}"
    hard = _failure_block("Hard Gate Failures", report.get("hard_gate_failures") or [], "fail")
    thr = _failure_block("Threshold Failures", report.get("threshold_failures") or [], "fail")
    warn = _failure_block("Warnings", report.get("warnings") or [], "warn")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{_esc(title)}</title>
  <style>{_styles()}</style>
</head>
<body>
  <div class="page">
    <header class="page-header">
      <div>
        <h1 class="page-title">{_esc(title)}</h1>
        <p class="page-subtitle">Generated {_esc(report.get("completed_at") or report.get("started_at") or "")}</p>
      </div>
      {_verdict_pill(verdict)}
    </header>

    <div class="summary-grid">
      <div class="summary-card">
        <div class="summary-card-label">Overall Status</div>
        <div class="summary-card-value summary-status {cls}">{_esc(_VERDICT_LABEL.get(verdict, verdict))}</div>
      </div>
      <div class="summary-card">
        <div class="summary-card-label">Run ID</div>
        <div class="summary-card-value">{_esc(report["run_id"])}</div>
      </div>
      <div class="summary-card">
        <div class="summary-card-label">Suite</div>
        <div class="summary-card-value">{_esc(report["suite"])}</div>
      </div>
      <div class="summary-card">
        <div class="summary-card-label">Samples</div>
        <div class="summary-card-value">{_esc(report.get("executed_samples", 0))} / {_esc(report.get("total_samples", 0))}
          <span class="meta-foot">({_esc(report.get("inconclusive_count", 0))} inconclusive)</span>
        </div>
      </div>
    </div>

    <section class="section">
      <h2 class="section-title">Execution</h2>
      <div class="execution-card">
        <div><div class="exec-field-label">Started</div><div class="exec-field-value">{_esc(report.get("started_at") or "—")}</div></div>
        <div><div class="exec-field-label">Completed</div><div class="exec-field-value">{_esc(report.get("completed_at") or "in progress")}</div></div>
        <div><div class="exec-field-label">Duration</div><div class="exec-field-value">{_esc(_duration(report.get("started_at"), report.get("completed_at")))}</div></div>
        <div><div class="exec-field-label">Target Model</div><div class="exec-field-value">{_esc(report.get("target_model") or "unknown")}</div></div>
      </div>
    </section>

    <section class="section">
      <h2 class="section-title">Metrics</h2>
      {_metrics_table(report.get("metrics") or {})}
      <p class="meta-foot">{_esc(report.get("sample_count", 0))} samples · {_esc(report.get("inconclusive_count", 0))} inconclusive</p>
    </section>

    <section class="section">
      <h2 class="section-title">Per-Sample Metrics</h2>
      {_per_sample_table(report.get("per_sample") or {})}
    </section>

    {hard}
    {thr}
    {warn}

    <footer>Assurance Agent · Eval Report</footer>
  </div>
</body>
</html>"""


def _render_md(report: dict) -> str:
    lines = [
        f"# eval report — {report['suite']}",
        "",
        f"- run_id: `{report['run_id']}`",
        f"- verdict: **{report['verdict']}**",
        "",
        "## Metrics",
        "",
    ]
    for name, value in sorted(report["metrics"].items()):
        lines.append(f"- `{name}`: {value}")
    return "\n".join(lines) + "\n"


def write_run_report(
    run_dir: Path, manifest: RunManifest, metrics: SuiteMetrics, gate: EvalGateResult
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    report = _build_report(manifest, metrics, gate)
    (run_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (run_dir / "report.html").write_text(_render_html(report), encoding="utf-8")
    (run_dir / "report.md").write_text(_render_md(report), encoding="utf-8")


def generate_trend_report(
    sut_root: Path,
    suite: str,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    html_out: Path | None = None,
) -> Path:
    points: list[dict] = []
    root = runs_dir(sut_root)
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
    out = html_out or (reports_dir(sut_root) / f"trend-{suite}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_render_trend_html(suite, points, date_from, date_to), encoding="utf-8")
    return out


def _render_trend_html(suite: str, points: list[dict], date_from: str | None, date_to: str | None) -> str:
    metric_keys = sorted({k for p in points for k in (p.get("metrics") or {})})
    head_metrics = "".join(f'<th class="value-col">{_esc(k)}</th>' for k in metric_keys)
    rows = []
    for p in points:
        metrics = p.get("metrics") or {}
        metric_cells = "".join(
            f'<td class="num value-col">{_fmt_num(metrics[k])}</td>'
            if k in metrics
            else '<td class="num value-col">—</td>'
            for k in metric_keys
        )
        rows.append(
            f"<tr><td>{_esc(p.get('run_id'))}</td>"
            f"<td>{_verdict_pill(p.get('verdict', 'inconclusive'))}</td>"
            f"<td>{_esc(p.get('started_at') or '—')}</td>"
            f"<td>{_esc(_duration(p.get('started_at'), p.get('completed_at')))}</td>"
            f"{metric_cells}</tr>"
        )
    col_span = 4 + len(metric_keys)
    body = "".join(rows) or (
        f'<tr><td colspan="{col_span}" class="meta-foot">No runs matched filters</td></tr>'
    )
    filter_note = " · ".join(
        n for n in [f"from {date_from}" if date_from else None, f"to {date_to}" if date_to else None] if n
    )
    subtitle = f"Suite: {_esc(suite)} · {len(points)} runs" + (
        f" · {_esc(filter_note)}" if filter_note else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Eval Trend — {_esc(suite)}</title>
  <style>{_styles()}</style>
</head>
<body>
  <div class="page">
    <header class="page-header">
      <div>
        <h1 class="page-title">Eval Trend Report</h1>
        <p class="page-subtitle">{subtitle}</p>
      </div>
    </header>
    <section class="section">
      <div class="table-card">
        <table>
          <thead><tr>
            <th>Run ID</th><th>Verdict</th><th>Started</th><th>Duration</th>{head_metrics}
          </tr></thead>
          <tbody>{body}</tbody>
        </table>
      </div>
    </section>
    <footer>Assurance Agent · Eval Trend</footer>
  </div>
</body>
</html>"""
