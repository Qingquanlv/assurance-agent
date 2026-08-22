"""Workspace path authentication for selected tests."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from assurance_execution.contracts.selection import _safe_project_relative_path
from assurance_execution.operations.common import InputError


def canonical_relative(path: str) -> str:
    return _safe_project_relative_path(path)


def resolve_selected_file(workspace: Path, relative: str) -> Path:
    try:
        canonical_relative(relative)
    except ValueError as error:
        raise InputError(str(error)) from error
    path = workspace.joinpath(*PurePosixPath(relative).parts)
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
