from pathlib import Path

import click

from assurance_agent.artifacts.validate import (
    UnknownPhaseError,
    ValidationReport,
    validate_change,
)
from assurance_agent.change_location import resolve_change
from assurance_agent.exceptions import AaError


@click.command("validate")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--phase", default=None, help="Only validate artifacts the phase produces.")
@click.option("--artifact", default=None, help="Validate a single change-relative artifact path.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output {ok, results}.")
def validate_command(change_id: str, phase: str | None, artifact: str | None, as_json: bool) -> None:
    """Validate per-change YAML/JSON artifacts against their schemas (deterministic, no LLM)."""
    try:
        loc = resolve_change(Path.cwd(), change_id)
        report = validate_change(loc.path, phase=phase, artifact=artifact)
    except UnknownPhaseError as err:
        raise click.UsageError(str(err)) from err  # click exits 2
    except AaError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(1) from err

    if as_json:
        click.echo(report.model_dump_json(indent=2))
    else:
        _print_report(change_id, report)
    raise SystemExit(0 if report.ok else 1)


def _print_report(change_id: str, report: ValidationReport) -> None:
    click.secho(f"aa validate — {change_id}", bold=True)
    click.echo()
    for result in report.results:
        if result.ok:
            click.secho(f"✓ {result.path} [{result.artifact_type}]", fg="green")
        else:
            click.secho(f"✗ {result.path} [{result.artifact_type}]", fg="red")
            for error in result.errors:
                click.echo(f"    · {error}")
    click.echo()
