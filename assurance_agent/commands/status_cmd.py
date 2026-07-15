"""aa status — 计算工作流图中每个相位的状态（确定性, 无 LLM）。

对齐 TS src/commands/status.ts 的 flag 面与退出码语义：
查询类命令写 best-effort 遥测事件（失败静默，退出码不变）。
"""

from pathlib import Path

import click

from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import UnsafeIdentifierError, assert_change_id_safe
from assurance_agent.workflow.core.events import append_event_best_effort
from assurance_agent.workflow.core.exit_codes import exit_code_for_terminal
from assurance_agent.workflow.core.state import read_state
from assurance_agent.workflow.orchestration.engine import WorkflowStatus, compute_status
from assurance_agent.workflow.orchestration.schema import load_workflow_schema


@click.command("status")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--next", "next_only", is_flag=True, help="Print only the next dispatch batch.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
def status_command(change_id: str, next_only: bool, as_json: bool) -> None:
    """Compute the state of every phase in the workflow graph (deterministic, no LLM)."""
    project_root = Path.cwd()
    try:
        assert_change_id_safe(change_id)
    except UnsafeIdentifierError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(1) from err
    change_dir = project_root / "qa" / "changes" / change_id
    if not change_dir.is_dir():
        click.secho(f"change '{change_id}' not found (expected: {change_dir}).", fg="red")
        raise SystemExit(1)

    try:
        schema = load_workflow_schema(project_root)
        state = read_state(change_dir)
        params = getattr(state, "params", None) or {}
        status = compute_status(schema, change_dir, state, params)
    except AaError as err:
        click.secho(f"status failed: {err}", fg="red")
        raise SystemExit(1) from err

    _emit_telemetry(change_dir, status)

    if next_only:
        _print_next(status, as_json)
    elif as_json:
        click.echo(status.model_dump_json(indent=2))
    else:
        _print_table(change_id, status)

    raise SystemExit(exit_code_for_terminal(status.terminal))


def _emit_telemetry(change_dir: Path, status: WorkflowStatus) -> None:
    append_event_best_effort(
        change_dir,
        {
            "source": "status",
            "type": "status_query",
            "terminal": status.terminal.kind if status.terminal else None,
            "next": [d.phase_id for d in status.next_dispatch],
        },
    )


def _print_next(status: WorkflowStatus, as_json: bool) -> None:
    if as_json:
        click.echo(
            _json_dumps(
                {
                    "next_dispatch": [d.model_dump() for d in status.next_dispatch],
                    "terminal": status.terminal.model_dump() if status.terminal else None,
                }
            )
        )
        return
    if status.next_dispatch:
        for entry in status.next_dispatch:
            click.echo(entry.phase_id)
    else:
        click.echo("(none)")


def _print_table(change_id: str, status: WorkflowStatus) -> None:
    click.secho(f"aa status — change: {change_id}", bold=True)
    click.echo()
    for phase in status.phases:
        click.echo(f"  {phase.status.ljust(14)} {phase.id}")
    click.echo()
    next_ids = ", ".join(d.phase_id for d in status.next_dispatch) or "(none)"
    click.echo(f"  Next     : {next_ids}")
    if status.terminal:
        color = "green" if status.terminal.kind == "completed" else "red"
        reason = status.terminal.reason or ""
        click.echo("  Terminal : " + click.style(f"{status.terminal.kind} — {reason}", fg=color))
    click.echo()


def _json_dumps(obj: object) -> str:
    import json

    return json.dumps(obj, indent=2, ensure_ascii=False)
