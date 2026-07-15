"""aa decide — thin Click adapter over record_decision."""

import os
from pathlib import Path

import click

from assurance_agent.change_location import ChangeNotFoundError, resolve_change
from assurance_agent.config import ConfigNotFoundError
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import UnsafeIdentifierError
from assurance_agent.workflow.core.exit_codes import EXIT_ERROR
from assurance_agent.workflow.core.progression import ProgressionError
from assurance_agent.workflow.orchestration.operations import HUMAN_DECISION_ACTIONS, record_decision


@click.command("decide")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--at", "checkpoint", required=True, help="Gate, phase, or supported workflow checkpoint.")
@click.option("--action", "action", required=True, help="Supported action for the checkpoint.")
@click.option("--reason", "reason", required=True, help="Human decision reason.")
@click.option(
    "--evidence", "evidence", default=None, help="Supporting evidence file within the project root."
)
def decide_command(change_id: str, checkpoint: str, action: str, reason: str, evidence: str | None) -> None:
    """Record a supported human workflow decision."""
    project_root = Path.cwd()
    try:
        loc = resolve_change(project_root, change_id)
    except (UnsafeIdentifierError, ChangeNotFoundError, ConfigNotFoundError) as err:
        click.secho(str(err), fg="red")
        raise SystemExit(1) from err
    change_dir = loc.path
    if action not in HUMAN_DECISION_ACTIONS:
        click.secho(f"decide failed: unsupported action '{action}'", fg="red")
        raise SystemExit(1)
    if not reason.strip():
        click.secho("decide failed: decision reason is required", fg="red")
        raise SystemExit(1)

    who = (os.environ.get("USER") or "unknown").strip() or "unknown"
    try:
        record_decision(
            project_root,
            change_dir,
            checkpoint=checkpoint,
            action=action,
            reason=reason,
            who=who,
            evidence=evidence,
        )
    except ProgressionError as err:
        click.secho(f"decide failed: {err}", fg="red")
        raise SystemExit(EXIT_ERROR) from err
    except AaError as err:
        click.secho(f"decide failed: {err}", fg="red")
        raise SystemExit(1) from err
    click.secho(f"aa decide — {checkpoint}", bold=True)
    click.secho(f"human_decision recorded: action={action}", fg="green")
