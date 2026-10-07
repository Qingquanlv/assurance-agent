"""Product entrypoint adapters for the shared worker admission protocol."""

from __future__ import annotations
import asyncio
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any
from assurance_product.worker_lifecycle import acquire_execution, reserve_preparation, request_stop
from assurance_product.worker_cleanup import cleanup_owned_resources


def run_workspace(run_dir: Path) -> tuple[Path, str]:
    from assurance_product.bootstrap.status import read_run_manifest

    manifest = read_run_manifest(run_dir)
    from assurance_product.bootstrap.status import read_bootstrap_status

    invocation = manifest.get("invocation_id") or read_bootstrap_status(run_dir).change_id
    return Path(str(manifest["project_dir"])), str(invocation)


def exclusive_application(function: Callable[..., Any]) -> Callable[..., Any]:
    from functools import wraps

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        workspace = kwargs["workspace"]
        with acquire_execution(workspace.paths.project_root, kwargs["invocation_id"]):
            return function(*args, **kwargs)

    return wrapped


def exclusive_resume(function: Callable[..., Any]) -> Callable[..., Any]:
    from functools import wraps

    @wraps(function)
    def wrapped(run_dir: Path, *args: Any, **kwargs: Any) -> Any:
        workspace, invocation = run_workspace(run_dir)
        with acquire_execution(workspace, invocation, run_dir=run_dir):
            return function(run_dir, *args, **kwargs)

    return wrapped


def exclusive_bootstrap(function: Callable[..., Any]) -> Callable[..., Any]:
    from functools import wraps

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        from assurance_product.bootstrap.status import derive_bootstrap_change_id
        from assurance_product.bootstrap.driver import utc_stamp
        from assurance_product.sut_worktree import ensure_run_worktree, resolve_run_worktree

        invocation = kwargs.get("change_id") or derive_bootstrap_change_id(
            stamp=utc_stamp(), nonce=secrets.token_hex(4)
        )
        kwargs["change_id"] = invocation
        workspace = kwargs.get("task_directory")
        if workspace is not None and not workspace.is_dir():
            raise ValueError("task directory does not exist")
        target = workspace or resolve_run_worktree(kwargs["project_dir"], invocation)
        with reserve_preparation(target):
            if workspace is None:
                workspace = ensure_run_worktree(kwargs["project_dir"], invocation)
            with acquire_execution(workspace, invocation, run_dir=kwargs["runs_root"] / invocation):
                return function(*args, **kwargs)

    return wrapped


def stop_run(run_dir: Path, *, force: bool = False, timeout: float = 2, change_id: str | None = None) -> str:
    from assurance_product.bootstrap.status import write_stop_request, read_bootstrap_status
    from assurance_product.retained_host import confirm_owned_calls

    invocation = change_id or read_bootstrap_status(run_dir).change_id
    write_stop_request(run_dir, change_id=invocation)
    if not force:
        return "stopping"
    workspace, recorded_invocation = run_workspace(run_dir)
    if recorded_invocation != invocation:
        return "unconfirmed"
    return request_stop(
        workspace,
        force=force,
        timeout=timeout,
        confirm_external=lambda owner: asyncio.run(confirm_owned_calls(owner)),
        cleanup=cleanup_owned_resources,
        expected_invocation=invocation,
    )


def exclusive_cli(function: Callable[..., Any]) -> Callable[..., Any]:
    from functools import wraps

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        from assurance_product.cli import _engine_failures, _project_for_run

        from assurance_product.sut_worktree import resolve_run_worktree
        from assurance_product.cli import CommandError, require_real_directory

        workspace = kwargs["project_dir"]
        prepare = function.__name__ != "_resume_invocation" and not kwargs.get("reuse_directory", False)
        with _engine_failures():
            try:
                target = (
                    resolve_run_worktree(require_real_directory(workspace), kwargs["change_id"])
                    if prepare
                    else workspace
                )
            except ValueError as error:
                raise CommandError(str(error)) from error
            with reserve_preparation(target):
                if prepare:
                    workspace = _project_for_run(workspace, kwargs["change_id"])
                    kwargs["project_dir"] = workspace
                    kwargs["reuse_directory"] = True
                with acquire_execution(workspace, kwargs["invocation_id"]):
                    return function(*args, **kwargs)

    return wrapped
