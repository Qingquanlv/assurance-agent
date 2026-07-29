"""aa trace — on-demand execution-phase trace projection (read-only fold).

Completely separate from ``aa status`` (graph ledger projection): this command
calls ``fold_trace`` with ``phase=execution`` and ``current=None``.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from assurance_agent.artifacts.models.trace import TraceProjection, TraceRow
from assurance_agent.change_location import ChangeNotFoundError, resolve_change
from assurance_agent.config import ConfigInvalidError, ConfigNotFoundError
from assurance_agent.evidence.trace import fold_trace
from assurance_agent.identifiers import UnsafeIdentifierError
from assurance_agent.workflow.core.exit_codes import EXIT_ERROR


def trace_error_no_cases(change_id: str) -> str:
    return f"trace failed: no cases found for change '{change_id}'"


def trace_error_all_unmapped(change_id: str) -> str:
    return f"trace failed: all tests unmapped for change '{change_id}'"


def validate_trace_projection(projection: TraceProjection) -> str | None:
    if projection.rows:
        return None
    if projection.unmapped_tests:
        return trace_error_all_unmapped(projection.change_id)
    return trace_error_no_cases(projection.change_id)


def filter_trace_rows(
    rows: tuple[TraceRow, ...],
    case_types: tuple[str, ...],
) -> tuple[TraceRow, ...]:
    if not case_types:
        return rows
    allowed = set(case_types)
    return tuple(row for row in rows if row.case_type in allowed)


def gaps_payload(projection: TraceProjection) -> dict[str, object]:
    return {
        "change_id": projection.change_id,
        "gaps": [gap.model_dump(mode="json") for gap in projection.gaps],
    }


def _print_human(
    change_id: str,
    projection: TraceProjection,
    *,
    only_gaps: bool,
    case_types: tuple[str, ...],
) -> None:
    click.secho(f"aa trace — change: {change_id}", bold=True)
    click.echo()
    if only_gaps:
        if not projection.gaps:
            click.echo("  gaps: (none)")
        for gap in projection.gaps:
            click.echo(f"  {gap.code:<28} {gap.source}")
            if gap.detail:
                click.echo(f"    detail: {gap.detail}")
        click.echo()
        return

    rows = filter_trace_rows(projection.rows, case_types)
    click.echo(f"  phase      : {projection.phase}")
    click.echo(f"  batch      : {projection.authoritative_batch_id or '(none)'}")
    click.echo(f"  integrity  : {projection.integrity}")
    click.echo(f"  rows       : {len(rows)}")
    click.echo(f"  gaps       : {len(projection.gaps)}")
    click.echo(f"  unmapped   : {len(projection.unmapped_tests)}")
    for row in rows:
        latest = row.latest_execution.status if row.latest_execution else "-"
        click.echo(f"    {row.case_id:<24} {row.case_type:<12} {row.coverage_state:<12} latest={latest}")
    if projection.gaps:
        click.echo()
        click.echo("  gaps:")
        for gap in projection.gaps:
            click.echo(f"    {gap.code:<26} {gap.source}")
    click.echo()


def run_trace(
    change_id: str,
    *,
    as_json: bool,
    case_types: tuple[str, ...],
    only_gaps: bool,
) -> int:
    project_root = Path.cwd()
    try:
        resolve_change(project_root, change_id)
    except (UnsafeIdentifierError, ChangeNotFoundError, ConfigNotFoundError, ConfigInvalidError) as err:
        click.secho(str(err), fg="red", err=True)
        return EXIT_ERROR

    projection = fold_trace(project_root, change_id, phase="execution", current=None)
    error = validate_trace_projection(projection)
    if error:
        click.secho(error, fg="red", err=True)
        return EXIT_ERROR

    if only_gaps:
        payload: TraceProjection | dict[str, object] = gaps_payload(projection)
    elif case_types:
        payload = projection.model_copy(update={"rows": filter_trace_rows(projection.rows, case_types)})
    else:
        payload = projection

    if as_json:
        if isinstance(payload, dict):
            click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            click.echo(payload.model_dump_json(indent=2))
    else:
        _print_human(change_id, projection, only_gaps=only_gaps, case_types=case_types)
    return 0


@click.command("trace")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
@click.option(
    "--type",
    "case_types",
    multiple=True,
    type=click.Choice(["API", "E2E", "Fuzz", "Performance"], case_sensitive=True),
    help="Filter rows by case type (repeatable).",
)
@click.option("--only-gaps", is_flag=True, help="Output only projection gaps.")
def trace_command(
    change_id: str,
    as_json: bool,
    case_types: tuple[str, ...],
    only_gaps: bool,
) -> None:
    """Fold execution-phase trace projection on demand (read-only)."""
    raise SystemExit(
        run_trace(
            change_id,
            as_json=as_json,
            case_types=case_types,
            only_gaps=only_gaps,
        )
    )
