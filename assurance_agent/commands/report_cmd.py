"""`aa report inspect|generate` commands."""

from datetime import datetime, timezone
from pathlib import Path

import click

from assurance_agent.artifacts.models import FailureAnalysis, Reclassified
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.events import append_event_best_effort
from assurance_agent.workflow.execution.evidence import EvidenceError
from assurance_agent.workflow.report.inspector import inspect_change
from assurance_agent.workflow.report.report_builder import generate_report

_GATE_COLOR = {"PASS": "green", "PASS_WITH_WARNINGS": "yellow", "FAIL": "red", "SKIPPED": "yellow"}


@click.group("report")
def report_group() -> None:
    """Inspect execution results and generate quality reports."""


@report_group.command("inspect")
@click.option("--change", "change_id", required=True, help="Change ID.")
def inspect_cmd(change_id: str) -> None:
    """Classify failures → inspect/failure-analysis.json + quality-gate-result.json."""
    try:
        result = inspect_change(Path.cwd(), change_id)
    except (EvidenceError, AaError) as err:
        click.secho(f"Inspect failed: {err}", fg="red")
        raise SystemExit(1) from err

    analysis = result.analysis
    gate = result.quality_gate
    click.secho(f"\naa report inspect — change: {change_id}\n", bold=True)
    click.echo(
        "  Final Status : " + click.style(gate.final_status, fg=_GATE_COLOR[gate.final_status], bold=True)
    )
    click.echo(f"  Batch ID     : {analysis.batch_id or '(unknown)'}")
    click.echo(
        f"  Failures     : {len(analysis.failures)} "
        f"(hard={len(analysis.hard_fails)}, review={len(analysis.needs_review)})"
    )
    for failure in analysis.failures:
        flag = (
            "fix-allowed"
            if failure.fix_proposal_eligible
            else ("review" if failure.needs_review else "no-fix")
        )
        click.echo(f"    {failure.category:<28} {failure.case_id}  [{flag}]")
    click.echo(f"  failure-analysis.json    → {result.analysis_path}")
    click.echo(f"  quality-gate-result.json → {result.quality_gate_path}")
    raise SystemExit(1 if gate.final_status == "FAIL" else 0)


@report_group.command("generate")
@click.option("--change", "change_id", required=True, help="Change ID.")
def generate_cmd(change_id: str) -> None:
    """Quality Score → report/ trio (quality-report.json/.md + executive-summary.md)."""
    try:
        result = generate_report(Path.cwd(), change_id)
    except (EvidenceError, FileNotFoundError, AaError) as err:
        click.secho(f"Report generation failed: {err}", fg="red")
        raise SystemExit(1) from err

    report = result.report
    click.secho(f"\naa report generate — change: {change_id}\n", bold=True)
    click.echo(
        "  Final Status  : "
        + click.style(report.final_status, fg=_GATE_COLOR[report.final_status], bold=True)
    )
    click.echo(f"  Quality Score : {report.quality_score} / 100")
    click.echo(f"  Risk Level    : {report.risk_level}")
    click.echo(f"  Recommendation: {report.recommendation}")
    click.echo(f"  quality-report.json  → {result.json_path}")
    click.echo(f"  quality-report.md    → {result.md_path}")
    click.echo(f"  executive-summary.md → {result.exec_summary_path}")
    raise SystemExit(0)


def _failure_key(failure) -> str:  # noqa: ANN001
    return failure.id or f"{failure.target}:{failure.case_id}"


def _load_prior_analysis(change_dir: Path, source_batch_id: str) -> FailureAnalysis | None:
    path = change_dir / "inspect" / "failure-analysis.json"
    if not path.is_file():
        return None
    try:
        prior = FailureAnalysis.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return prior if prior.source_batch_id == source_batch_id else None


@report_group.command("reclassify")
@click.option("--change", "change_id", required=True, help="Change ID.")
@click.option("--batch", "batch_id", default=None, help="Execution batch to reclassify.")
def reclassify_cmd(change_id: str, batch_id: str | None) -> None:
    """Re-run deterministic failure classification for a batch without executing tests."""
    change_dir = Path.cwd() / "qa" / "changes" / change_id
    prior = _load_prior_analysis(change_dir, batch_id) if batch_id else None
    try:
        result = inspect_change(Path.cwd(), change_id, batch_id=batch_id)
    except (EvidenceError, AaError) as err:
        click.secho(f"Reclassify failed: {err}", fg="red")
        raise SystemExit(1) from err

    if prior is not None:
        old_by_key = {_failure_key(f): f for f in prior.failures}
        now = datetime.now(timezone.utc).isoformat()
        for failure in result.analysis.failures:
            old = old_by_key.get(_failure_key(failure))
            if old is None or old.category == failure.category:
                continue
            failure.reclassified = Reclassified.model_validate(
                {
                    "from": old.category,
                    "evidence": "deterministic rules re-run",
                    "at": now,
                }
            )
        inspect_dir = change_dir / "inspect"
        (inspect_dir / "failure-analysis.json").write_text(
            result.analysis.model_dump_json(indent=2, by_alias=True),
            encoding="utf-8",
        )

    analysis = result.analysis
    gate = result.quality_gate
    click.secho(f"\naa report reclassify — change: {change_id}\n", bold=True)
    click.echo(
        "  Final Status : " + click.style(gate.final_status, fg=_GATE_COLOR[gate.final_status], bold=True)
    )
    click.echo(f"  Source Batch : {analysis.source_batch_id}")
    click.echo(f"  Failures     : {len(analysis.failures)}")
    append_event_best_effort(
        change_dir,
        {
            "source": "report",
            "type": "reclassified",
            "batch_id": analysis.source_batch_id,
        },
    )
    raise SystemExit(1 if gate.final_status == "FAIL" else 0)
