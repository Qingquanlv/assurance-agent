"""Shared root/child invocation start: started event pins plus runtime meta."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from assurance_kernel.workflow.core.graph_events import GraphInvocationStartedEvent
from assurance_kernel.workflow.graph.models import RuntimeContext


def write_invocation_runtime_meta(
    txn: Any,
    invocation_id: str,
    context: RuntimeContext,
    extra: Mapping[str, object] | None = None,
) -> None:
    """Persist host paths for a new invocation. Does not append ledger events."""
    host_root = context.resolved_host_root
    payload: dict[str, object] = {
        "project_root": str(context.project_root),
        "repo_root": str(context.repo_root),
        "host_project_root": str(host_root),
        "change_id": context.change_id,
        "parent_session_id": context.parent_session_id,
    }
    if extra:
        payload.update(dict(extra))
    txn.write_runtime_file(
        f".graph-runtime/invocations/{invocation_id}.json",
        json.dumps(payload, sort_keys=True).encode("utf-8"),
    )


def runtime_context_from_meta(
    *,
    change_dir: Path,
    params: dict[str, object],
    meta: Mapping[str, object] | None,
) -> RuntimeContext:
    """Rebuild RuntimeContext from invocation meta, preserving the host SUT root."""
    project_root = change_dir.parent.parent.parent
    repo_root = project_root
    change_id = change_dir.name
    parent_session_id: str | None = None
    host_project_root: Path | None = None
    if meta:
        raw_project = meta.get("project_root")
        if raw_project is not None:
            project_root = Path(str(raw_project))
        raw_repo = meta.get("repo_root")
        repo_root = Path(str(raw_repo)) if raw_repo is not None else project_root
        change_id = str(meta.get("change_id", change_id))
        raw_session = meta.get("parent_session_id")
        parent_session_id = str(raw_session) if raw_session is not None else None
        raw_host = meta.get("host_project_root")
        if raw_host is not None:
            host_project_root = Path(str(raw_host))
    if host_project_root is None:
        host_project_root = project_root
        try:
            change_dir.resolve().relative_to(host_project_root.resolve())
        except ValueError:
            host_project_root = change_dir.parent.parent.parent
    return RuntimeContext(
        project_root=project_root,
        repo_root=repo_root,
        change_dir=change_dir,
        change_id=change_id,
        params=params,
        parent_session_id=parent_session_id,
        host_project_root=host_project_root,
    )


def append_invocation_bootstrap(
    txn: Any,
    *,
    started: GraphInvocationStartedEvent,
    context: RuntimeContext,
    stage_pins: Callable[[Any], None],
    extra_meta: Mapping[str, object] | None = None,
) -> None:
    """Append started, pin definitions, and write invocation meta in one txn."""
    txn.append_strict(started)
    stage_pins(txn)
    write_invocation_runtime_meta(txn, started.invocation_id, context, extra_meta)
