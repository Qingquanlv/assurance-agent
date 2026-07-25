"""`aa workflow run|status|resume|import-checkpoint|start` — GraphRuntime CLI surface."""

from __future__ import annotations

import json
import os
from pathlib import Path

import click
import yaml

from assurance_agent.change_location import ChangeNotFoundError, resolve_change
from assurance_agent.identifiers import UnsafeIdentifierError
from assurance_agent.workflow.driver.adapter import DriverError
from assurance_agent.workflow.driver.driver_state import (
    evaluate_start_guard,
    project_graph_pointer,
    read_driver_state,
    write_driver_state,
)
from assurance_agent.workflow.driver.headless_adapter import HeadlessAdapter
from assurance_agent.workflow.driver.loop import (
    EXIT_COMPLETED,
    EXIT_ERROR,
    EXIT_HUMAN_REVIEW,
    EXIT_STOPPED,
    run_workflow_loop,
)
from assurance_agent.workflow.driver.opencode_adapter import OpenCodeAdapter, auth_headers_from_env
from assurance_agent.workflow.driver.runtime_factory import build_graph_runtime, runtime_context_for
from assurance_agent.workflow.driver.workflow_start import start_workflow_detached
from assurance_agent.workflow.graph.checkpoint import CheckpointImportError, parse_import_manifest
from assurance_agent.workflow.graph.models import ResumeCommand
from assurance_agent.workflow.graph.runtime import GraphRuntimeError


@click.group("workflow")
def workflow_group() -> None:
    """Graph workflow driver (run / status / resume / import-checkpoint)."""


def _parse_json_object(raw: str | None, option_name: str) -> dict[str, object]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as err:
        click.secho(f"Invalid {option_name} JSON: {err}", fg="red")
        raise SystemExit(EXIT_ERROR) from err
    if not isinstance(parsed, dict):
        click.secho(f"Invalid {option_name} JSON: expected an object", fg="red")
        raise SystemExit(EXIT_ERROR)
    return parsed


def _parse_params(raw: str | None) -> dict[str, object]:
    return _parse_json_object(raw, "--params")


_DEFAULT_CURSOR_MODEL = "cursor-grok-4.5-high-fast"


