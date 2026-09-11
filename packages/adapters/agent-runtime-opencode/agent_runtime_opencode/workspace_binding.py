from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from agent_runtime_contracts import AgentWorkspaceV1
from agent_runtime_contracts.schema import canonical_digest, canonical_json_bytes
from graph_engine.plugin_api import StagedFile, TaskContext


BINDING_TITLE_PREFIX = "aa-workspace-binding-v1:"


def _logical_parts(value: str) -> tuple[str, ...]:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("allowed output must be a canonical project-relative path")
    return path.parts


def _open_root(path: Path, label: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(f"{label} is missing or invalid") from error
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError(f"{label} is missing or invalid")
    return descriptor


def _open_parent(root_fd: int, parts: tuple[str, ...], *, create: bool, label: str) -> int:
    current = os.dup(root_fd)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, flags, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=0o700, dir_fd=current)
                next_fd = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = next_fd
        return current
    except OSError as error:
        os.close(current)
        raise ValueError(f"{label} has an invalid parent") from error


def _read_regular_at(parent_fd: int, name: str, *, label: str) -> tuple[bytes, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        raise ValueError(f"{label} is missing or invalid") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError(f"{label} is missing or invalid")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks), stat.S_IMODE(metadata.st_mode)
    finally:
        os.close(descriptor)


def _write_exclusive_at(parent_fd: int, name: str, contents: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, mode, dir_fd=parent_fd)
    try:
        remaining = memoryview(contents)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short baseline write")
            remaining = remaining[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        try:
            os.unlink(name, dir_fd=parent_fd)
        except OSError:
            pass
        raise
    else:
        os.close(descriptor)


def materialize_allowed_baselines(
    *,
    project_root: Path,
    write_root: Path,
    baseline_files: Sequence[StagedFile],
    allowed_outputs: Sequence[str],
) -> None:
    """Copy authenticated exact provider outputs into the private attempt overlay."""

    baselines = {item.path: item for item in baseline_files}
    project_fd = _open_root(project_root, "project root")
    write_fd = _open_root(write_root, "write root")
    try:
        for logical in allowed_outputs:
            parts = _logical_parts(logical)
            baseline = baselines.get(logical)
            if baseline is None:
                continue
            if baseline.before_sha256 is None or baseline.before_mode is None:
                raise ValueError("workspace baseline identity is invalid")
            source_parent = _open_parent(
                project_fd,
                parts,
                create=False,
                label="baseline source",
            )
            try:
                contents, mode = _read_regular_at(
                    source_parent,
                    parts[-1],
                    label="baseline source",
                )
            finally:
                os.close(source_parent)
            if hashlib.sha256(contents).hexdigest() != baseline.before_sha256 or mode != baseline.before_mode:
                raise ValueError("baseline source drifted from the workspace identity")
            destination_parent = _open_parent(
                write_fd,
                parts,
                create=True,
                label="staged baseline",
            )
            try:
                try:
                    existing, existing_mode = _read_regular_at(
                        destination_parent,
                        parts[-1],
                        label="staged baseline",
                    )
                except ValueError as error:
                    if "missing or invalid" not in str(error):
                        raise
                    try:
                        _write_exclusive_at(destination_parent, parts[-1], contents, mode)
                    except FileExistsError:
                        existing, existing_mode = _read_regular_at(
                            destination_parent,
                            parts[-1],
                            label="staged baseline",
                        )
                    else:
                        continue
                if existing != contents or existing_mode != mode:
                    raise ValueError("staged baseline drifted before prompt admission")
            finally:
                os.close(destination_parent)
    finally:
        os.close(write_fd)
        os.close(project_fd)


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
    payload: dict[str, object] = {
        "schema_version": "1",
        "session_id": session_id,
        "agent_profile": agent_profile,
        "project_root_digest": canonical_digest(str(project_root.resolve())),
        "write_root": write_root,
        "allowed_outputs": list(outputs),
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
    )
    return BINDING_TITLE_PREFIX + canonical_json_bytes(document).decode("utf-8")


__all__ = [
    "BINDING_TITLE_PREFIX",
    "materialize_allowed_baselines",
    "workspace_binding_document",
    "workspace_binding_title",
]
