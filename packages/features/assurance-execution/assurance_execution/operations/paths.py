"""Workspace path authentication for selected tests."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from assurance_execution.contracts.selection import selected_test_file
from assurance_execution.operations.common import InputError


def _safe_component(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise InputError(f"{label} must be a non-empty path component")
    if "\x00" in value or value in {".", ".."} or "/" in value or "\\" in value:
        raise InputError(f"{label} must be a safe path component")
    return value


def canonical_relative(path: str) -> str:
    return selected_test_file(path)


def execution_view_relative(change_id: str, batch_id: str) -> str:
    return (
        f"qa/changes/{_safe_component(change_id, label='change_id')}"
        f"/.staging/execution/{_safe_component(batch_id, label='batch_id')}"
    )


def resolve_canonical_evidence(project: Path, change_id: str, filename: str) -> Path:
    closed = _safe_component(change_id, label="change_id")
    relative = f"qa/changes/{closed}/execution/{filename}"
    path = project.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved = path.resolve()
        resolved.relative_to(project.resolve())
    except ValueError as error:
        raise InputError(f"canonical evidence path escapes the project: {relative}") from error
    return path


def resolve_execution_view(project: Path, change_id: str, batch_id: str) -> Path:
    relative = execution_view_relative(change_id, batch_id)
    path = project.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved = path.resolve()
        resolved.relative_to(project.resolve())
    except ValueError as error:
        raise InputError(f"execution view path escapes the project: {relative}") from error
    if path.is_symlink() or not path.is_dir():
        raise InputError(f"execution view is missing: {relative}")
    return path


def resolve_selected_file(workspace: Path, relative: str) -> Path:
    try:
        file_path = canonical_relative(relative)
    except ValueError as error:
        raise InputError(str(error)) from error
    path = workspace.joinpath(*PurePosixPath(file_path).parts)
    if path.is_symlink():
        raise InputError(f"selected test is not a regular workspace file: {relative}")
    if not path.is_file() or path.is_symlink():
        raise InputError(f"selected test is not a regular workspace file: {relative}")
    if path.stat().st_nlink != 1:
        raise InputError(f"selected test is not a regular single-link file: {relative}")
    try:
        resolved = path.resolve()
        resolved.relative_to(workspace.resolve())
    except ValueError as error:
        raise InputError(f"selected test path escapes the workspace: {relative}") from error
    return path
