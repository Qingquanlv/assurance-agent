from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from assurance_product.bootstrap.contracts import BootstrapStatusV1, OpenCodeHandleV1, RunSpecV1
from assurance_product.bootstrap.opencode import OpenCodeLaunchError
from assurance_product.bootstrap.opencode import attach_shared_opencode as _attach_shared_opencode
from assurance_product.bootstrap.opencode import dispose_project_instance
from assurance_product.bootstrap.opencode import start_opencode_serve as _start_opencode_serve
from assurance_product.bootstrap.opencode import stop_opencode as _stop_opencode
from assurance_product.bootstrap.opencode import wait_http_ready
from assurance_product.bootstrap.preflight import BootstrapPreflightError, preflight_bootstrap
from assurance_product.bootstrap.resources import release_active_resource_authorizations
from assurance_product.bootstrap.spec import load_run_spec
from assurance_product.bootstrap.status import (
    derive_bootstrap_change_id,
    read_bootstrap_status,
    read_run_manifest,
    run_dir_for,
    write_bootstrap_status,
    write_effective_spec,
    write_run_manifest,
    write_stop_request,
)
from assurance_product.opencode_agents import install_opencode_agents
from assurance_product.sut_worktree import ensure_run_worktree

_SUT_READY_TIMEOUT_SECONDS = 90.0
_TERMINAL_STATUSES = frozenset({"completed", "succeeded", "stopped", "interrupted", "failed"})
_EXIT_BY_STATUS = {
    "completed": 0,
    "succeeded": 0,
    "stopped": 20,
    "interrupted": 30,
    "failed": 40,
}


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _persist(
    run_dir: Path,
    *,
    phase: str,
    change_id: str,
    opencode: OpenCodeHandleV1 | None = None,
    status: Mapping[str, Any] | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
    exit_code: int | None = None,
    error: str | None = None,
    root_session_id: str | None = None,
) -> BootstrapStatusV1:
    if root_session_id is None:
        try:
            root_session_id = read_bootstrap_status(run_dir).root_session_id
        except (OSError, ValueError):
            root_session_id = None
    value = BootstrapStatusV1(
        phase=phase,  # type: ignore[arg-type]
        change_id=change_id,
        opencode=opencode,
        root_session_id=root_session_id,
        status=dict(status or {}),
        started_at=started_at,
        updated_at=_iso_now(),
        ended_at=ended_at,
        exit_code=exit_code,
        error=error,
    )
    write_bootstrap_status(run_dir, value)
    return value


def _secret_arg(spec: RunSpecV1) -> str:
    return f"opencode.token=env:{spec.opencode_token_env}"


def _default_start_invocation(**kwargs: Any) -> dict[str, object]:
    from assurance_product.cli import _start_invocation

    return _start_invocation(**kwargs)


def _default_run_invocation(**kwargs: Any) -> tuple[object, str]:
    from assurance_product.cli import _run_invocation

    result, mapped = _run_invocation(**kwargs)
    return result, mapped


def _default_read_status(**kwargs: Any) -> dict[str, object]:
    from assurance_product.application import AssuranceProductApplication
    from assurance_product.cli import _authorize_secrets, _bind_workspace, _resolve_and_audit

    reuse_directory = bool(kwargs.pop("reuse_directory", False))
    composition, _audit = _resolve_and_audit(
        product=str(kwargs["product"]),
        binding_dist=str(kwargs["binding_dist"]),
        binding_entrypoint=str(kwargs["binding_entrypoint"]),
        binding_declaration=str(kwargs["binding_declaration"]),
        config_tree=str(kwargs["config_tree"]),
    )
    secrets = kwargs["secrets"]
    authorization = _authorize_secrets(composition, secrets)
    workspace = _bind_workspace(
        Path(str(kwargs["project_dir"])),
        str(kwargs["change_id"]),
        create=reuse_directory,
    )
    return (
        AssuranceProductApplication()
        .status(
            workspace=workspace,
            composition=composition,
            authorization=authorization,
            invocation_id=str(kwargs["invocation_id"]),
            change_id=str(kwargs["change_id"]),
        )
        .model_dump(mode="json")
    )


