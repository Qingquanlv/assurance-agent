from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from assurance_product.bootstrap.contracts import BootstrapStatusV1, OpenCodeHandleV1, RunSpecV1
from assurance_product.bootstrap.opencode import OpenCodeLaunchError
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
)
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
) -> BootstrapStatusV1:
    value = BootstrapStatusV1(
        phase=phase,  # type: ignore[arg-type]
        change_id=change_id,
        opencode=opencode,
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

    composition, _audit = _resolve_and_audit(
        product=str(kwargs["product"]),
        binding_dist=str(kwargs["binding_dist"]),
        binding_entrypoint=str(kwargs["binding_entrypoint"]),
        binding_declaration=str(kwargs["binding_declaration"]),
        config_tree=str(kwargs["config_tree"]),
    )
    secrets = kwargs["secrets"]
    authorization = _authorize_secrets(composition, secrets)
    workspace = _bind_workspace(Path(str(kwargs["project_dir"])), str(kwargs["change_id"]), create=False)
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


def _exit_code_for(snapshot: Mapping[str, Any], mapped: str | None) -> int:
    status = str(snapshot.get("status") or mapped or "failed")
    return _EXIT_BY_STATUS.get(status, 40)


def _is_terminal(snapshot: Mapping[str, Any], mapped: str | None) -> bool:
    status = str(snapshot.get("status") or mapped or "")
    return status in _TERMINAL_STATUSES


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
        _result, mapped = run_invocation(
            **common,
            entrypoint=None,
            input_path=None,
        )
        snapshot = dict(read_status(**common))
        if _is_terminal(snapshot, mapped):
            return _persist(
                run_dir,
                phase="terminal",
                change_id=change_id,
                opencode=handle,
                status=snapshot,
                started_at=started_at,
                ended_at=_iso_now(),
                exit_code=_exit_code_for(snapshot, mapped),
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
) -> BootstrapStatusV1:
    from assurance_product.bootstrap.composition import prepare_composition as _prepare_composition

    resolved_change = change_id or derive_bootstrap_change_id(
        stamp=utc_stamp(),
        nonce=secrets.token_hex(4),
    )
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
            handle = start_serve(
                spec=spec,
                project_dir=project_dir,
                run_dir=run_dir,
                environ=environ,
            )
        except (OpenCodeLaunchError, ValueError) as error:
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
        current = _persist(
            run_dir,
            phase="opencode_ready",
            change_id=resolved_change,
            opencode=handle,
            started_at=started_at,
        )
        prepared = prepare(
            project_dir=project_dir,
            run_dir=run_dir,
            spec=spec,
            opencode_endpoint=handle.endpoint,
            change_id=resolved_change,
        )
        write_run_manifest(
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
            },
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
        if handle is not None:
            stop(handle)


def stop_bootstrap(
    run_dir: Path,
    *,
    stop_opencode: Callable[[OpenCodeHandleV1], None] | None = None,
) -> BootstrapStatusV1:
    status = read_bootstrap_status(run_dir)
    if status.phase == "terminal":
        return status
    stop = stop_opencode or _stop_opencode
    if status.opencode is not None:
        stop(status.opencode)
    return _persist(
        run_dir,
        phase="terminal",
        change_id=status.change_id,
        opencode=status.opencode,
        status=status.status,
        started_at=status.started_at,
        ended_at=_iso_now(),
        exit_code=20,
        error=status.error,
    )


def resume_bootstrap(
    run_dir: Path,
    *,
    environ: Mapping[str, str],
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
    try:
        ready(spec.sut.readiness_url, timeout=_SUT_READY_TIMEOUT_SECONDS)
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
        )
    finally:
        if handle is not None:
            stop(handle)
