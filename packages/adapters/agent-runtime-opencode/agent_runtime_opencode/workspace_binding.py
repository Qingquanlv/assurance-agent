from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from agent_runtime_contracts import AgentWorkspaceV1
from agent_runtime_contracts.schema import canonical_digest, canonical_json_bytes
from graph_engine.plugin_api import TaskContext


BINDING_TITLE_PREFIX = "aa-workspace-binding-v1:"
FORBIDDEN_DISCOVERY_FILES = (
    "opencode.json",
    "opencode.jsonc",
    "AGENTS.md",
    "CLAUDE.md",
    "CONTEXT.md",
)
FORBIDDEN_DISCOVERY_DIRS = (".opencode", ".agents")


def reject_isolated_root_discovery(root: Path) -> None:
    resolved = root.resolve()
    for name in FORBIDDEN_DISCOVERY_FILES:
        if (resolved / name).is_file():
            raise ValueError(f"isolated execution root must not contain {name}")
    for name in FORBIDDEN_DISCOVERY_DIRS:
        if (resolved / name).exists():
            raise ValueError(f"isolated execution root must not contain {name}")


def _relative_write_root(context: TaskContext) -> str:
    project = context.project_root.resolve()
    write_root = context.write_root.resolve()
    try:
        relative = write_root.relative_to(project).as_posix()
    except ValueError as error:
        raise ValueError("write_root must be a canonical project-relative path") from error
    if not relative or relative.startswith("/") or "\\" in relative or ".." in relative.split("/"):
        raise ValueError("write_root must be a canonical project-relative path")
    return relative


def workspace_binding_document(
    *,
    session_id: str,
    agent_profile: str,
    project_root: Path,
    write_root: str,
    allowed_outputs: Sequence[str],
    task_id: str,
    attempt: int,
    attempt_id: str,
    read_roots: Sequence[str] = (),
) -> dict[str, object]:
    if not session_id or session_id.strip() != session_id or any(ch.isspace() for ch in session_id):
        raise ValueError("workspace binding requires a provider session id")
    if not agent_profile:
        raise ValueError("workspace binding requires an agent profile")
    if not task_id or attempt < 1 or not attempt_id:
        raise ValueError("workspace binding requires task and attempt identity")
    if not write_root or write_root.startswith("/") or "\\" in write_root or ".." in write_root.split("/"):
        raise ValueError("write_root must be a canonical project-relative path")
    outputs = tuple(allowed_outputs)
    if outputs != tuple(sorted(outputs)) or len(set(outputs)) != len(outputs):
        raise ValueError("allowed outputs must be unique sorted exact logical paths")
    for item in outputs:
        if not item or item.startswith("/") or "\\" in item or ".." in item.split("/"):
            raise ValueError("allowed outputs must be canonical project-relative paths")
    roots = tuple(read_roots)
    if roots != tuple(sorted(roots)) or len(set(roots)) != len(roots):
        raise ValueError("read roots must be unique sorted exact project-relative paths")
    for item in roots:
        if not item or item.startswith("/") or "\\" in item or ".." in item.split("/"):
            raise ValueError("read roots must be canonical project-relative paths")
    payload: dict[str, object] = {
        "schema_version": "1",
        "session_id": session_id,
        "agent_profile": agent_profile,
        "project_root_digest": canonical_digest(str(project_root.resolve())),
        "write_root": write_root,
        "allowed_outputs": list(outputs),
        "read_roots": list(roots),
        "task_id": task_id,
        "attempt": attempt,
        "attempt_id": attempt_id,
    }
    return {**payload, "digest": canonical_digest(payload)}


def workspace_binding_title(
    context: TaskContext,
    workspace: AgentWorkspaceV1,
    session_id: str,
) -> str:
    reject_isolated_root_discovery(context.write_root)
    write_root = _relative_write_root(context)
    if write_root != workspace.write_root:
        raise ValueError("write_root must match the task context")
    identity = context.workspace_identity
    document = workspace_binding_document(
        session_id=session_id,
        agent_profile=workspace.agent_profile,
        project_root=context.project_root,
        write_root=workspace.write_root,
        allowed_outputs=workspace.allowed_outputs,
        task_id=identity.task_id,
        attempt=identity.attempt,
        attempt_id=identity.attempt_id,
        read_roots=workspace.read_roots,
    )
    return BINDING_TITLE_PREFIX + canonical_json_bytes(document).decode("utf-8")


__all__ = [
    "BINDING_TITLE_PREFIX",
    "FORBIDDEN_DISCOVERY_DIRS",
    "FORBIDDEN_DISCOVERY_FILES",
    "reject_isolated_root_discovery",
    "workspace_binding_document",
    "workspace_binding_title",
]