def _private_server(run_dir: Path) -> bool:
    try:
        manifest = read_run_manifest(run_dir)
    except (OSError, ValueError):
        return True
    return manifest.get("ownership") != "shared"


def _manifest_fields(run_dir: Path, fields: Mapping[str, object]) -> dict[str, object]:
    try:
        current = read_run_manifest(run_dir)
    except (OSError, ValueError):
        current = {}
    return {**current, **fields}


def _exit_code_for(snapshot: Mapping[str, Any], mapped: str | None) -> int:
    status = str(snapshot.get("status") or mapped or "failed")
    return _EXIT_BY_STATUS.get(status, 40)


def _is_terminal(snapshot: Mapping[str, Any], mapped: str | None) -> bool:
    status = str(snapshot.get("status") or mapped or "")
    return status in _TERMINAL_STATUSES


def _publish_managed_attempts(project_dir: Path, change_id: str) -> None:
    database = project_dir / "qa" / ".runtime" / "langgraph" / "checkpoints.sqlite3"
    if not database.is_file() or database.is_symlink():
        return
    from assurance_product.operator_views import publish_run_attempts

    publish_run_attempts(project_dir, change_id)


def _drive_application(
    *,
    project_dir: Path,
    spec: RunSpecV1,
    run_dir: Path,
    change_id: str,
    prepared: Mapping[str, object],
    handle: OpenCodeHandleV1,
    started_at: str,
    start_invocation: Callable[..., dict[str, object]],
    run_invocation: Callable[..., tuple[object, str]],
    read_status: Callable[..., Mapping[str, Any]],
    probe_shared: Callable[..., None] | None = None,
    reuse_directory: bool = False,
    resume_invocation: Callable[..., tuple[object, str]] | None = None,
) -> BootstrapStatusV1:
    invocation_id = change_id
    secrets = (_secret_arg(spec),)
    common = {
        "project_dir": project_dir,
        "change_id": change_id,
        "invocation_id": invocation_id,
        "product": str(prepared["product"]),
        "binding_dist": str(prepared["binding_dist"]),
        "binding_entrypoint": "deployment",
        "binding_declaration": str(prepared["binding_declaration"]),
        "config_tree": str(prepared["config_tree"]),
        "secrets": secrets,
        "reuse_directory": reuse_directory,
    }
    start_invocation(
        **common,
        entrypoint=spec.entrypoint,
        input_path=Path(str(prepared["input_path"])),
    )
    current = _persist(
        run_dir,
        phase="started",
        change_id=change_id,
        opencode=handle,
        started_at=started_at,
    )
    deadline = time.monotonic() + spec.timeout_seconds
    mapped: str | None = None
    snapshot: Mapping[str, Any] = {}
    while True:
        if handle.ownership == "shared" and probe_shared is not None:
            try:
                probe_shared(f"{handle.endpoint.rstrip('/')}/global/health", timeout=2)
            except OpenCodeLaunchError as error:
                return _persist(
                    run_dir,
                    phase="terminal",
                    change_id=change_id,
                    opencode=handle,
                    status=snapshot,
                    started_at=started_at,
                    ended_at=_iso_now(),
                    exit_code=30,
                    error=f"shared OpenCode server is unavailable: {error}",
                )
        if time.monotonic() >= deadline:
            return _persist(
                run_dir,
                phase="terminal",
                change_id=change_id,
                opencode=handle,
                status=snapshot,
                started_at=started_at,
                ended_at=_iso_now(),
                exit_code=20,
                error="timeout",
            )
        current = _persist(
            run_dir,
            phase="running",
            change_id=change_id,
            opencode=handle,
            status=snapshot,
            started_at=started_at,
        )
        del current
        if resume_invocation is not None:
            _result, mapped = resume_invocation(**common, stop_file=run_dir / "stop-request.json")
            resume_invocation = None
        else:
            _result, mapped = run_invocation(
                **common,
                entrypoint=None,
                input_path=None,
                stop_file=run_dir / "stop-request.json",
            )
        snapshot = dict(read_status(**common))
        if reuse_directory:
            _publish_managed_attempts(project_dir, change_id)
        pending = snapshot.get("pending_interrupt")
        paused = (
            snapshot.get("status") == "blocked"
            and isinstance(pending, Mapping)
            and pending.get("reason_category") == "operator_stop"
        )
        if _is_terminal(snapshot, mapped) or paused:
            return _persist(
                run_dir,
                phase="terminal",
                change_id=change_id,
                opencode=handle,
                status=snapshot,
                started_at=started_at,
                ended_at=_iso_now(),
                exit_code=20 if paused else _exit_code_for(snapshot, mapped),
            )
        time.sleep(1.0)


