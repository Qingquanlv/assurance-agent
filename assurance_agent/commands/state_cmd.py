"""aa state configure — pre-run params convenience (params freeze after graph start)."""

import json
from pathlib import Path

import click

from assurance_agent.change_location import ChangeLocation, ChangeNotFoundError, resolve_change
from assurance_agent.config import ConfigNotFoundError
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import UnsafeIdentifierError
from assurance_agent.workflow.core.events import Ledger
from assurance_agent.workflow.core.exit_codes import EXIT_ERROR
from assurance_agent.workflow.core.progression import ProgressionError
from assurance_agent.workflow.core.state import CONFIGURE_ORCHESTRATORS, configure_workflow_params


@click.group("state")
def state_group() -> None:
    """Workflow-state maintenance commands for the orchestrator."""


def _validated_change(change_id: str) -> ChangeLocation:
    project_root = Path.cwd()
    try:
        loc = resolve_change(project_root, change_id)
    except (UnsafeIdentifierError, ChangeNotFoundError, ConfigNotFoundError) as err:
        click.secho(str(err), fg="red")
        raise SystemExit(1) from err
    return loc


def _cli_fail(err: Exception) -> None:
    click.secho(str(err), fg="red")
    if isinstance(err, ProgressionError):
        raise SystemExit(EXIT_ERROR) from err
    raise SystemExit(1) from err


@state_group.command("configure")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option(
    "--params-json", "params_json", default="{}", show_default=True, help="JSON object of params to merge."
)
@click.option(
    "--orchestrator",
    "orchestrator",
    required=True,
    help="Logical orchestrator: aa-intake, aa-execute, or aa-workflow.",
)
def state_configure(change_id: str, params_json: str, orchestrator: str) -> None:
    """Merge runtime params into workflow-state.yaml and stamp run_context.

    Refuses once ``graph_invocation_started`` exists because invocation params
    are frozen for the GraphRuntime run.
    """
    loc = _validated_change(change_id)

    if orchestrator not in CONFIGURE_ORCHESTRATORS:
        expected = ", ".join(sorted(CONFIGURE_ORCHESTRATORS))
        click.secho(f'unsupported orchestrator "{orchestrator}". Expected {expected}', fg="red")
        raise SystemExit(1)

    if Ledger(loc.path).latest(type="graph_invocation_started") is not None:
        click.secho(
            "params are frozen after graph_invocation_started; refuse configure",
            fg="red",
        )
        raise SystemExit(1)

    try:
        params = json.loads(params_json)
        if not isinstance(params, dict):
            raise ValueError("params-json must be a JSON object")
    except (json.JSONDecodeError, ValueError) as err:
        click.secho(f"Invalid --params-json: {err}", fg="red")
        raise SystemExit(1) from err

    try:
        configure_workflow_params(loc.path, params, orchestrator)
    except AaError as err:
        _cli_fail(err)
    click.secho(f'params merged and run_context stamped for "{orchestrator}"', fg="green")
