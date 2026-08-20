from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from graph_engine.composition.models import (
    SourceFile,
    SourceKind,
    SourceSnapshot,
    _validate_canonical_relative_path,
)
from graph_engine.errors import GraphEngineError


_READ_BUFFER_SIZE = 1024 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW
_FILE_READ_FLAGS = os.O_RDONLY | _NOFOLLOW | _NONBLOCK
_EXECUTABLE_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
_EntryState: TypeAlias = tuple[int, int, int, int, int, int, int]


class SourceSnapshotError(GraphEngineError):
    """Raised when an explicit source tree cannot be captured exactly."""


@dataclass(frozen=True, slots=True)
class DeclaredTreePolicy:
    kind: SourceKind

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SourceKind):
            raise TypeError("declared tree policy kind must be a SourceKind")

    @property
    def require_single_link(self) -> bool:
        return self.kind in {SourceKind.CONFIG_TREE, SourceKind.EDITABLE_PLUGIN}

    @property
    def reject_executable_files(self) -> bool:
        return self.kind == SourceKind.CONFIG_TREE

    @classmethod
    def config_tree(cls) -> DeclaredTreePolicy:
        return cls(kind=SourceKind.CONFIG_TREE)

    @classmethod
    def editable(cls) -> DeclaredTreePolicy:
        return cls(kind=SourceKind.EDITABLE_PLUGIN)


@dataclass(frozen=True, slots=True)
class _TreeState:
    files: tuple[tuple[str, _EntryState], ...]
    directories: tuple[tuple[str, _EntryState], ...]

    @property
    def file_names(self) -> frozenset[str]:
        return frozenset(path for path, _state in self.files)


def capture_declared_tree(
    root: Path,
    files: tuple[str, ...],
    policy: DeclaredTreePolicy,
) -> SourceSnapshot:
    root_fd = _open_physical_directory(root)
    try:
        normalized = _validate_closed_file_list(files)
        _snapshot_boundary("before_enumeration", None)
        before_tree = _enumerate_regular_tree(root_fd, policy)
        _snapshot_boundary("after_enumeration", None)
        if before_tree.file_names != set(normalized):
            raise SourceSnapshotError("declared source file set does not match the physical tree")
        captured = tuple(_read_stable_file_at(root_fd, path, policy) for path in normalized)
        _snapshot_boundary("before_rescan", None)
        after_tree = _enumerate_regular_tree(root_fd, policy)
        _snapshot_boundary("after_rescan", None)
        if after_tree != before_tree:
            raise SourceSnapshotError("source tree changed while it was captured")
        resolved_root = _resolve_stable_root(root, root_fd)
        return SourceSnapshot.from_files(policy.kind, resolved_root, captured)
    finally:
        _close_descriptors_preserving_primary((root_fd,), sys.exception())


