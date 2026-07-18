"""aa state apply / aa state configure / aa state heal — thin Click adapters over orchestration operations."""

import json
from pathlib import Path

import click

from assurance_agent.change_location import ChangeLocation, ChangeNotFoundError, resolve_change
from assurance_agent.config import ConfigNotFoundError
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import UnsafeIdentifierError
from assurance_agent.workflow.core.exit_codes import EXIT_ERROR
from assurance_agent.workflow.core.progression import ProgressionError
from assurance_agent.workflow.core.state import CONFIGURE_ORCHESTRATORS, configure_workflow_params
from assurance_agent.workflow.orchestration.operations import (
    ARCHIVE_STATUSES,
    HEAL_STATUSES,
    apply_phase_outcome,
    commit_archive_outcome,
    record_heal_transition,
)
from assurance_agent.workflow.orchestration.schema import load_workflow_schema


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


@state_group.command("apply")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--phase", "phase_id", required=True, help="Schema phase id to apply.")
@click.option("--attempt-id", "attempt_id", default=None, help="Dispatch attempt id from the driver.")
@click.option("--skill", "skill", default=None, help="Skill loaded for this phase (Skill Load Gate).")
@click.option("--skill-md-path", "skill_md_path", default=None, help="Path to the loaded SKILL.md.")
@click.option(
    "--status",
    "status",
    default=None,
    help=(
        "Archive-only: explicit archive status "
        f"({' | '.join(sorted(ARCHIVE_STATUSES))}). Routes the write through the "
        "guarded progression boundary (re-hashes _integrity, emits an event)."
    ),
)
def state_apply(
    change_id: str,
    phase_id: str,
    attempt_id: str | None,
    skill: str | None,
    skill_md_path: str | None,
    status: str | None,
) -> None:
    """Commit one completed phase outcome; gate routing happens in compute_status."""
    loc = _validated_change(change_id)

    if status is not None and phase_id != "archive":
        click.secho("--status is only supported for --phase archive", fg="red")
        raise SystemExit(1)

    try:
        schema = load_workflow_schema(loc.project_root)
        if phase_id == "archive" and status is not None:
            commit_archive_outcome(
                loc,
                schema,
                status=status,
                skill=skill,
                skill_md_path=skill_md_path,
            )
        else:
            apply_phase_outcome(
                loc,
                schema,
                phase_id,
                attempt_id=attempt_id,
                skill=skill,
                skill_md_path=skill_md_path,
            )
    except ProgressionError as err:
        _cli_fail(err)
    except AaError as err:
        _cli_fail(err)
    click.secho(f'workflow-state.yaml updated for phase "{phase_id}"', fg="green")


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
    """Merge runtime params into workflow-state.yaml and stamp run_context."""
    loc = _validated_change(change_id)

    if orchestrator not in CONFIGURE_ORCHESTRATORS:
        expected = ", ".join(sorted(CONFIGURE_ORCHESTRATORS))
        click.secho(f'unsupported orchestrator "{orchestrator}". Expected {expected}', fg="red")
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


@state_group.command("heal")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--status", "status", required=True, help="Healing judgment.")
def state_heal(change_id: str, status: str) -> None:
    """Record an orchestrator healing judgment."""
    loc = _validated_change(change_id)
    if status not in HEAL_STATUSES:
        allowed = ", ".join(sorted(HEAL_STATUSES))
        click.secho(f'unsupported healing status "{status}". Expected one of: {allowed}', fg="red")
        raise SystemExit(1)

    try:
        result = record_heal_transition(loc.path, status)
    except ProgressionError as err:
        _cli_fail(err)
    except AaError as err:
        _cli_fail(err)
    else:
        click.secho(f"healing judgment recorded: {result.from_status} → {result.to_status}", fg="green")
