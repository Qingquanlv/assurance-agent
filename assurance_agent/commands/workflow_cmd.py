"""`aa workflow run|resume|import-checkpoint|start|supersede|compile` — GraphRuntime CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path

import click
import yaml

from assurance_agent.change_location import ChangeNotFoundError, resolve_change
from assurance_agent.commands.overlay_cli import overlay_options, resolve_cli_overlays
from assurance_agent.exceptions import AaError
from assurance_agent.identifiers import UnsafeIdentifierError
from assurance_agent.retro.supervisor import RetroInvocation, run_retro_supervised
from assurance_agent.workflow.driver.adapter import DriverError
from assurance_agent.workflow.driver.adapter_factory import (
    build_adapter as _factory_build_adapter,
)
from assurance_agent.workflow.driver.driver_state import (
    driver_status_for_graph,
    evaluate_start_guard,
    project_graph_pointer,
    project_supersede_reason,
    read_driver_state,
    supersede_reason_from_error,
    write_driver_state,
)
from assurance_agent.workflow.driver.loop import (
    EXIT_COMPLETED,
    EXIT_ERROR,
    EXIT_HUMAN_REVIEW,
    EXIT_STOPPED,
    run_workflow_loop,
)
from assurance_agent.workflow.driver.runtime_factory import build_graph_runtime, runtime_context_for
from assurance_agent.workflow.graph.compiler import compile_loaded_workflow
from assurance_agent.workflow.graph.contracts import load_execution_contracts
from assurance_agent.workflow.graph.schema_v2 import load_workflow_v2_with_origin
from assurance_agent.workflow.driver.workflow_start import start_workflow_detached
from assurance_agent.workflow.graph.checkpoint import CheckpointImportError, parse_import_manifest
from assurance_agent.workflow.graph.models import ResumeCommand
from assurance_agent.workflow.graph.runtime import GraphRuntimeError, ensure_retro_params
from assurance_agent.workflow.graph.supersede import SupersedeError


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_bytes(raw)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _write_workflow_run_result(
    path: Path,
    *,
    change_id: str,
    entrypoint: str,
    invocation_id: str | None,
    started_new_root: bool,
) -> None:
    _atomic_write_json(
        path,
        {
            "schema_version": "1",
            "change_id": change_id,
            "entrypoint": entrypoint,
            "root_invocation_id": invocation_id,
            "started_new_root": started_new_root,
        },
    )


def _validate_root_invocation(
    runtime,
    *,
    change_dir: Path,
    change_id: str,
    invocation_id: str,
    expected_entrypoint: str,
) -> None:
    try:
        projection = runtime._checkpoints.project(invocation_id)  # noqa: SLF001
    except Exception as exc:
        raise GraphRuntimeError(f"unknown invocation {invocation_id}: {exc}") from exc
    if projection.parent_invocation_id is not None:
        raise GraphRuntimeError(f"invocation {invocation_id} is not a root")
    if projection.entrypoint != expected_entrypoint:
        raise GraphRuntimeError(
            f"invocation {invocation_id} entrypoint {projection.entrypoint!r} "
            f"!= expected {expected_entrypoint!r}"
        )
    meta_path = change_dir / ".graph-runtime" / "invocations" / f"{invocation_id}.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        bound_change = str(meta.get("change_id", change_id))
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise GraphRuntimeError(f"invocation {invocation_id} lacks change metadata: {exc}") from exc
    if bound_change != change_id:
        raise GraphRuntimeError(
            f"invocation {invocation_id} belongs to change {bound_change!r}, not {change_id!r}"
        )


@click.group("workflow")
def workflow_group() -> None:
    """Graph workflow driver (run / resume / import-checkpoint / supersede)."""


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


def _build_adapter(
    adapter_name: str,
    project_root: Path,
    server: str | None,
    directory: str | None,
    model: str | None,
    parent_session: str | None,
    agent_cmd: str,
):  # noqa: ANN201 — returns an AgentInvoker implementation
    try:
        return _factory_build_adapter(
            adapter_name,
            project_root,
            server,
            directory,
            model,
            parent_session,
            agent_cmd,
        )
    except DriverError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(EXIT_ERROR) from err


def _default_who(explicit: str | None) -> str:
    if explicit is not None and explicit.strip():
        return explicit.strip()
    return (os.environ.get("USER") or "unknown").strip() or "unknown"


_ADAPTER_CHOICE = click.Choice(["opencode", "headless"])


def _require_entrypoint(project_root: Path, entrypoint: str, explicit_schema: Path | None) -> None:
    loaded = load_workflow_v2_with_origin(project_root, explicit_schema)
    if entrypoint in loaded.schema.entrypoints:
        return
    available = ", ".join(sorted(loaded.schema.entrypoints))
    click.secho(f"unknown entrypoint {entrypoint!r}; available: {available}", fg="red")
    raise SystemExit(EXIT_ERROR)


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
    result_json: Path | None = None,
    explicit_schema: Path | None = None,
    explicit_contracts: Path | None = None,
) -> None:
    project_root = Path.cwd()
    schema_path, contracts_path = resolve_cli_overlays(project_root, explicit_schema, explicit_contracts)
    try:
        _require_entrypoint(project_root, entrypoint, schema_path)
    except AaError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(EXIT_ERROR) from err
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
            explicit_schema=schema_path,
            explicit_contracts=contracts_path,
        )
        if not started.ok:
            click.secho(started.message, fg="red")
            raise SystemExit(EXIT_ERROR)
        click.secho(started.message, fg="green")
        raise SystemExit(EXIT_COMPLETED)
    adapter = _build_adapter(adapter_name, project_root, server, directory, model, parent_session, agent_cmd)
    if entrypoint == "retro":
        retro_params = ensure_retro_params(parsed_params)
        retro_id = str(retro_params["retro_id"])
        result_holder = None

        def graph_runner(invocation: RetroInvocation):  # noqa: ANN202
            nonlocal result_holder
            result_holder = run_workflow_loop(
                project_root=invocation.project_root,
                change_id=invocation.shell_change_id,
                entrypoint="retro",
                adapter=adapter,
                params=dict(invocation.params),
                explicit_schema=schema_path,
                explicit_contracts=contracts_path,
                parent_session_id=parent_session,
                adopt_lock_token=adopt_lock,
                adapter_name=adapter_name,
                cli_model_override=model,
            )
            return result_holder

        supervised = run_retro_supervised(
            RetroInvocation(
                project_root=project_root,
                shell_change_id=change_id,
                retro_id=retro_id,
                params=retro_params,
            ),
            graph_runner=graph_runner,
        )
        if supervised.status is None:
            click.secho("Retro supervision failed", fg="red")
            raise SystemExit(EXIT_ERROR)
        click.secho(supervised.status.result, fg="green")
        raise SystemExit(EXIT_COMPLETED)

    def bind_root(invocation_id: str, bound_entrypoint: str, started_new_root: bool) -> None:
        if result_json is None:
            return
        _write_workflow_run_result(
            result_json,
            change_id=change_id,
            entrypoint=bound_entrypoint,
            invocation_id=invocation_id,
            started_new_root=started_new_root,
        )

    result = run_workflow_loop(
        project_root=project_root,
        change_id=change_id,
        entrypoint=entrypoint,
        adapter=adapter,
        params=parsed_params,
        explicit_schema=schema_path,
        explicit_contracts=contracts_path,
        parent_session_id=parent_session,
        adopt_lock_token=adopt_lock,
        adapter_name=adapter_name,
        cli_model_override=model,
        on_root_bound=bind_root if result_json is not None else None,
    )
    if result_json is not None:
        _write_workflow_run_result(
            result_json,
            change_id=change_id,
            entrypoint=entrypoint,
            invocation_id=result.invocation_id,
            started_new_root=result.started_new_root,
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
@click.option(
    "--entrypoint",
    default="execute",
    show_default=True,
    help="Graph entrypoint from the loaded workflow schema.",
)
@overlay_options
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
@click.option(
    "--result-json",
    "result_json",
    default=None,
    type=click.Path(path_type=Path),
    help="Atomically write workflow root identity before drive completes.",
)
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
    result_json: Path | None,
    explicit_schema: Path | None,
    explicit_contracts: Path | None,
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
        result_json=result_json,
        explicit_schema=explicit_schema,
        explicit_contracts=explicit_contracts,
    )


@workflow_group.command("start")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option(
    "--entrypoint",
    default="execute",
    show_default=True,
    help="Graph entrypoint from the loaded workflow schema.",
)
@overlay_options
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
    explicit_schema: Path | None,
    explicit_contracts: Path | None,
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
        explicit_schema=explicit_schema,
        explicit_contracts=explicit_contracts,
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
@overlay_options
@click.option(
    "--invocation",
    "invocation_id",
    default=None,
    help="Exact root invocation id to resume (requires --entrypoint).",
)
@click.option(
    "--entrypoint",
    "expected_entrypoint",
    default=None,
    help="Expected entrypoint for --invocation validation.",
)
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
    explicit_schema: Path | None,
    explicit_contracts: Path | None,
    invocation_id: str | None,
    expected_entrypoint: str | None,
) -> None:
    """Plain-resume retry/abandoned work, or resolve one pending interrupt."""
    if (invocation_id is None) ^ (expected_entrypoint is None):
        click.secho("--invocation and --entrypoint must be provided together", fg="red")
        raise SystemExit(EXIT_ERROR)
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
    schema_path, contracts_path = resolve_cli_overlays(project_root, explicit_schema, explicit_contracts)
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
            explicit_schema=schema_path,
            explicit_contracts=contracts_path,
            adapter_name=adapter_name,
            cli_model_override=model,
        )
        if invocation_id is not None:
            assert expected_entrypoint is not None
            _validate_root_invocation(
                bundle.runtime,
                change_dir=change_dir,
                change_id=change_id,
                invocation_id=invocation_id,
                expected_entrypoint=expected_entrypoint,
            )
            target = invocation_id
        else:
            target = bundle.runtime.latest_root_invocation()
            if target is None:
                click.secho("no root invocation to resume", fg="red")
                raise SystemExit(EXIT_ERROR)
        result = bundle.runtime.resume(target, command)
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
                status=driver_status_for_graph(result.status.status),
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


@workflow_group.command("supersede")
@click.option("--change", "change_id", required=True, help="Change ID under qa/changes/.")
@click.option("--invocation", "invocation_id", required=True, help="Latest active legacy root ID.")
@click.option(
    "--action",
    "action",
    required=True,
    type=click.Choice(["rerun-v6", "stop"]),
    help="rerun-v6 starts one replacement root; stop is terminal disposition only.",
)
@click.option("--who", required=True, help="Operator identity (non-empty).")
@click.option("--reason", required=True, help="Operator reason (non-empty).")
@click.option(
    "--params",
    default=None,
    help="Optional JSON params for rerun-v6 only; omission reuses the old root params.",
)
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
@overlay_options
@click.option(
    "--entrypoint",
    "expected_entrypoint",
    default=None,
    help="Optional entrypoint check; must match the root when provided.",
)
def workflow_supersede(
    change_id: str,
    invocation_id: str,
    action: str,
    who: str,
    reason: str,
    params: str | None,
    adapter_name: str,
    server: str | None,
    directory: str | None,
    model: str | None,
    parent_session: str | None,
    agent_cmd: str,
    explicit_schema: Path | None,
    explicit_contracts: Path | None,
    expected_entrypoint: str | None,
) -> None:
    """Audited exit for a legacy root blocked on unbound commit-safety semantics."""
    if not who.strip() or not reason.strip():
        click.secho("supersede requires non-empty --who and --reason", fg="red")
        raise SystemExit(EXIT_ERROR)
    if action == "stop" and params is not None:
        click.secho("stop rejects --params", fg="red")
        raise SystemExit(EXIT_ERROR)

    project_root = Path.cwd()
    schema_path, contracts_path = resolve_cli_overlays(project_root, explicit_schema, explicit_contracts)
    try:
        change_dir = resolve_change(project_root, change_id).path
    except (UnsafeIdentifierError, ChangeNotFoundError) as err:
        click.secho(str(err), fg="red")
        raise SystemExit(EXIT_ERROR) from err

    parsed_params: dict[str, object] | None
    if params is None:
        parsed_params = None
    else:
        parsed_params = _parse_params(params)

    adapter = _build_adapter(adapter_name, project_root, server, directory, model, parent_session, agent_cmd)
    try:
        bundle = build_graph_runtime(
            project_root=project_root,
            change_id=change_id,
            adapter=adapter,
            explicit_schema=schema_path,
            explicit_contracts=contracts_path,
        )
        projection = bundle.runtime._checkpoints.project(invocation_id)  # noqa: SLF001
        entrypoint = expected_entrypoint or projection.entrypoint
        _validate_root_invocation(
            bundle.runtime,
            change_dir=change_dir,
            change_id=change_id,
            invocation_id=invocation_id,
            expected_entrypoint=entrypoint,
        )
        context = runtime_context_for(project_root, change_id, {}, parent_session)
        result = bundle.runtime.supersede(
            bundle.compiled,
            context,
            invocation_id=invocation_id,
            action=action,  # type: ignore[arg-type]
            who=who.strip(),
            reason=reason.strip(),
            params=parsed_params,
        )
    except SupersedeError as err:
        driver = read_driver_state(change_dir)
        if driver is not None:
            write_driver_state(
                change_dir,
                project_supersede_reason(driver, supersede_reason_from_error(err)),
            )
        click.secho(err.reason_code, fg="red")
        raise SystemExit(EXIT_ERROR) from err
    except GraphRuntimeError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(EXIT_ERROR) from err

    driver = read_driver_state(change_dir)
    if driver is not None:
        pointer_id = result.replacement_invocation_id or result.superseded_invocation_id
        write_driver_state(
            change_dir,
            project_supersede_reason(
                project_graph_pointer(
                    driver,
                    invocation_id=pointer_id,
                    checkpoint_id=None,
                    event_seq=driver.event_seq,
                    status="failed" if result.action == "stop" else "running",
                ),
                "superseded",
            ),
        )

    if result.replacement_invocation_id:
        click.secho(
            f"superseded={result.superseded_invocation_id} "
            f"replacement={result.replacement_invocation_id} "
            f"supersede_id={result.supersede_id}",
            fg="green",
        )
    else:
        click.secho(
            f"superseded={result.superseded_invocation_id} action=stop supersede_id={result.supersede_id}",
            fg="green",
        )
    raise SystemExit(EXIT_COMPLETED)


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
@overlay_options
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
    explicit_schema: Path | None,
    explicit_contracts: Path | None,
) -> None:
    """Import an explicit checkpoint manifest, then continue from the ledger."""
    project_root = Path.cwd()
    schema_path, contracts_path = resolve_cli_overlays(project_root, explicit_schema, explicit_contracts)
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
            explicit_schema=schema_path,
            explicit_contracts=contracts_path,
            adapter_name=adapter_name,
            cli_model_override=model,
        )
        context = runtime_context_for(project_root, change_id, parsed_params, parent_session)
        result = bundle.runtime.import_checkpoint(bundle.compiled, manifest, context)
    except AaError as err:
        click.secho(str(err), fg="red")
        raise SystemExit(EXIT_ERROR) from err

    click.secho(
        f"imported invocation={result.invocation_id} checkpoint={result.checkpoint_id} "
        f"tasks={len(result.imported_tasks)}",
        fg="green",
    )
    raise SystemExit(EXIT_COMPLETED)


@workflow_group.command("compile")
@overlay_options
@click.option("--json", "as_json", is_flag=True, help="Machine-readable compile report.")
def workflow_compile(
    explicit_schema: Path | None,
    explicit_contracts: Path | None,
    as_json: bool,
) -> None:
    """Load and compile the workflow schema without driving a change."""
    project_root = Path.cwd()
    schema_path, contracts_path = resolve_cli_overlays(project_root, explicit_schema, explicit_contracts)
    try:
        loaded = load_workflow_v2_with_origin(project_root, schema_path)
        contracts = load_execution_contracts(project_root, contracts_path)
        compiled = compile_loaded_workflow(loaded, contracts)
    except AaError as err:
        if as_json:
            click.echo(json.dumps({"ok": False, "error": str(err)}, sort_keys=True))
        else:
            click.secho(str(err), fg="red")
        raise SystemExit(EXIT_ERROR) from err
    payload = {
        "ok": True,
        "origin": loaded.origin,
        "name": loaded.schema.name,
        "digest": compiled.digest,
        "entrypoints": sorted(compiled.entrypoints),
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    click.echo(f"origin: {loaded.origin}")
    click.echo(f"name: {loaded.schema.name}")
    click.echo(f"digest: {compiled.digest}")
    click.echo("entrypoints:")
    for name in payload["entrypoints"]:
        click.echo(f"  - {name}")
