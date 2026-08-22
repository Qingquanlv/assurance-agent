"""Canonical relative path checks for improvement commit validators."""

from __future__ import annotations

from pathlib import Path, PurePosixPath


def canonical_relative(path: str) -> bool:
    posix = PurePosixPath(path)
    return not (
        posix.is_absolute()
        or "\\" in path
        or (len(path) >= 2 and path[1] == ":")
        or posix.as_posix() != path
        or any(part in {"", ".", ".."} for part in posix.parts)
    )


def authenticate_workspace_path(workspace: Path, relative: str) -> Path:
    if not canonical_relative(relative):
        raise ValueError(f"path must be canonical and relative: {relative}")
    path = workspace.joinpath(*PurePosixPath(relative).parts)
    if path.is_symlink():
        raise ValueError(f"declared output file is missing: {relative}")
    try:
        resolved = path.resolve()
        resolved.relative_to(workspace.resolve())
    except ValueError as error:
        raise ValueError(f"path must be canonical and relative: {relative}") from error
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"declared output file is missing: {relative}")
    if path.stat().st_nlink != 1:
        raise ValueError(f"declared output file is not a regular single-link file: {relative}")
    return path


def under_root(path: str, roots: tuple[str, ...]) -> bool:
    relative = PurePosixPath(path).as_posix()
    for root in roots:
        prefix = root if root.endswith("/") else f"{root.rstrip('/')}/"
        if relative == root.rstrip("/") or relative.startswith(prefix):
            return True
    return False
