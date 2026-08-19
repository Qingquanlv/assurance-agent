from pathlib import Path

import click

from assurance_agent.artifacts.validate import (
    UnknownPhaseError,
    ValidationReport,
    validate_change,
)
from assurance_agent.change_location import resolve_change
from assurance_agent.commands.overlay_cli import overlay_options, resolve_cli_overlays
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.graph.schema_v2 import WorkflowSchemaV2, load_workflow_v2_with_origin


class _LoadedSchemaProduces:
    def __init__(self, schema: WorkflowSchemaV2) -> None:
        self._schema = schema

    def phase_produces(self, phase_id: str) -> list[str] | None:
        for graph in self._schema.graphs.values():
            node = graph.nodes.get(phase_id)
            if node is None:
                continue
            return [_strip_locator(output) for output in node.outputs]
        return None


def _strip_locator(path: str) -> str:
    for prefix in ("change:", "repo:", "qa:", "project:"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


@click.command("validate")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--phase", default=None, help="Only validate artifacts the phase produces.")
@click.option("--artifact", default=None, help="Validate a single change-relative artifact path.")
@overlay_options
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output {ok, results}.")
def validate_command(
    change_id: str,
    phase: str | None,
    artifact: str | None,
    explicit_schema: Path | None,
    explicit_contracts: Path | None,
    as_json: bool,
) -> None:
    """Validate per-change YAML/JSON artifacts against their schemas (deterministic, no LLM)."""
    project_root = Path.cwd()
    schema_path, _contracts_path = resolve_cli_overlays(project_root, explicit_schema, explicit_contracts)
    try:
        loaded = load_workflow_v2_with_origin(project_root, schema_path)
        loc = resolve_change(project_root, change_id)
        report = validate_change(
            loc.path,
            phase=phase,
            artifact=artifact,
            schema=_LoadedSchemaProduces(loaded.schema),
        )
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
