from __future__ import annotations

import hashlib
import os
import stat
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from graph_engine.errors import GraphEngineError
from graph_engine.plugin_api import FrozenModel
from graph_engine.runtime.seed import (
    SeedFile,
    WorkspaceSeed,
    _validate_relative_canonical_path,
    workspace_tree_id,
)
from graph_engine.runtime.workspace import (
    SnapshotStore,
    WorkspaceViolation,
    _OpenedTree,
    _assert_entry_identity,
    _assert_open_file_stable,
    _entry_exists,
    _open_absolute_directory,
    _open_directory_at,
    _open_file_at,
    _read_opened_tree_file,
    _remove_entry_at,
    _validate_relative_path,
    _validate_tree_id,
    _validated_names,
    _write_all,
)

_COPY_BUFFER_SIZE = 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)


class SeedCaptureError(GraphEngineError):
    """Raised when a workspace tree cannot be captured exactly."""


class SnapshotExportError(GraphEngineError):
    """Raised when a snapshot tree cannot be exported exactly."""


class SeedCapturePolicy(FrozenModel):
    excluded_names: tuple[str, ...] = (
        ".git",
        ".aa-runtime",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "benchmark-results",
        ".env",
    )
    maximum_file_count: int = 100_000
    maximum_file_bytes: int = 16 * 1024 * 1024
    maximum_total_bytes: int = 512 * 1024 * 1024


@dataclass(frozen=True)
class ExportedFile:
    path: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        _validate_relative_path(self.path)
        _validate_tree_id(self.sha256, "exported file sha256")
        if self.size_bytes < 0:
            raise SnapshotExportError("exported file size must be non-negative")


@dataclass(frozen=True)
class ExportManifest:
    tree_id: str
    destination: str
    files: tuple[ExportedFile, ...]

    def __post_init__(self) -> None:
        _validate_tree_id(self.tree_id)
        if not self.destination:
            raise SnapshotExportError("export destination must not be empty")
        paths = [item.path for item in self.files]
        if len(paths) != len(set(paths)):
            raise SnapshotExportError("duplicate exported path")
        digest_map = {item.path: item.sha256 for item in self.files}
        if workspace_tree_id(digest_map) != self.tree_id:
            raise SnapshotExportError("export manifest tree_id does not match exported files")


def capture_workspace_seed(source: Path, *, policy: SeedCapturePolicy) -> WorkspaceSeed:
    files = tuple(_capture_regular_files_beneath(source, policy))
    paths = [item.path for item in files]
    if len(paths) != len(set(paths)):
        raise SeedCaptureError("duplicate workspace seed path")
    digest_map = {item.path: item.sha256 for item in files}
    return WorkspaceSeed(
        schema_version="1",
        tree_id=workspace_tree_id(digest_map),
        files=files,
    )


