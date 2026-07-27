"""aa status — 从 ledger 投影 GraphStatus（纯只读：不构建 runtime、不编译 schema、零写入）。"""

from __future__ import annotations

import json
from pathlib import Path

import click

from assurance_agent.change_location import ChangeNotFoundError, resolve_change
from assurance_agent.config import ConfigInvalidError, ConfigNotFoundError
from assurance_agent.identifiers import UnsafeIdentifierError
from assurance_agent.workflow.core.events import LedgerIntegrityError
from assurance_agent.workflow.core.exit_codes import (
    EXIT_COMPLETED,
    EXIT_ERROR,
    EXIT_HUMAN_REVIEW,
    EXIT_STOPPED,
)
from assurance_agent.workflow.graph.models import GraphStatus
from assurance_agent.workflow.graph.status import read_latest_graph_status


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


def _print_guidance(change_id: str, status: GraphStatus) -> None:
    if status.status == "interrupted":
        for interrupt in status.pending_interrupts:
            click.echo(
                f"resume: aa workflow resume --change {change_id} "
                f"--interrupt {interrupt.interrupt_id} --action <{'|'.join(interrupt.actions)}> --reason <text>"
            )
    elif status.status in {"failed", "stopped"}:
        # terminal line already printed in _print_human; guidance is next-action only
        click.echo("invocation 已终局，resume 不会推进；请诊断后新开 change/invocation")
    else:
        click.echo(
            f"resume: re-run `aa workflow run --change {change_id} --entrypoint {status.entrypoint}` "
            "to advance from the ledger"
        )


def _print_next(status: GraphStatus, as_json: bool) -> None:
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
        return
    if status.pending_tasks:
        for task_id in status.pending_tasks:
            click.echo(task_id)
    elif status.pending_interrupts:
        for interrupt in status.pending_interrupts:
            click.echo(interrupt.interrupt_id)
    else:
        click.echo("(none)")


def _print_human(change_id: str, status: GraphStatus) -> None:
    click.secho(f"aa status — change: {change_id}", bold=True)
    click.echo()
    click.echo(f"  invocation : {status.invocation_id}")
    click.echo(f"  entrypoint : {status.entrypoint}")
    click.echo(f"  status     : {status.status}")
    click.echo(f"  checkpoint : {status.checkpoint_id}")
    click.echo(f"  event_seq  : {status.event_seq}")
    click.echo(f"  pending    : {', '.join(status.pending_tasks) or '(none)'}")
    for interrupt in status.pending_interrupts:
        click.echo(
            f"  interrupt  : {interrupt.interrupt_id} @ {interrupt.node_id} actions={list(interrupt.actions)}"
        )
    if status.terminal_reason and status.status in {"completed", "failed", "stopped"}:
        color = "green" if status.status == "completed" else "red"
        click.echo("  terminal   : " + click.style(f"{status.status} — {status.terminal_reason}", fg=color))
    if status.status != "completed":
        _print_guidance(change_id, status)
    click.echo()


def _run_status(change_id: str, next_only: bool, as_json: bool) -> int:
    project_root = Path.cwd()
    try:
        loc = resolve_change(project_root, change_id)
    except (UnsafeIdentifierError, ChangeNotFoundError, ConfigNotFoundError, ConfigInvalidError) as err:
        click.secho(str(err), fg="red", err=True)
        return EXIT_ERROR
    try:
        status = read_latest_graph_status(loc.path)
    except (LedgerIntegrityError, OSError) as err:
        click.secho(f"status failed: {err}", fg="red", err=True)
        return EXIT_ERROR
    if status is None:
        if as_json:
            click.echo(json.dumps({"status": None, "invocation_id": None}, indent=2, ensure_ascii=False))
        else:
            click.echo("no graph invocation found (workflow not started)")
        return EXIT_COMPLETED
    if next_only:
        _print_next(status, as_json)
    elif as_json:
        click.echo(status.model_dump_json(indent=2))
    else:
        _print_human(change_id, status)
    return _exit_for_graph_status(status)


@click.command("status")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--next", "next_only", is_flag=True, help="Print only pending work.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
def status_command(change_id: str, next_only: bool, as_json: bool) -> None:
    """Print GraphStatus for the latest root invocation (pure read-only)."""
    raise SystemExit(_run_status(change_id, next_only, as_json))