def _validate_closed_file_list(files: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in files:
        try:
            canonical = _validate_canonical_relative_path(candidate)
        except (TypeError, ValueError) as error:
            raise SourceSnapshotError(f"unsafe declared source path: {candidate!r}") from error
        if canonical in seen:
            raise SourceSnapshotError(f"duplicate declared source path: {canonical!r}")
        seen.add(canonical)
        normalized.append(canonical)
    return tuple(sorted(normalized))


def _open_physical_directory(root: Path) -> int:
    _require_descriptor_relative_posix()
    absolute = root.absolute()
    try:
        descriptor = os.open("/", _DIRECTORY_FLAGS)
    except OSError as error:
        raise SourceSnapshotError("cannot open source root as a no-follow directory") from error
    relative = ""
    for component in absolute.parts[1:]:
        relative = f"{relative}/{component}" if relative else component
        try:
            child = _open_directory_at(descriptor, component, relative)
        except BaseException:
            _close_descriptors_preserving_primary((descriptor,), sys.exception())
            raise
        try:
            _close_descriptor(descriptor)
        except BaseException:
            _close_descriptors_preserving_primary((child,), sys.exception())
            raise
        descriptor = child
    return descriptor


def _open_directory_at(parent_fd: int, name: str, relative_path: str) -> int:
    try:
        enumerated = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _snapshot_boundary("before_component_open", relative_path)
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        raise SourceSnapshotError(
            f"source path component must be a no-follow directory: {relative_path}"
        ) from error
    try:
        try:
            opened = os.fstat(descriptor)
        except OSError as error:
            raise SourceSnapshotError(
                f"cannot authenticate source directory: {relative_path}"
            ) from error
        if not stat.S_ISDIR(opened.st_mode) or _entry_identity(opened) != _entry_identity(
            enumerated
        ):
            raise SourceSnapshotError(
                f"source directory identity changed while opening: {relative_path}"
            )
        _snapshot_boundary("after_component_open", relative_path)
    except BaseException:
        _close_descriptors_preserving_primary((descriptor,), sys.exception())
        raise
    return descriptor


def _enumerate_regular_tree(
    root_fd: int,
    policy: DeclaredTreePolicy,
    *,
    prefix: str = "",
) -> _TreeState:
    try:
        names = os.listdir(root_fd)
    except OSError as error:
        raise SourceSnapshotError("cannot enumerate declared source tree") from error
    files: list[tuple[str, _EntryState]] = []
    directories: list[tuple[str, _EntryState]] = []
    for name in sorted(names):
        _validate_directory_entry_name(name)
        relative_path = f"{prefix}/{name}" if prefix else name
        try:
            entry = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except OSError as error:
            raise SourceSnapshotError(
                f"source tree entry changed during enumeration: {relative_path}"
            ) from error
        if stat.S_ISDIR(entry.st_mode):
            directories.append((relative_path, _complete_entry_state(entry)))
            child_fd = _open_directory_at(root_fd, name, relative_path)
            try:
                child = _enumerate_regular_tree(child_fd, policy, prefix=relative_path)
                files.extend(child.files)
                directories.extend(child.directories)
            finally:
                _close_descriptors_preserving_primary((child_fd,), sys.exception())
            continue
        if not stat.S_ISREG(entry.st_mode):
            raise SourceSnapshotError(
                f"source tree entry is not a no-follow regular file or directory: {relative_path}"
            )
        _require_allowed_link_count(entry, relative_path, policy)
        _require_allowed_mode(entry, relative_path, policy)
        files.append((relative_path, _complete_entry_state(entry)))
    return _TreeState(files=tuple(files), directories=tuple(directories))


def _read_stable_file_at(
    root_fd: int,
    relative_path: str,
    policy: DeclaredTreePolicy,
) -> SourceFile:
    components = relative_path.split("/")
    opened_directories: list[int] = []
    parent_fd = root_fd
    descriptor: int | None = None
    try:
        prefix = ""
        for component in components[:-1]:
            prefix = f"{prefix}/{component}" if prefix else component
            child_fd = _open_directory_at(parent_fd, component, prefix)
            opened_directories.append(child_fd)
            parent_fd = child_fd
        name = components[-1]
        try:
            enumerated = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise SourceSnapshotError(
                f"cannot safely inspect regular file: {relative_path}"
            ) from error
        _require_regular_file(enumerated, relative_path, policy)
        _snapshot_boundary("before_component_open", relative_path)
        try:
            descriptor = os.open(name, _FILE_READ_FLAGS, dir_fd=parent_fd)
        except OSError as error:
            raise SourceSnapshotError(
                f"cannot safely open regular file: {relative_path}"
            ) from error
        opened = os.fstat(descriptor)
        _require_regular_file(opened, relative_path, policy)
        if _entry_identity(opened) != _entry_identity(enumerated):
            raise SourceSnapshotError(f"source file changed while opening: {relative_path}")
        _snapshot_boundary("after_component_open", relative_path)
        _snapshot_boundary("before_read", relative_path)
        chunks: list[bytes] = []
        try:
            while chunk := os.read(descriptor, _READ_BUFFER_SIZE):
                chunks.append(chunk)
        except OSError as error:
            raise SourceSnapshotError(f"cannot read source file: {relative_path}") from error
        _snapshot_boundary("after_read", relative_path)
        _snapshot_boundary("before_final_stat", relative_path)
        try:
            final_opened = os.fstat(descriptor)
        except OSError as error:
            raise SourceSnapshotError(
                f"source file changed while it was read: {relative_path}"
            ) from error
        try:
            final_entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise SourceSnapshotError(
                f"source file changed while it was read: {relative_path}"
            ) from error
        _snapshot_boundary("after_final_stat", relative_path)
        if not _stable_file_stats(enumerated, opened, final_opened, final_entry):
            raise SourceSnapshotError(f"source file changed while it was read: {relative_path}")
        _require_regular_file(final_opened, relative_path, policy)
        _require_regular_file(final_entry, relative_path, policy)
        return SourceFile.from_bytes(relative_path, b"".join(chunks))
    finally:
        descriptors = (() if descriptor is None else (descriptor,)) + tuple(
            reversed(opened_directories)
        )
        _close_descriptors_preserving_primary(descriptors, sys.exception())


def _require_regular_file(
    entry: os.stat_result,
    relative_path: str,
    policy: DeclaredTreePolicy,
) -> None:
    if not stat.S_ISREG(entry.st_mode):
        raise SourceSnapshotError(f"source path is not a regular file: {relative_path}")
    _require_allowed_link_count(entry, relative_path, policy)
    _require_allowed_mode(entry, relative_path, policy)


def _require_allowed_link_count(
    entry: os.stat_result,
    relative_path: str,
    policy: DeclaredTreePolicy,
) -> None:
    if policy.require_single_link and entry.st_nlink != 1:
        raise SourceSnapshotError(f"hard link is not allowed: {relative_path}")


def _require_allowed_mode(
    entry: os.stat_result,
    relative_path: str,
    policy: DeclaredTreePolicy,
) -> None:
    if policy.reject_executable_files and entry.st_mode & _EXECUTABLE_BITS:
        raise SourceSnapshotError(f"executable file is not allowed: {relative_path}")


def _stable_file_stats(*entries: os.stat_result) -> bool:
    first = _complete_entry_state(entries[0])
    return all(_complete_entry_state(entry) == first for entry in entries[1:])


def _entry_identity(entry: os.stat_result) -> tuple[int, int]:
    return (entry.st_dev, entry.st_ino)


def _complete_entry_state(entry: os.stat_result) -> _EntryState:
    return (
        entry.st_dev,
        entry.st_ino,
        entry.st_size,
        entry.st_mtime_ns,
        entry.st_ctime_ns,
        entry.st_mode,
        entry.st_nlink,
    )


def _resolve_stable_root(root: Path, root_fd: int) -> Path:
    try:
        resolved = root.resolve(strict=True)
    except OSError as error:
        raise SourceSnapshotError("source root changed while it was captured") from error
    verification_fd = _open_physical_directory(resolved)
    try:
        try:
            captured = os.fstat(root_fd)
            current = os.fstat(verification_fd)
        except OSError as error:
            raise SourceSnapshotError("source root changed while it was captured") from error
        if _entry_identity(captured) != _entry_identity(current):
            raise SourceSnapshotError("source root changed while it was captured")
    finally:
        _close_descriptors_preserving_primary((verification_fd,), sys.exception())
    return resolved


def _validate_directory_entry_name(name: str) -> None:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError as error:
        raise SourceSnapshotError(f"source tree contains a non-UTF-8 name: {name!r}") from error
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\0" in name:
        raise SourceSnapshotError(f"source tree contains an unsafe name: {name!r}")


def _require_descriptor_relative_posix() -> None:
    if (
        _NOFOLLOW == 0
        or _NONBLOCK == 0
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
    ):
        raise SourceSnapshotError(
            "declared source capture requires POSIX descriptor-relative no-follow operations"
        )


def _close_descriptors_preserving_primary(
    descriptors: tuple[int, ...],
    active_exception: BaseException | None,
) -> None:
    cleanup_error: BaseException | None = None
    for descriptor in descriptors:
        try:
            _close_descriptor(descriptor)
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = error
    if active_exception is None and cleanup_error is not None:
        raise cleanup_error


def _close_descriptor(descriptor: int) -> None:
    os.close(descriptor)


def _snapshot_boundary(phase: str, relative_path: str | None) -> None:
    del phase, relative_path


__all__ = [
    "DeclaredTreePolicy",
    "SourceSnapshotError",
    "capture_declared_tree",
]