def _resolve_headless_model(model: str | None) -> str:
    """Prefer explicit --model, then env, then the cursor-agent default."""
    if model and model.strip():
        return model.strip()
    for key in ("AA_CURSOR_MODEL", "CURSOR_MODEL"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return _DEFAULT_CURSOR_MODEL


def _build_adapter(
    adapter_name: str,
    project_root: Path,
    server: str | None,
    directory: str | None,
    model: str | None,
    parent_session: str | None,
    agent_cmd: str,
):  # noqa: ANN201 — returns an AgentInvoker implementation
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
        except DriverError as err:
            click.secho(str(err), fg="red")
            raise SystemExit(EXIT_ERROR) from err
    try:
        return HeadlessAdapter(
            agent_cmd=agent_cmd,
            cwd=project_root,
            model=_resolve_headless_model(model),
        )
    except DriverError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(EXIT_ERROR) from err


def _default_who(explicit: str | None) -> str:
    if explicit is not None and explicit.strip():
        return explicit.strip()
    return (os.environ.get("USER") or "unknown").strip() or "unknown"


_ADAPTER_CHOICE = click.Choice(["opencode", "headless"])
_ENTRYPOINT_CHOICE = click.Choice(
    [
        "full",
        "intake",
        "execute",
        "case",
        "archive",
        "retro",
        "issue-review",
        "issue-analyze",
        "issue-reconcile",
        "improvement-review",
        "improvement-evaluate",
        "improvement-export",
        "improvement-apply",
        "improvement-rollback",
    ]
)


def _run_or_detach(
    *,
    change_id: str,
    entrypoint: str,
    adapter_name: str,
    params: str | None,
    server: str | None,
    directory: str | None,
    model: str | None,
    parent_session: str | None,
    agent_cmd: str,
    detach: bool,
    adopt_lock: str | None,
) -> None:
    project_root = Path.cwd()
    parsed_params = _parse_params(params)
    if detach:
        if adopt_lock:
            raise click.UsageError("--detach cannot be combined with --adopt-lock")
        started = start_workflow_detached(
            project_root=project_root,
            change_id=change_id,
            entrypoint=entrypoint,
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
        entrypoint=entrypoint,
        adapter=adapter,
        params=parsed_params,
        parent_session_id=parent_session,
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


@workflow_group.command("run")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--entrypoint", type=_ENTRYPOINT_CHOICE, default="execute", show_default=True)
@click.option("--adapter", "adapter_name", type=_ADAPTER_CHOICE, default="headless", show_default=True)
@click.option("--params", default=None, help="Runtime params JSON override.")
@click.option("--server", default=None, help="OpenCode server URL (opencode adapter).")
@click.option("--directory", default=None, help="SUT directory for OpenCode ?directory=.")
@click.option(
    "--model",
    default=None,
    help='Model id. OpenCode: "provider/model". Headless/cursor-agent: defaults to cursor-grok-4.5-high-fast.',
)
@click.option("--parent-session", "parent_session", default=None, help="Parent session id.")
@click.option("--agent-cmd", "agent_cmd", default="cursor-agent --print", show_default=True)
@click.option(
    "--detach",
    "detach",
    is_flag=True,
    help="Launch in a detached background process and return immediately.",
)
@click.option("--adopt-lock", "adopt_lock", default=None, help="Adopt lock from detached start (internal).")
def workflow_run(
    change_id: str,
    entrypoint: str,
    adapter_name: str,
    params: str | None,
    server: str | None,
    directory: str | None,
    model: str | None,
    parent_session: str | None,
    agent_cmd: str,
    detach: bool,
    adopt_lock: str | None,
) -> None:
    """Run a new graph invocation, or plain-resume the latest root if one exists."""
    _run_or_detach(
        change_id=change_id,
        entrypoint=entrypoint,
        adapter_name=adapter_name,
        params=params,
        server=server,
        directory=directory,
        model=model,
        parent_session=parent_session,
        agent_cmd=agent_cmd,
        detach=detach,
        adopt_lock=adopt_lock,
    )


@workflow_group.command("start")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--entrypoint", type=_ENTRYPOINT_CHOICE, default="execute", show_default=True)
@click.option("--adapter", "adapter_name", type=_ADAPTER_CHOICE, default="headless", show_default=True)
@click.option("--params", default=None, help="Runtime params JSON override.")
@click.option("--server", default=None, help="OpenCode server URL (opencode adapter).")
@click.option("--directory", default=None, help="SUT directory for OpenCode ?directory=.")
@click.option(
    "--model",
    default=None,
    help='Model id. OpenCode: "provider/model". Headless/cursor-agent: defaults to cursor-grok-4.5-high-fast.',
)
@click.option("--parent-session", "parent_session", default=None, help="Parent session id.")
@click.option("--agent-cmd", "agent_cmd", default="cursor-agent --print", show_default=True)
def workflow_start(
    change_id: str,
    entrypoint: str,
    adapter_name: str,
    params: str | None,
    server: str | None,
    directory: str | None,
    model: str | None,
    parent_session: str | None,
    agent_cmd: str,
) -> None:
    """Detached alias for ``aa workflow run --detach``."""
    _run_or_detach(
        change_id=change_id,
        entrypoint=entrypoint,
        adapter_name=adapter_name,
        params=params,
        server=server,
        directory=directory,
        model=model,
        parent_session=parent_session,
        agent_cmd=agent_cmd,
        detach=True,
        adopt_lock=None,
    )


@workflow_group.command("resume")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--interrupt", "interrupt_id", default=None, help="Pending interrupt id to resolve.")
@click.option("--action", default=None, help="Interrupt action (requires --interrupt).")
@click.option("--reason", default=None, help="Human reason (required with --interrupt).")
@click.option("--who", "who", default=None, help="Decision author (defaults to $USER).")
@click.option("--payload", default=None, help="Structured JSON payload for the interrupt resolution.")
@click.option("--adapter", "adapter_name", type=_ADAPTER_CHOICE, default="headless", show_default=True)
@click.option("--server", default=None, help="OpenCode server URL (opencode adapter).")
@click.option("--directory", default=None, help="SUT directory for OpenCode ?directory=.")
@click.option(
    "--model",
    default=None,
    help='Model id. OpenCode: "provider/model". Headless/cursor-agent: defaults to cursor-grok-4.5-high-fast.',
)
@click.option("--parent-session", "parent_session", default=None, help="Parent session id.")
@click.option("--agent-cmd", "agent_cmd", default="cursor-agent --print", show_default=True)
def workflow_resume(
    change_id: str,
    interrupt_id: str | None,
    action: str | None,
    reason: str | None,
    who: str | None,
    payload: str | None,
    adapter_name: str,
    server: str | None,
    directory: str | None,
    model: str | None,
    parent_session: str | None,
    agent_cmd: str,
) -> None:
    """Plain-resume retry/abandoned work, or resolve one pending interrupt."""
    if action is not None and interrupt_id is None:
        click.secho("--action requires --interrupt", fg="red")
        raise SystemExit(EXIT_ERROR)
    if interrupt_id is not None:
        if action is None:
            click.secho("--interrupt requires --action", fg="red")
            raise SystemExit(EXIT_ERROR)
        if reason is None or not reason.strip():
            click.secho("--reason is required with --interrupt", fg="red")
            raise SystemExit(EXIT_ERROR)
        resolved_who = _default_who(who)
        if not resolved_who.strip():
            click.secho("--who (or $USER) is required with --interrupt", fg="red")
            raise SystemExit(EXIT_ERROR)
        command = ResumeCommand(
            interrupt_id=interrupt_id,
            action=action,
            reason=reason.strip(),
            who=resolved_who,
            payload=_parse_json_object(payload, "--payload"),
        )
    else:
        command = None

    project_root = Path.cwd()
    try:
        change_dir = resolve_change(project_root, change_id).path
    except (UnsafeIdentifierError, ChangeNotFoundError) as err:
        click.secho(str(err), fg="red")
        raise SystemExit(EXIT_ERROR) from err

    guard = evaluate_start_guard(change_dir)
    if not guard.allowed:
        click.secho(guard.reason or "start refused", fg="red")
        raise SystemExit(EXIT_ERROR)

    adapter = _build_adapter(adapter_name, project_root, server, directory, model, parent_session, agent_cmd)
    try:
        bundle = build_graph_runtime(
            project_root=project_root,
            change_id=change_id,
            adapter=adapter,
        )
        latest = bundle.runtime.latest_root_invocation()
        if latest is None:
            click.secho("no root invocation to resume", fg="red")
            raise SystemExit(EXIT_ERROR)
        result = bundle.runtime.resume(latest, command)
    except GraphRuntimeError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(EXIT_ERROR) from err

    driver = read_driver_state(change_dir)
    if driver is not None:
        write_driver_state(
            change_dir,
            project_graph_pointer(
                driver,
                invocation_id=result.invocation_id,
                checkpoint_id=result.status.checkpoint_id,
                event_seq=result.status.event_seq,
                status=(
                    "completed"
                    if result.status.status == "completed"
                    else "paused"
                    if result.status.status == "interrupted"
                    else "failed"
                    if result.status.status in {"stopped", "failed"}
                    else "running"
                ),
            ),
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
    """Show GraphStatus for the latest root invocation (ledger, not driver.json)."""
    project_root = Path.cwd()
    try:
        resolve_change(project_root, change_id)
    except (UnsafeIdentifierError, ChangeNotFoundError) as err:
        click.secho(str(err), fg="red")
        raise SystemExit(EXIT_ERROR) from err

    adapter = HeadlessAdapter(agent_cmd="true", cwd=project_root)
    try:
        bundle = build_graph_runtime(
            project_root=project_root,
            change_id=change_id,
            adapter=adapter,
        )
        latest = bundle.runtime.latest_root_invocation()
        if latest is None:
            payload = {"status": None, "invocation_id": None}
            if as_json:
                click.echo(json.dumps(payload, indent=2))
            else:
                click.echo("no graph invocation found (workflow not started)")
            raise SystemExit(0)
        status = bundle.runtime.status(latest)
    except GraphRuntimeError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(EXIT_ERROR) from err

    if as_json:
        click.echo(json.dumps(status.model_dump(mode="json"), indent=2))
    else:
        click.echo(f"invocation_id:     {status.invocation_id}")
        click.echo(f"entrypoint:        {status.entrypoint}")
        click.echo(f"status:            {status.status}")
        click.echo(f"checkpoint_id:     {status.checkpoint_id}")
        click.echo(f"event_seq:         {status.event_seq}")
        click.echo(f"pending_tasks:     {', '.join(status.pending_tasks) or '(none)'}")
        interrupts = ", ".join(i.interrupt_id for i in status.pending_interrupts) or "(none)"
        click.echo(f"pending_interrupts:{interrupts}")
        if status.terminal_reason:
            click.echo(f"terminal_reason:   {status.terminal_reason}")
        if status.status in {"interrupted", "failed", "stopped"}:
            click.echo(
                f"resume:            re-run `aa workflow resume --change {change_id}` "
                "to advance from the ledger"
            )
    raise SystemExit(0)


@workflow_group.command("import-checkpoint")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option(
    "--manifest",
    "manifest_path",
    required=True,
    type=click.Path(path_type=Path),
    help="Import manifest YAML.",
)
@click.option("--adapter", "adapter_name", type=_ADAPTER_CHOICE, default="headless", show_default=True)
@click.option("--params", default=None, help="Runtime params JSON override.")
@click.option("--server", default=None, help="OpenCode server URL (opencode adapter).")
@click.option("--directory", default=None, help="SUT directory for OpenCode ?directory=.")
@click.option(
    "--model",
    default=None,
    help='Model id. OpenCode: "provider/model". Headless/cursor-agent: defaults to cursor-grok-4.5-high-fast.',
)
@click.option("--parent-session", "parent_session", default=None, help="Parent session id.")
@click.option("--agent-cmd", "agent_cmd", default="cursor-agent --print", show_default=True)
def workflow_import_checkpoint(
    change_id: str,
    manifest_path: Path,
    adapter_name: str,
    params: str | None,
    server: str | None,
    directory: str | None,
    model: str | None,
    parent_session: str | None,
    agent_cmd: str,
) -> None:
    """Import an explicit checkpoint manifest, then continue from the ledger."""
    project_root = Path.cwd()
    parsed_params = _parse_params(params)
    path = manifest_path if manifest_path.is_absolute() else project_root / manifest_path
    if not path.is_file():
        click.secho(f"manifest not found: {path}", fg="red")
        raise SystemExit(EXIT_ERROR)
    try:
        manifest = parse_import_manifest(path.read_text(encoding="utf-8"))
    except (CheckpointImportError, yaml.YAMLError) as err:
        click.secho(f"invalid manifest: {err}", fg="red")
        raise SystemExit(EXIT_ERROR) from err

    adapter = _build_adapter(adapter_name, project_root, server, directory, model, parent_session, agent_cmd)
    try:
        bundle = build_graph_runtime(
            project_root=project_root,
            change_id=change_id,
            adapter=adapter,
        )
        context = runtime_context_for(project_root, change_id, parsed_params, parent_session)
        result = bundle.runtime.import_checkpoint(bundle.compiled, manifest, context)
    except (GraphRuntimeError, CheckpointImportError) as err:
        click.secho(str(err), fg="red")
        raise SystemExit(EXIT_ERROR) from err

    click.secho(
        f"imported invocation={result.invocation_id} checkpoint={result.checkpoint_id} "
        f"tasks={len(result.imported_tasks)}",
        fg="green",
    )
    raise SystemExit(EXIT_COMPLETED)
