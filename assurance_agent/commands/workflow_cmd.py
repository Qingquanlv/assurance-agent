"""`aa workflow run|status` — the deterministic driver command surface."""

import json
from pathlib import Path

import click

from assurance_agent.change_location import ChangeNotFoundError, resolve_change
from assurance_agent.identifiers import UnsafeIdentifierError
from assurance_agent.workflow.driver.adapter import DriverError
from assurance_agent.workflow.driver.driver_state import read_driver_state
from assurance_agent.workflow.driver.headless_adapter import HeadlessAdapter
from assurance_agent.workflow.driver.loop import (
    EXIT_COMPLETED,
    EXIT_ERROR,
    EXIT_HUMAN_REVIEW,
    EXIT_STOPPED,
    run_workflow_loop,
)
from assurance_agent.workflow.driver.opencode_adapter import OpenCodeAdapter, auth_headers_from_env
from assurance_agent.workflow.driver.workflow_start import start_workflow_detached


@click.group("workflow")
def workflow_group() -> None:
    """Deterministic workflow driver (run / status)."""


def _parse_params(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as err:
        click.secho(f"Invalid --params JSON: {err}", fg="red")
        raise SystemExit(EXIT_ERROR) from err
    if not isinstance(parsed, dict):
        click.secho("Invalid --params JSON: expected an object", fg="red")
        raise SystemExit(EXIT_ERROR)
    return parsed


def _build_adapter(
    adapter_name: str,
    project_root: Path,
    server: str | None,
    directory: str | None,
    model: str | None,
    parent_session: str | None,
    agent_cmd: str,
):  # noqa: ANN201 — returns an Adapter implementation
    if adapter_name == "opencode":
        if not server:
            click.secho("--server is required for --adapter opencode", fg="red")
            raise SystemExit(EXIT_ERROR)
        try:
            return OpenCodeAdapter(
                server=server,
                directory=directory or str(project_root),
                model=model,
                parent_session=parent_session,
                auth_headers=auth_headers_from_env(),
            )
        except DriverError as err:  # e.g. malformed --model "provider/model"
            click.secho(str(err), fg="red")
            raise SystemExit(EXIT_ERROR) from err
    try:
        return HeadlessAdapter(agent_cmd=agent_cmd, cwd=project_root)
    except DriverError as err:  # empty --agent-cmd
        click.secho(str(err), fg="red")
        raise SystemExit(EXIT_ERROR) from err


_ADAPTER_CHOICE = click.Choice(["opencode", "headless"])
_SCOPE_CHOICE = click.Choice(["full", "execute"])


@workflow_group.command("run")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--scope", type=_SCOPE_CHOICE, default="execute", show_default=True)
@click.option("--adapter", "adapter_name", type=_ADAPTER_CHOICE, default="headless", show_default=True)
@click.option("--params", default=None, help="Runtime params JSON override.")
@click.option("--server", default=None, help="OpenCode server URL (opencode adapter).")
@click.option("--directory", default=None, help="SUT directory for OpenCode ?directory=.")
@click.option("--model", default=None, help='Explicit phase model "provider/model" (opencode).')
@click.option("--parent-session", "parent_session", default=None, help="Parent session id.")
@click.option("--agent-cmd", "agent_cmd", default="cursor-agent --print", show_default=True)
@click.option("--break-at", "break_at", default=None, help="Pause before dispatching this phase.")
@click.option(
    "--detach",
    "detach",
    is_flag=True,
    help="Launch in a detached background process and return immediately (TS `workflow run --detach` parity).",
)
@click.option("--adopt-lock", "adopt_lock", default=None, help="Adopt lock from detached start (internal).")
def workflow_run(
    change_id: str,
    scope: str,
    adapter_name: str,
    params: str | None,
    server: str | None,
    directory: str | None,
    model: str | None,
    parent_session: str | None,
    agent_cmd: str,
    break_at: str | None,
    detach: bool,
    adopt_lock: str | None,
) -> None:
    """Run or resume the deterministic workflow driver.

    Without ``--detach`` the loop runs in the foreground and its four-state
    exit code (0/20/30/40) is the process code. With ``--detach``, lock
    acquisition + child spawn happen via ``start_workflow_detached`` and this
    process returns immediately (startup success/failure only).
    """
    project_root = Path.cwd()
    parsed_params = _parse_params(params)
    if detach:
        if adopt_lock:
            raise click.UsageError("--detach cannot be combined with --adopt-lock")
        started = start_workflow_detached(
            project_root=project_root,
            change_id=change_id,
            scope=scope,
            adapter=adapter_name,
            params=parsed_params,
            agent_cmd=agent_cmd,
            model=model,
            parent_session=parent_session,
            server=server,
            directory=directory,
        )
        if not started.ok:
            click.secho(started.message, fg="red")
            raise SystemExit(EXIT_ERROR)
        click.secho(started.message, fg="green")
        raise SystemExit(EXIT_COMPLETED)
    adapter = _build_adapter(adapter_name, project_root, server, directory, model, parent_session, agent_cmd)
    result = run_workflow_loop(
        project_root=project_root,
        change_id=change_id,
        scope=scope,
        adapter=adapter,
        params=parsed_params,
        parent_session_id=parent_session,
        break_at=break_at,
        adopt_lock_token=adopt_lock,
    )
    color = {
        EXIT_COMPLETED: "green",
        EXIT_HUMAN_REVIEW: "yellow",
        EXIT_STOPPED: "red",
        EXIT_ERROR: "red",
    }.get(result.exit_code, "red")
    click.secho(result.reason, fg=color)
    raise SystemExit(result.exit_code)


@workflow_group.command("status")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
def workflow_status(change_id: str, as_json: bool) -> None:
    """Show driver progress from the driver-state file."""
    try:
        change_dir = resolve_change(Path.cwd(), change_id).path
    except (UnsafeIdentifierError, ChangeNotFoundError) as err:
        click.secho(str(err), fg="red")
        raise SystemExit(EXIT_ERROR) from err
    driver = read_driver_state(change_dir)
    if as_json:
        click.echo(json.dumps({"driver": driver.model_dump() if driver else None}, indent=2))
    elif driver is None:
        click.echo("no driver state found (workflow not started)")
    else:
        click.echo(f"run_id:            {driver.run_id}")
        click.echo(f"status:            {driver.status}")
        click.echo(f"current_phase:     {driver.current_phase}")
        click.echo(f"paused_on:         {driver.paused_on}")
        click.echo(f"checkpoint(iter):  {driver.iteration}")
        click.echo(f"last_checkpoint:   {driver.last_checkpoint_at}")
        if driver.status in {"paused", "failed"}:
            click.echo(
                f"resume:            re-run `aa workflow run --change {change_id}` to resume "
                "from the last iteration boundary"
            )
    raise SystemExit(0)
