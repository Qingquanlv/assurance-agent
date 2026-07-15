"""aa state apply / aa state heal — thin Click adapters over orchestration operations."""

from pathlib import Path

import click

from assurance_agent.change_location import ChangeNotFoundError, resolve_change
from assurance_agent.config import ConfigNotFoundError
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import UnsafeIdentifierError
from assurance_agent.workflow.core.exit_codes import EXIT_ERROR
from assurance_agent.workflow.core.progression import ProgressionError
from assurance_agent.workflow.orchestration.operations import (
    HEAL_STATUSES,
    apply_phase_outcome,
    record_heal_transition,
)
from assurance_agent.workflow.orchestration.schema import load_workflow_schema


@click.group("state")
def state_group() -> None:
    """Workflow-state maintenance commands for the orchestrator."""


def _validated_change_dir(change_id: str) -> tuple[Path, Path]:
    project_root = Path.cwd()
    try:
        loc = resolve_change(project_root, change_id)
    except (UnsafeIdentifierError, ChangeNotFoundError, ConfigNotFoundError) as err:
        click.secho(str(err), fg="red")
        raise SystemExit(1) from err
    return loc.project_root, loc.path


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
def state_apply(
    change_id: str,
    phase_id: str,
    attempt_id: str | None,
    skill: str | None,
    skill_md_path: str | None,
) -> None:
    """Commit one completed phase outcome; gate routing happens in compute_status."""
    project_root, change_dir = _validated_change_dir(change_id)

    try:
        schema = load_workflow_schema(project_root)
        apply_phase_outcome(
            project_root,
            change_dir,
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


@state_group.command("heal")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--status", "status", required=True, help="Healing judgment.")
def state_heal(change_id: str, status: str) -> None:
    """Record an orchestrator healing judgment."""
    _project_root, change_dir = _validated_change_dir(change_id)
    if status not in HEAL_STATUSES:
        allowed = ", ".join(sorted(HEAL_STATUSES))
        click.secho(f'unsupported healing status "{status}". Expected one of: {allowed}', fg="red")
        raise SystemExit(1)

    try:
        result = record_heal_transition(change_dir, status)
    except ProgressionError as err:
        _cli_fail(err)
    except AaError as err:
        _cli_fail(err)
    else:
        click.secho(f"healing judgment recorded: {result.from_status} → {result.to_status}", fg="green")
