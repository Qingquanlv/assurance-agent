"""aa decide — non-graph policy decisions only.

Graph gate interrupt actions must go through ``aa workflow resume``.
"""

from __future__ import annotations

import os
from pathlib import Path

import click

from assurance_agent.change_location import ChangeNotFoundError, resolve_change
from assurance_agent.config import ConfigNotFoundError
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import UnsafeIdentifierError
from assurance_agent.workflow.core.exit_codes import EXIT_ERROR
from assurance_agent.workflow.core.progression import ProgressionError
from assurance_agent.workflow.orchestration.operations import record_decision

_GRAPH_GATE_ACTIONS = frozenset({"fix_and_proceed", "accept_risk", "stop"})
_POLICY_ACTIONS = frozenset({"allow_test_changes", "skip_branch"})


@click.command("decide")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--at", "checkpoint", required=True, help="Supported non-graph policy checkpoint.")
@click.option("--action", "action", required=True, help="Supported policy action.")
@click.option("--reason", "reason", required=True, help="Human decision reason.")
@click.option(
    "--evidence", "evidence", default=None, help="Supporting evidence file within the project root."
)
def decide_command(change_id: str, checkpoint: str, action: str, reason: str, evidence: str | None) -> None:
    """Record a supported non-graph policy decision."""
    if action in _GRAPH_GATE_ACTIONS:
        click.secho(
            f"decide failed: graph gate action '{action}' is no longer supported here; "
            "use `aa workflow resume --change <id> --interrupt <id> --action "
            f"{action} --reason <reason>`",
            fg="red",
        )
        raise SystemExit(1)
    if action not in _POLICY_ACTIONS:
        click.secho(f"decide failed: unsupported action '{action}'", fg="red")
        raise SystemExit(1)

    project_root = Path.cwd()
    try:
        loc = resolve_change(project_root, change_id)
    except (UnsafeIdentifierError, ChangeNotFoundError, ConfigNotFoundError) as err:
        click.secho(str(err), fg="red")
        raise SystemExit(1) from err
    if not reason.strip():
        click.secho("decide failed: decision reason is required", fg="red")
        raise SystemExit(1)

    who = (os.environ.get("USER") or "unknown").strip() or "unknown"
    try:
        record_decision(
            loc,
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
