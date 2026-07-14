"""`aa doctor` command: check environment and configuration health."""

from pathlib import Path

import click

from assurance_agent.workflow.core.checks import CheckResult, DoctorResult, run_doctor_checks

_GROUP_LABELS = {
    "config": "Config",
    "sources": "Sources",
    "directories": "Directories",
    "frameworks": "Frameworks",
}
_ICONS = {"ok": ("✓", "green"), "warning": ("!", "yellow"), "error": ("✗", "red")}


@click.command("doctor")
@click.option("--json", "as_json", is_flag=True, help="Output machine-readable JSON.")
def doctor_command(as_json: bool) -> None:
    """Check assurance-agent environment and configuration."""
    result = run_doctor_checks(Path.cwd())
    if as_json:
        click.echo(result.model_dump_json(indent=2))
    else:
        _print_result(result)
    raise SystemExit(1 if result.status == "error" else 0)


def _print_result(result: DoctorResult) -> None:
    click.secho("\naa doctor\n", bold=True)
    seen_groups: list[str] = []
    for check in result.checks:
        if check.group not in seen_groups:
            seen_groups.append(check.group)
            click.secho(_GROUP_LABELS.get(check.group, check.group), bold=True)
        _print_check(check)
    color = {"ok": "green", "warning": "yellow", "error": "red"}[result.status]
    click.echo("Result: " + click.style(result.status.upper(), fg=color, bold=True))


def _print_check(check: CheckResult) -> None:
    icon, color = _ICONS[check.status]
    click.echo(f"{click.style(icon, fg=color)} {check.message}")
    if check.suggested_fix and check.status != "ok":
        click.secho(f"  -> {check.suggested_fix}", dim=True)