def run_bootstrap(
    *,
    project_dir: Path,
    spec: RunSpecV1,
    runs_root: Path,
    change_id: str | None,
    environ: Mapping[str, str],
    start_opencode_serve: Callable[..., OpenCodeHandleV1] | None = None,
    stop_opencode: Callable[[OpenCodeHandleV1], None] | None = None,
    prepare_composition: Callable[..., dict[str, object]] | None = None,
    start_invocation: Callable[..., dict[str, object]] | None = None,
    run_invocation: Callable[..., tuple[object, str]] | None = None,
    read_status: Callable[..., Mapping[str, Any]] | None = None,
    wait_ready: Callable[..., None] | None = None,
    shared_endpoint: str | None = None,
    ensure_run_root: Callable[..., str] | None = None,
    task_directory: Path | None = None,
    dispose_instance: Callable[..., None] | None = None,
) -> BootstrapStatusV1:
    from assurance_product.bootstrap.composition import prepare_composition as _prepare_composition

    if dispose_instance is None and start_opencode_serve is None:
        dispose_instance = dispose_project_instance
    resolved_change = change_id or derive_bootstrap_change_id(
        stamp=utc_stamp(),
        nonce=secrets.token_hex(4),
    )
    selected_task = task_directory
    reuse_directory = selected_task is not None
    if selected_task is not None:
        project_dir = selected_task.resolve()
        if not project_dir.is_dir():
            raise ValueError("task directory does not exist")
        # Shared attach skips start_opencode_serve, which is what installs the
        # write boundary. Without that plugin, agent writes land in the task
        # root and intake finalize cannot see them in the attempt write root.
        install_opencode_agents(project_dir)
    else:
        project_dir = ensure_run_worktree(project_dir, resolved_change)
    run_dir = run_dir_for(runs_root, resolved_change)
    write_effective_spec(run_dir, spec)
    started_at = _iso_now()
    current = _persist(run_dir, phase="preparing", change_id=resolved_change, started_at=started_at)
    start_serve = start_opencode_serve or _start_opencode_serve
    stop = stop_opencode or _stop_opencode
    prepare = prepare_composition or _prepare_composition
    start_app = start_invocation or _default_start_invocation
    run_app = run_invocation or _default_run_invocation
    status_app = read_status or _default_read_status
    ready = wait_ready or wait_http_ready
    handle: OpenCodeHandleV1 | None = None
    import assurance_product.cli as cli_mod

    original_resolve = cli_mod._resolve_and_audit
    resolve_cache: dict[tuple[str, str, str, str, str], object] = {}

    def _cached_resolve(
        *,
        product: str,
        binding_dist: str,
        binding_entrypoint: str,
        binding_declaration: str,
        config_tree: str,
    ) -> object:
        key = (product, binding_dist, binding_entrypoint, binding_declaration, config_tree)
        hit = resolve_cache.get(key)
        if hit is None:
            hit = original_resolve(
                product=product,
                binding_dist=binding_dist,
                binding_entrypoint=binding_entrypoint,
                binding_declaration=binding_declaration,
                config_tree=config_tree,
            )
            resolve_cache[key] = hit
        return hit

    cli_mod._resolve_and_audit = _cached_resolve
    try:
        try:
            preflight_bootstrap(
                project_dir=project_dir,
                spec=spec,
                runs_root=runs_root,
                change_id=resolved_change,
                environ=environ,
                reuse_directory=reuse_directory,
            )
        except BootstrapPreflightError as error:
            _persist(
                run_dir,
                phase="terminal",
                change_id=resolved_change,
                started_at=started_at,
                ended_at=_iso_now(),
                exit_code=40,
                error=str(error),
            )
            raise
        release_active_resource_authorizations(
            project_dir / "qa" / ".runtime" / "langgraph" / "checkpoints.sqlite3"
        )
        try:
            ready(spec.sut.readiness_url, timeout=_SUT_READY_TIMEOUT_SECONDS)
        except OpenCodeLaunchError as error:
            _persist(
                run_dir,
                phase="terminal",
                change_id=resolved_change,
                started_at=started_at,
                ended_at=_iso_now(),
                exit_code=40,
                error=f"SUT not ready at {spec.sut.readiness_url}: {error}",
            )
            raise
        try:
            if shared_endpoint is not None:
                token = environ.get(spec.opencode_token_env)
                authorization = None
                if token:
                    from assurance_product.bootstrap.opencode import _basic_opencode_authorization

                    authorization = _basic_opencode_authorization(token)
                handle = _attach_shared_opencode(
                    endpoint=shared_endpoint,
                    authorization=authorization,
                    probe=ready,
                )
            else:
                handle = start_serve(
                    spec=spec,
                    project_dir=project_dir,
                    run_dir=run_dir,
                    environ=environ,
                )
        except (OpenCodeLaunchError, ValueError) as error:
            shared_failure = shared_endpoint is not None and isinstance(error, OpenCodeLaunchError)
            persisted = _persist(
                run_dir,
                phase="terminal",
                change_id=resolved_change,
                started_at=started_at,
                ended_at=_iso_now(),
                exit_code=30 if shared_failure else 40,
                error=str(error),
            )
            if shared_failure:
                return persisted
            raise
        if reuse_directory and dispose_instance is not None:
            token = environ.get(spec.opencode_token_env)
            authorization = None
            if token:
                from assurance_product.bootstrap.opencode import _basic_opencode_authorization

                authorization = _basic_opencode_authorization(token)
            try:
                dispose_instance(
                    endpoint=handle.endpoint,
                    directory=str(project_dir.resolve()),
                    authorization=authorization,
                )
            except OpenCodeLaunchError as error:
                return _persist(
                    run_dir,
                    phase="terminal",
                    change_id=resolved_change,
                    opencode=handle,
                    started_at=started_at,
                    ended_at=_iso_now(),
                    exit_code=30,
                    error=str(error),
                )
        current = _persist(
            run_dir,
            phase="opencode_ready",
            change_id=resolved_change,
            opencode=handle,
            started_at=started_at,
            root_session_id=None,
        )
        prepared = prepare(
            project_dir=project_dir,
            run_dir=run_dir,
            spec=spec,
            opencode_endpoint=handle.endpoint,
            change_id=resolved_change,
            parent_session_id=None,
        )
        write_run_manifest(
            run_dir,
            _manifest_fields(
                run_dir,
                {
                    "project_dir": str(project_dir.resolve()),
                    "change_id": resolved_change,
                    "invocation_id": resolved_change,
                    "product": prepared["product"],
                    "binding_dist": prepared["binding_dist"],
                    "binding_declaration": prepared["binding_declaration"],
                    "config_tree": str(prepared["config_tree"]),
                    "input_path": str(prepared["input_path"]),
                    "entrypoint": spec.entrypoint,
                    "opencode_endpoint": handle.endpoint,
                    **(
                        {"ownership": "shared", "requested_opencode_endpoint": handle.endpoint}
                        if handle.ownership == "shared"
                        else {}
                    ),
                },
            ),
        )
        current = _persist(
            run_dir,
            phase="compiled",
            change_id=resolved_change,
            opencode=handle,
            started_at=started_at,
        )
        current = _drive_application(
            project_dir=project_dir,
            spec=spec,
            run_dir=run_dir,
            change_id=resolved_change,
            prepared=prepared,
            handle=handle,
            started_at=started_at,
            start_invocation=start_app,
            run_invocation=run_app,
            read_status=status_app,
            probe_shared=ready if handle.ownership == "shared" else None,
            reuse_directory=reuse_directory,
        )
        return current
    except (BootstrapPreflightError, OpenCodeLaunchError):
        raise
    except Exception as error:
        previous = read_bootstrap_status(run_dir)
        if previous.phase != "terminal":
            cause = error.__cause__ or error.__context__
            detail = str(error) if cause is None else f"{error}: {cause}"
            _persist(
                run_dir,
                phase="terminal",
                change_id=resolved_change,
                opencode=handle,
                status=previous.status,
                started_at=started_at,
                ended_at=_iso_now(),
                exit_code=40,
                error=detail,
            )
        raise
    finally:
        cli_mod._resolve_and_audit = original_resolve
        if handle is not None and handle.ownership != "shared" and _private_server(run_dir):
            stop(handle)


