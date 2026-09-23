from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from agent_runtime_contracts import AgentRunRequest, AgentWorkspaceV1
from agent_runtime_contracts.schema import canonical_digest
from graph_engine.plugin_api import TaskContext


_ACTIVITY_LABEL_MAX = 120
_BINDING_SCHEMA_VERSION = "2"
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


def validate_activity_label(value: str) -> str:
    if (
        not value
        or value != value.strip()
        or len(value) > _ACTIVITY_LABEL_MAX
        or any(character.isspace() and character != " " for character in value)
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("workspace binding requires an activity label")
    return value


def activity_label_for_session(agent_run: AgentRunRequest, node_id: str) -> str:
    """Use the skill heading OpenCode should show for this child session."""
    for part in agent_run.instructions:
        text = part.text_content
        if not text:
            continue
        first = text.splitlines()[0].strip()
        if not first.startswith("# "):
            continue
        candidate = first[2:].strip()
        if candidate:
            return validate_activity_label(candidate)
    return validate_activity_label(node_id)


def child_session_title(*, activity_label: str, task_id: str, attempt: int) -> str:
    """Readable child-session title. The workspace binding lives in session metadata."""
    label = validate_activity_label(activity_label)
    if not task_id or task_id != task_id.strip() or any(character.isspace() for character in task_id):
        raise ValueError("workspace binding requires task and attempt identity")
    if attempt < 1:
        raise ValueError("workspace binding requires task and attempt identity")
    if len(task_id) >= 32 and all(character in "0123456789abcdefABCDEF" for character in task_id):
        compact = f"{task_id[:10]}…"
    elif len(task_id) > 32:
        compact = f"{task_id[:29]}…"
    else:
        compact = task_id
    return f"Assurance · {label} · {compact} · #{attempt}"


def workspace_binding_document(
    *,
    agent_profile: str,
    project_root: Path,
    write_root: str,
    allowed_outputs: Sequence[str],
    task_id: str,
    attempt: int,
    attempt_id: str,
    activity_label: str,
    read_roots: Sequence[str] = (),
) -> dict[str, object]:
    if not agent_profile:
        raise ValueError("workspace binding requires an agent profile")
    if not task_id or attempt < 1 or not attempt_id:
        raise ValueError("workspace binding requires task and attempt identity")
    label = validate_activity_label(activity_label)
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
        "schema_version": _BINDING_SCHEMA_VERSION,
        "activity_label": label,
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


def workspace_binding_for(
    context: TaskContext,
    workspace: AgentWorkspaceV1,
    *,
    activity_label: str,
) -> dict[str, object]:
    reject_isolated_root_discovery(context.write_root)
    write_root = _relative_write_root(context)
    if write_root != workspace.write_root:
        raise ValueError("write_root must match the task context")
    identity = context.workspace_identity
    return workspace_binding_document(
        agent_profile=workspace.agent_profile,
        project_root=context.project_root,
        write_root=workspace.write_root,
        allowed_outputs=workspace.allowed_outputs,
        task_id=identity.task_id,
        attempt=identity.attempt,
        attempt_id=identity.attempt_id,
        activity_label=activity_label,
        read_roots=workspace.read_roots,
    )


__all__ = [
    "FORBIDDEN_DISCOVERY_DIRS",
    "FORBIDDEN_DISCOVERY_FILES",
    "activity_label_for_session",
    "child_session_title",
    "reject_isolated_root_discovery",
    "validate_activity_label",
    "workspace_binding_document",
    "workspace_binding_for",
]
