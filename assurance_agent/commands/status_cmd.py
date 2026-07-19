"""aa status — project GraphStatus from the ledger (no v1 compute_status)."""

from __future__ import annotations

import json
from pathlib import Path

import click

from assurance_agent.change_location import ChangeNotFoundError, resolve_change
from assurance_agent.config import ConfigNotFoundError
from assurance_agent.identifiers import UnsafeIdentifierError
from assurance_agent.workflow.core.exit_codes import (
    EXIT_COMPLETED,
    EXIT_ERROR,
    EXIT_HUMAN_REVIEW,
    EXIT_STOPPED,
)
from assurance_agent.workflow.driver.headless_adapter import HeadlessAdapter
from assurance_agent.workflow.driver.runtime_factory import build_graph_runtime
from assurance_agent.workflow.graph.models import GraphStatus
from assurance_agent.workflow.graph.runtime import GraphRuntimeError


def _exit_for_graph_status(status: GraphStatus) -> int:
    if status.status == "completed":
        return EXIT_COMPLETED
    if status.status == "stopped":
        return EXIT_STOPPED
    if status.status == "interrupted":
        return EXIT_HUMAN_REVIEW
    if status.status == "failed":
        return EXIT_ERROR
    return EXIT_COMPLETED


@click.command("status")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--next", "next_only", is_flag=True, help="Print only pending work.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
def status_command(change_id: str, next_only: bool, as_json: bool) -> None:
    """Print GraphStatus for the latest root invocation."""
    project_root = Path.cwd()
    try:
        resolve_change(project_root, change_id)
    except (UnsafeIdentifierError, ChangeNotFoundError, ConfigNotFoundError) as err:
        click.secho(str(err), fg="red")
        raise SystemExit(1) from err

    adapter = HeadlessAdapter(agent_cmd="true", cwd=project_root)
    try:
        bundle = build_graph_runtime(
            project_root=project_root,
            change_id=change_id,
            adapter=adapter,
        )
        latest = bundle.runtime.latest_root_invocation()
        if latest is None:
            if as_json:
                click.echo(json.dumps({"status": None}, indent=2, ensure_ascii=False))
            else:
                click.echo("no graph invocation found (workflow not started)")
            raise SystemExit(EXIT_COMPLETED)
        status = bundle.runtime.status(latest)
    except GraphRuntimeError as err:
        click.secho(f"status failed: {err}", fg="red")
        raise SystemExit(1) from err

    if next_only:
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "pending_tasks": list(status.pending_tasks),
                        "pending_interrupts": [i.model_dump(mode="json") for i in status.pending_interrupts],
                        "status": status.status,
                        "terminal_reason": status.terminal_reason,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            if status.pending_tasks:
                for task_id in status.pending_tasks:
                    click.echo(task_id)
            elif status.pending_interrupts:
                for interrupt in status.pending_interrupts:
                    click.echo(interrupt.interrupt_id)
            else:
                click.echo("(none)")
        raise SystemExit(_exit_for_graph_status(status))

    if as_json:
        click.echo(json.dumps(status.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        click.secho(f"aa status — change: {change_id}", bold=True)
        click.echo()
        click.echo(f"  invocation : {status.invocation_id}")
        click.echo(f"  entrypoint : {status.entrypoint}")
        click.echo(f"  status     : {status.status}")
        click.echo(f"  checkpoint : {status.checkpoint_id}")
        click.echo(f"  event_seq  : {status.event_seq}")
        pending = ", ".join(status.pending_tasks) or "(none)"
        click.echo(f"  pending    : {pending}")
        if status.pending_interrupts:
            click.echo("  interrupts :")
            for interrupt in status.pending_interrupts:
                click.echo(
                    f"    {interrupt.interrupt_id} @ {interrupt.node_id} actions={list(interrupt.actions)}"
                )
        if status.terminal_reason:
            color = "green" if status.status == "completed" else "red"
            click.echo(
                "  terminal   : " + click.style(f"{status.status} — {status.terminal_reason}", fg=color)
            )
        click.echo()

    raise SystemExit(_exit_for_graph_status(status))
