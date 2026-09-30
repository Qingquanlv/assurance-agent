from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from agent_runtime_contracts import AgentRunRequest, AgentWorkspaceV1
from agent_runtime_contracts.wire.schema import canonical_digest, thaw_json
from graph_engine.plugin_api import (
    TaskActivityPort,
    TaskActivityReconcileResult,
    TaskActivitySnapshot,
    TaskContext,
    TaskRequest,
)

from agent_runtime_opencode.session.discovery import (
    OpenCodeActivityReference,
    _bind_match,
    discovery_metadata_from_record,
    metadata_match_digest,
)
from agent_runtime_opencode.session.identity import _reference_drifted
from agent_runtime_opencode.transport.connection import PROVIDER_ERRORS
from agent_runtime_opencode.transport.http import (
    OpenCodeDiscoveryMetadata,
    OpenCodeHttpClient,
    OpenCodeSessionCreateRequest,
)


_ACTIVITY_LABEL_MAX = 120
_BINDING_INVALID = "workspace binding is missing or invalid"
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


async def _create_and_bind(
    client: OpenCodeHttpClient,
    port: TaskActivityPort,
    request: TaskRequest,
    context: TaskContext,
    metadata: OpenCodeDiscoveryMetadata,
    expected: dict[str, str],
    *,
    agent_run: AgentRunRequest,
) -> TaskActivityReconcileResult:
    try:
        label = activity_label_for_session(agent_run, request.node_id)
        binding = workspace_binding_for(
            context,
            agent_run.workspace,
            activity_label=label,
        )
        title = child_session_title(
            activity_label=label,
            task_id=context.workspace_identity.task_id,
            attempt=context.workspace_identity.attempt,
        )
        body = OpenCodeSessionCreateRequest.model_validate(
            {
                "title": title,
                "metadata": {
                    "discovery": metadata.model_dump(mode="json"),
                    "workspace_binding": binding,
                },
                "agent": agent_run.workspace.agent_profile,
                "parentID": expected.get("parent_session_id"),
            }
        )
    except ValueError:
        return TaskActivityReconcileResult(
            status="indeterminate",
            reason=_BINDING_INVALID,
        )
    payload = body.model_dump(mode="json", exclude_none=True)
    if "id" in payload:
        raise ValueError("create must not supply a session id")
    record = await client.create_session(payload)
    session_id = record.get("id") if isinstance(record, dict) else None
    if isinstance(session_id, str) and session_id:
        verified = await _verify_workspace_binding(
            client,
            agent_run,
            context,
            session_id,
            activity_label=label,
            check_title=True,
        )
        if verified is not None:
            _bind_match(port, record, expected)
            return verified
    return _bind_match(port, record, expected)


async def _load_bound_session(
    client: OpenCodeHttpClient,
    snapshot: TaskActivitySnapshot,
    expected: dict[str, str],
) -> TaskActivityReconcileResult | tuple[OpenCodeActivityReference, dict[str, Any]]:
    try:
        reference = OpenCodeActivityReference.model_validate(thaw_json(snapshot.reference))
    except ValidationError:
        return TaskActivityReconcileResult(
            status="indeterminate",
            reason="bound reference is not authentic",
        )
    if _reference_drifted(reference, expected):
        return TaskActivityReconcileResult(
            status="indeterminate",
            reason="bound reference identity drifted",
        )
    if not reference.session_id:
        return TaskActivityReconcileResult(
            status="indeterminate",
            reason="bound session identity is unknown",
        )
    try:
        record = await client.get_session(reference.session_id)
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            return TaskActivityReconcileResult(
                status="indeterminate",
                reason="formerly bound session is missing",
            )
        raise
    parent = record.get("parentID") if isinstance(record, dict) else None
    expected_parent = reference.parent_session_id
    if expected_parent:
        if parent != expected_parent:
            return TaskActivityReconcileResult(
                status="indeterminate",
                reason="child session parent does not match the run root",
            )
        if reference.worktree:
            directory = record.get("directory") if isinstance(record, dict) else None
            if (
                not isinstance(directory, str)
                or not directory
                or Path(directory).resolve() != Path(reference.worktree).resolve()
            ):
                return TaskActivityReconcileResult(
                    status="indeterminate",
                    reason="child session worktree does not match the run",
                )
    elif isinstance(parent, str) and parent:
        return TaskActivityReconcileResult(
            status="indeterminate",
            reason="parentID reconnect is forbidden",
        )
    if not isinstance(record, dict) or record.get("id") != reference.session_id:
        return TaskActivityReconcileResult(
            status="indeterminate",
            reason="session identity drifted",
        )
    try:
        observed = discovery_metadata_from_record(record)
    except (ValidationError, ValueError):
        return TaskActivityReconcileResult(
            status="indeterminate",
            reason="foreign session metadata",
        )
    if metadata_match_digest(observed) != reference.metadata_match_digest:
        return TaskActivityReconcileResult(
            status="indeterminate",
            reason="foreign session metadata",
        )
    return reference, record


async def _verify_workspace_binding(
    client: OpenCodeHttpClient,
    agent_run: AgentRunRequest,
    context: TaskContext,
    session_id: str,
    *,
    activity_label: str,
    check_title: bool,
) -> TaskActivityReconcileResult | None:
    try:
        expected = workspace_binding_for(
            context,
            agent_run.workspace,
            activity_label=activity_label,
        )
        title = child_session_title(
            activity_label=activity_label,
            task_id=context.workspace_identity.task_id,
            attempt=context.workspace_identity.attempt,
        )
        agent = agent_run.workspace.agent_profile
        record = await client.get_session(session_id)
        metadata = record.get("metadata") if isinstance(record, dict) else None
        binding = metadata.get("workspace_binding") if isinstance(metadata, dict) else None
        if (
            not isinstance(record, dict)
            or record.get("id") != session_id
            or record.get("agent") != agent
            or binding != expected
            or (check_title and record.get("title") != title)
        ):
            raise ValueError(_BINDING_INVALID)
    except PROVIDER_ERRORS:
        return TaskActivityReconcileResult(
            status="indeterminate",
            reason=_BINDING_INVALID,
        )
    return None


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