def materialize_snapshot(store: SnapshotStore, tree_id: str, destination: Path) -> ExportManifest:
    validated_tree_id = _validate_tree_id(tree_id)
    absolute_destination = destination.absolute()
    parent_path = absolute_destination.parent
    export_name = absolute_destination.name
    if not export_name or export_name in {".", ".."}:
        raise SnapshotExportError("export destination must name one path component")

    parent_fd = _open_absolute_directory(parent_path)
    staging_name: str | None = None
    staging_fd: int | None = None
    installed = False
    try:
        if _entry_exists(parent_fd, export_name):
            raise SnapshotExportError(f"destination already exists: {absolute_destination}")
        with store._opened_layout() as (_root_fd, trees_fd, _attempts_fd, _lock_fd):
            tree = store._open_tree(trees_fd, validated_tree_id)
            try:
                manifest = _validate_snapshot_manifest(tree.manifest)
                staging_name = f".export-staging-{uuid.uuid4().hex}"
                os.mkdir(staging_name, mode=0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
                staging_fd, _ = _open_directory_at(parent_fd, staging_name, "export staging directory")
                exported = _write_snapshot_tree(staging_fd, tree, manifest)
            finally:
                os.close(tree.descriptor)
        os.fsync(staging_fd)
        os.rename(
            staging_name,
            export_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        installed = True
        os.fsync(parent_fd)
        return ExportManifest(
            tree_id=validated_tree_id,
            destination=os.fsdecode(absolute_destination),
            files=exported,
        )
    except WorkspaceViolation as error:
        raise SnapshotExportError(str(error)) from error
    except OSError as error:
        raise SnapshotExportError(f"cannot export snapshot tree: {error}") from error
    finally:
        if staging_fd is not None:
            try:
                os.close(staging_fd)
            except OSError:
                pass
        if staging_name is not None and not installed:
            try:
                if _entry_exists(parent_fd, staging_name):
                    _remove_entry_at(parent_fd, staging_name)
                    os.fsync(parent_fd)
            except OSError:
                pass
        os.close(parent_fd)


def _capture_regular_files_beneath(source: Path, policy: SeedCapturePolicy) -> Iterator[SeedFile]:
    _require_posix_primitives()
    root_fd = _open_absolute_directory(source.absolute())
    captured: list[SeedFile] = []
    total_bytes = 0
    try:
        for seed_file in _walk_capture(root_fd, policy, prefix=""):
            if len(captured) + 1 > policy.maximum_file_count:
                raise SeedCaptureError("capture exceeds maximum_file_count")
            total_bytes += len(seed_file.content)
            if len(seed_file.content) > policy.maximum_file_bytes:
                raise SeedCaptureError("capture exceeds maximum_file_bytes")
            if total_bytes > policy.maximum_total_bytes:
                raise SeedCaptureError("capture exceeds maximum_total_bytes")
            captured.append(seed_file)
    except WorkspaceViolation as error:
        message = str(error)
        if "symlink" in message:
            raise SeedCaptureError(message) from error
        raise SeedCaptureError(message) from error
    finally:
        os.close(root_fd)
    for item in sorted(captured, key=lambda value: value.path):
        yield item


def _walk_capture(
    directory_fd: int,
    policy: SeedCapturePolicy,
    *,
    prefix: str,
) -> Iterator[SeedFile]:
    root_stat = os.fstat(directory_fd)
    if not stat.S_ISDIR(root_stat.st_mode):
        raise SeedCaptureError("capture root is not a directory")
    initial_names = _validated_names(directory_fd)
    final_entries: dict[str, tuple[int, int, int, int, int, int, int]] = {}
    for name in initial_names:
        if name in policy.excluded_names:
            continue
        relative_path = f"{prefix}/{name}" if prefix else name
        try:
            _validate_relative_path(relative_path)
        except WorkspaceViolation as error:
            raise SeedCaptureError(str(error)) from error
        try:
            entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as error:
            raise SeedCaptureError(f"capture entry changed during walk: {relative_path}") from error
        if stat.S_ISLNK(entry.st_mode):
            raise SeedCaptureError(f"symlink is not allowed: {relative_path}")
        if stat.S_ISDIR(entry.st_mode):
            child_fd, child_stat = _open_directory_at(directory_fd, name, "capture directory")
            try:
                yield from _walk_capture(child_fd, policy, prefix=relative_path)
                child_final = os.fstat(child_fd)
            finally:
                os.close(child_fd)
            _assert_entry_identity(directory_fd, name, child_stat, "capture directory")
            final_entries[name] = _entry_state(child_final)
            continue
        if not stat.S_ISREG(entry.st_mode):
            raise SeedCaptureError(f"path is not a regular file: {relative_path}")
        yield _capture_regular_file(directory_fd, name, relative_path)
        final_entries[name] = _entry_state(os.stat(name, dir_fd=directory_fd, follow_symlinks=False))
    final_root = os.fstat(directory_fd)
    if (final_root.st_dev, final_root.st_ino) != (root_stat.st_dev, root_stat.st_ino):
        raise SeedCaptureError("capture directory identity changed during walk")
    if _validated_names(directory_fd) != list(initial_names):
        raise SeedCaptureError("capture directory name set changed during walk")
    for name in initial_names:
        if name in policy.excluded_names:
            continue
        try:
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as error:
            raise SeedCaptureError(f"capture entry changed during walk: {name}") from error
        if _entry_state(current) != final_entries[name]:
            raise SeedCaptureError(f"capture entry state changed during walk: {name}")


def _capture_regular_file(parent_fd: int, name: str, relative_path: str) -> SeedFile:
    file_fd, file_stat = _open_file_at(parent_fd, name, relative_path, immutable=False)
    try:
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        while chunk := os.read(file_fd, _COPY_BUFFER_SIZE):
            chunks.append(chunk)
            digest.update(chunk)
        content = b"".join(chunks)
        _assert_open_file_stable(
            file_fd,
            parent_fd,
            name,
            file_stat,
            "captured file",
            immutable=False,
        )
        sha256 = digest.hexdigest()
        if hashlib.sha256(content).hexdigest() != sha256:
            raise SeedCaptureError(f"capture digest mismatch before close: {relative_path}")
        return SeedFile(path=relative_path, sha256=sha256, content=content)
    except WorkspaceViolation as error:
        raise SeedCaptureError(str(error)) from error
    finally:
        os.close(file_fd)


def _validate_snapshot_manifest(manifest: Mapping[str, str]) -> dict[str, str]:
    validated: dict[str, str] = {}
    for path, digest in manifest.items():
        try:
            canonical_path = _validate_relative_canonical_path(path)
        except ValidationError as error:
            raise SnapshotExportError("relative canonical path") from error
        _validate_tree_id(digest, "snapshot file digest")
        if canonical_path in validated:
            raise SnapshotExportError("duplicate snapshot path")
        validated[canonical_path] = digest
    return validated


def _write_snapshot_tree(
    staging_fd: int,
    tree: _OpenedTree,
    manifest: Mapping[str, str],
) -> tuple[ExportedFile, ...]:
    exported: list[ExportedFile] = []
    for path in sorted(manifest):
        expected_digest = manifest[path]
        content = _read_opened_tree_file(tree, path, "snapshot")
        actual_digest = hashlib.sha256(content).hexdigest()
        if actual_digest != expected_digest:
            raise SnapshotExportError(f"snapshot file hash does not match manifest: {path}")
        _write_snapshot_file(staging_fd, path, content)
        exported.append(ExportedFile(path=path, sha256=actual_digest, size_bytes=len(content)))
    os.fsync(staging_fd)
    return tuple(exported)


def _write_snapshot_file(root_fd: int, relative_path: str, content: bytes) -> None:
    components = relative_path.split("/")
    parent_fd = root_fd
    opened_directories: list[int] = []
    try:
        prefix = ""
        for component in components[:-1]:
            prefix = f"{prefix}/{component}" if prefix else component
            try:
                child_fd, _ = _open_directory_at(parent_fd, component, "export path directory")
            except WorkspaceViolation:
                os.mkdir(component, mode=0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
                child_fd, _ = _open_directory_at(parent_fd, component, "export path directory")
            opened_directories.append(child_fd)
            parent_fd = child_fd
        name = components[-1]
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
        try:
            _write_all(descriptor, content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        for directory_fd in reversed(opened_directories):
            os.fsync(directory_fd)
    finally:
        for directory_fd in reversed(opened_directories):
            os.close(directory_fd)


def _entry_state(entry: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        entry.st_dev,
        entry.st_ino,
        entry.st_mode,
        entry.st_nlink,
        entry.st_size,
        entry.st_mtime_ns,
        entry.st_ctime_ns,
    )


def _require_posix_primitives() -> None:
    required = ("O_DIRECTORY", "O_NOFOLLOW", "supports_dir_fd")
    if any(not hasattr(os, name) for name in required) or os.open not in os.supports_dir_fd:
        raise SeedCaptureError("workspace capture requires POSIX dir_fd and O_NOFOLLOW support")


__all__ = [
    "ExportManifest",
    "ExportedFile",
    "SeedCaptureError",
    "SeedCapturePolicy",
    "SnapshotExportError",
    "capture_workspace_seed",
    "materialize_snapshot",
]