def stop_bootstrap(
    run_dir: Path,
    *,
    stop_opencode: Callable[[OpenCodeHandleV1], None] | None = None,
) -> BootstrapStatusV1:
    # The worker owns cleanup after the graph reaches a durable pause boundary.
    del stop_opencode
    status = read_bootstrap_status(run_dir)
    if status.phase == "terminal":
        return status
    write_stop_request(run_dir, change_id=status.change_id)
    return read_bootstrap_status(run_dir)


def resume_bootstrap(
    run_dir: Path,
    *,
    environ: Mapping[str, str],
    action: str | None = None,
    reason: str | None = None,
    start_opencode_serve: Callable[..., OpenCodeHandleV1] | None = None,
    stop_opencode: Callable[[OpenCodeHandleV1], None] | None = None,
    prepare_composition: Callable[..., dict[str, object]] | None = None,
    start_invocation: Callable[..., dict[str, object]] | None = None,
    run_invocation: Callable[..., tuple[object, str]] | None = None,
    read_status: Callable[..., Mapping[str, Any]] | None = None,
    wait_ready: Callable[..., None] | None = None,
) -> BootstrapStatusV1:
    from assurance_product.bootstrap.composition import prepare_composition as _prepare_composition

    status = read_bootstrap_status(run_dir)
    if status.phase != "terminal":
        raise ValueError("resume requires a terminal bootstrap run")
    if (action is None) != (reason is None):
        raise ValueError("interrupt resume requires action and reason")
    stop_path = run_dir / "stop-request.json"
    stopped = stop_path.exists() or stop_path.is_symlink()
    acknowledged = False
    if stopped:
        import json

        if stop_path.is_symlink() or not stop_path.is_file():
            raise ValueError("stop request must be a regular file")
        request = json.loads(stop_path.read_bytes())
        if not isinstance(request, dict) or request.get("change_id") != status.change_id:
            raise ValueError("operator pause does not match the stop request")
        pending = status.status.get("pending_interrupt")
        graph_status = status.status.get("status")
        acknowledged = (
            action is None
            and graph_status == "completed"
            or action is not None
            and graph_status == "interrupted"
            and isinstance(pending, Mapping)
            or action is None
            and graph_status == "blocked"
            and isinstance(pending, Mapping)
            and pending.get("reason_category") == "operator_stop"
        )
        if action is not None and not acknowledged:
            raise ValueError("restart requires a confirmed operator pause")
    spec = load_run_spec(run_dir / "run-spec.effective.yaml")
    manifest = read_run_manifest(run_dir)
    project_dir = Path(str(manifest["project_dir"]))
    change_id = status.change_id
    started_at = status.started_at or _iso_now()
    start_serve = start_opencode_serve or _start_opencode_serve
    stop = stop_opencode or _stop_opencode
    prepare = prepare_composition or _prepare_composition
    start_app = start_invocation or _default_start_invocation
    run_app = run_invocation or _default_run_invocation
    status_app = read_status or _default_read_status
    ready = wait_ready or wait_http_ready
    handle: OpenCodeHandleV1 | None = None

    def resume_app(**kwargs: Any) -> tuple[object, str]:
        from assurance_product.cli import _resume_invocation

        kwargs.pop("reuse_directory", None)
        result, mapped, _code = _resume_invocation(**kwargs, action=action, reason=reason)
        return result, mapped

    prepared: dict[str, object] | None = None
    try:
        ready(spec.sut.readiness_url, timeout=_SUT_READY_TIMEOUT_SECONDS)
        if manifest.get("ownership") == "shared":
            endpoint = manifest.get("requested_opencode_endpoint") or (
                status.opencode.endpoint if status.opencode is not None else ""
            )
            if not isinstance(endpoint, str) or not endpoint:
                raise OpenCodeLaunchError("shared resume is missing the borrowed endpoint")
            handle = _attach_shared_opencode(endpoint=endpoint, probe=ready)
        else:
            handle = start_serve(
                spec=spec,
                project_dir=project_dir,
                run_dir=run_dir,
                environ=environ,
            )
        prepared = prepare(
            project_dir=project_dir,
            run_dir=run_dir,
            spec=spec,
            opencode_endpoint=handle.endpoint,
            change_id=change_id,
            parent_session_id=None,
        )
        write_run_manifest(
            run_dir,
            {
                **manifest,
                "binding_dist": prepared["binding_dist"],
                "binding_declaration": prepared["binding_declaration"],
                "config_tree": str(prepared["config_tree"]),
                "input_path": str(prepared["input_path"]),
                "opencode_endpoint": handle.endpoint,
            },
        )
        if acknowledged:
            stop_path.unlink()
        return _drive_application(
            project_dir=project_dir,
            spec=spec,
            run_dir=run_dir,
            change_id=change_id,
            prepared=prepared,
            handle=handle,
            started_at=started_at,
            start_invocation=start_app,
            run_invocation=run_app,
            read_status=status_app,
            probe_shared=ready if handle.ownership == "shared" else None,
            reuse_directory=isinstance(manifest.get("task_directory"), str),
            resume_invocation=resume_app if action is not None else None,
        )
    except OpenCodeLaunchError as error:
        if manifest.get("ownership") == "shared":
            return _persist(
                run_dir,
                phase="terminal",
                change_id=change_id,
                opencode=handle,
                status=dict(status.status),
                started_at=started_at,
                ended_at=_iso_now(),
                exit_code=30,
                error=f"shared OpenCode server is unavailable: {error}",
            )
        raise
    except Exception as error:
        previous = read_bootstrap_status(run_dir)
        snapshot = previous.status or status.status
        if prepared is not None:
            try:
                snapshot = status_app(
                    **prepared,
                    project_dir=project_dir,
                    change_id=change_id,
                    invocation_id=change_id,
                    binding_entrypoint="deployment",
                    secrets=(_secret_arg(spec),),
                    reuse_directory=isinstance(manifest.get("task_directory"), str),
                )
            except Exception:
                pass
        cause = error.__cause__ or error.__context__
        detail = str(error) if cause is None else f"{error}: {cause}"
        _persist(
            run_dir,
            phase="terminal",
            change_id=change_id,
            opencode=handle,
            status=snapshot,
            started_at=started_at,
            ended_at=_iso_now(),
            exit_code=40,
            error=detail,
        )
        raise
    finally:
        if handle is not None and handle.ownership != "shared" and _private_server(run_dir):
            stop(handle)
