"""Authentication of the workspace files an Agent receipt names."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath

from agent_runtime_contracts.ops import ArtifactListResultV1, InputError, OutputError

from assurance_intake.contracts.common import is_canonical_relative


def leafs(values: Iterable[str]) -> frozenset[str]:
    return frozenset(values)


def file_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def workspace_file(workspace: Path, relative: str) -> Path:
    if not is_canonical_relative(relative):
        raise OutputError(f"output file path must be canonical and relative: {relative}")
    path = workspace
    for part in PurePosixPath(relative).parts:
        path = path / part
        if path.is_symlink():
            raise OutputError(f"declared output file is a symlink: {relative}")
    if path.is_symlink():
        raise OutputError(f"declared output file is missing: {relative}")
    try:
        path.resolve().relative_to(workspace.resolve())
    except ValueError as error:
        raise OutputError(f"output file path must be canonical and relative: {relative}") from error
    if path.is_file() and not path.is_symlink() and path.stat().st_nlink != 1:
        raise OutputError(f"declared output file is not a regular single-link file: {relative}")
    return path


def read_regular_bytes(workspace: Path, relative: str, *, kind: str) -> bytes:
    path = workspace_file(workspace, relative)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise OutputError(f"{kind} is missing or is not a regular file: {relative}") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise OutputError(f"{kind} is missing or is not a regular file: {relative}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    before_signature = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_signature = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if before_signature != after_signature or len(data) != before.st_size:
        raise OutputError(f"{kind} changed while being read: {relative}")
    return data


def allowed_by_lock(relative: str, locked: tuple[str, ...]) -> bool:
    return any(relative == prefix or relative.startswith(f"{prefix}/") for prefix in locked)


def authenticate_files(
    workspace: Path,
    declared: tuple[str, ...],
    locked: tuple[str, ...],
) -> list[dict[str, str]]:
    if not locked:
        raise InputError("artifact_paths must lock the expected output files")
    artifacts: list[dict[str, str]] = []
    for relative in declared:
        if not allowed_by_lock(relative, locked):
            raise OutputError(f"undeclared output file: {relative}")
        path = workspace_file(workspace, relative)
        if not path.is_file() or path.is_symlink():
            raise OutputError(f"declared output file is missing: {relative}")
        artifacts.append({"path": relative, "digest": file_digest(path.read_bytes())})
    return artifacts


def authenticate_review_repair_images(
    images: Mapping[str, bytes],
    declared: tuple[str, ...],
    locked: tuple[str, ...],
) -> list[dict[str, str]]:
    if not locked:
        raise InputError("artifact_paths must lock the expected output files")
    artifacts: list[dict[str, str]] = []
    for relative in declared:
        if not allowed_by_lock(relative, locked):
            raise OutputError(f"undeclared output file: {relative}")
        data = images.get(relative)
        if data is None:
            raise OutputError(f"declared output file is missing: {relative}")
        artifacts.append({"path": relative, "digest": file_digest(data)})
    return artifacts


def case_change_id(value: str | None) -> str:
    if value is None:
        raise InputError("change_id is required for case-design finalize")
    posix = PurePosixPath(value)
    if len(posix.parts) != 1 or not is_canonical_relative(value):
        raise InputError("change_id must be a canonical path segment")
    return value


def authenticate_receipt(
    workspace: Path,
    receipt: ArtifactListResultV1,
    locked: tuple[str, ...],
) -> list[dict[str, str]]:
    if not locked:
        raise InputError("artifact_paths must lock the expected output files")
    return authenticate_files(workspace, receipt.output_files, locked)
