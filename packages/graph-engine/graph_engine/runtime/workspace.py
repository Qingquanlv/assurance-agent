from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterator, cast

from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.errors import GraphEngineError
from graph_engine.identifiers import IdentifierError, validate_qualified_id
from graph_engine.plugin_api import (
    CandidateFile,
    CandidateWriteSet,
    CommitValidator,
    ResourceClaims,
    ValidationContext,
    ValidationResult,
)
from graph_engine.runtime.models import CommitResult, ValidationReceipt

_TREE_ID_LENGTH = 64
_COPY_BUFFER_SIZE = 1024 * 1024
_WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_LAYOUT = ".layout.json"
_HEAD = "HEAD.json"
_HEAD_TRANSACTION = ".HEAD-transaction.json"
NamedValidator = tuple[str, CommitValidator]
Identity = tuple[int, int]
EntryState = tuple[int, int, int, int, int, int, int]


class WorkspaceViolation(GraphEngineError):
    """Raised when workspace state could escape or violate snapshot invariants."""


class HeadPublicationIndeterminate(WorkspaceViolation):
    """Raised when a post-replacement failure prevents proving a durable HEAD outcome."""


class FinalizationRolledBack(WorkspaceViolation):
    """Raised when finalization failed after publication and exact HEAD was restored."""


class _HeadTransactionExists(WorkspaceViolation):
    """Raised when a prior authenticated HEAD transaction requires recovery."""


def _require_posix_primitives() -> None:
    required = ("O_DIRECTORY", "O_NOFOLLOW", "supports_dir_fd")
    if any(not hasattr(os, name) for name in required) or os.open not in os.supports_dir_fd:
        raise WorkspaceViolation("snapshot workspaces require POSIX dir_fd and O_NOFOLLOW support")


def _identity(value: os.stat_result) -> Identity:
    return value.st_dev, value.st_ino


def _entry_state(value: os.stat_result) -> EntryState:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _validate_utf8(value: str, kind: str) -> None:
    try:
        encoded = value.encode("utf-8")
        decoded = encoded.decode("utf-8")
    except UnicodeError as error:
        raise WorkspaceViolation(f"{kind} must round-trip as UTF-8") from error
    if decoded != value or os.fsdecode(os.fsencode(value)) != value:
        raise WorkspaceViolation(f"{kind} must round-trip as UTF-8")


def _validate_relative_path(value: str) -> str:
    if not isinstance(value, str):
        raise WorkspaceViolation("path must be a string")
    _validate_utf8(value, "path")
    windows_path = PureWindowsPath(value)
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or value.startswith("/")
        or path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or any(segment in {"", ".", ".."} for segment in value.split("/"))
        or path.as_posix() != value
    ):
        raise WorkspaceViolation(f"invalid relative path: {value!r}")
    return value


def _validate_attempt_id(value: str) -> str:
    if not isinstance(value, str):
        raise WorkspaceViolation(f"invalid attempt id: {value!r}")
    _validate_utf8(value, "attempt id")
    windows_path = PureWindowsPath(value)
    if (
        value in {"", ".", ".."}
        or "/" in value
        or "\\" in value
        or Path(value).is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
    ):
        raise WorkspaceViolation(f"invalid attempt id: {value!r}")
    return value


def _validate_tree_id(value: object, kind: str = "tree id") -> str:
    if (
        not isinstance(value, str)
        or len(value) != _TREE_ID_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise WorkspaceViolation(f"invalid {kind}: {value!r}")
    return value


def _sha256_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, _COPY_BUFFER_SIZE):
        digest.update(chunk)
    return digest.hexdigest()


def _tree_id(files: Mapping[str, str]) -> str:
    pairs: JSONValue = [[path, files[path]] for path in sorted(files)]
    return canonical_digest(pairs)


def _assert_entry_identity(parent_fd: int, name: str, expected: os.stat_result, kind: str) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise WorkspaceViolation(f"{kind} changed during operation: {name}") from error
    if _identity(current) != _identity(expected) or stat.S_IFMT(current.st_mode) != stat.S_IFMT(
        expected.st_mode
    ):
        raise WorkspaceViolation(f"{kind} identity changed during operation: {name}")


def _open_directory_at(parent_fd: int, name: str, kind: str) -> tuple[int, os.stat_result]:
    try:
        enumerated = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        raise WorkspaceViolation(f"{kind} must be a no-follow directory: {name}") from error
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or _identity(opened) != _identity(enumerated):
        os.close(descriptor)
        raise WorkspaceViolation(f"{kind} identity changed while opening: {name}")
    _assert_entry_identity(parent_fd, name, opened, kind)
    return descriptor, opened


def _open_file_at(
    parent_fd: int, name: str, relative_path: str, *, immutable: bool
) -> tuple[int, os.stat_result]:
    try:
        enumerated = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        descriptor = os.open(name, _FILE_READ_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        raise WorkspaceViolation(f"cannot safely open regular file: {relative_path}") from error
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or _identity(opened) != _identity(enumerated):
        os.close(descriptor)
        if opened.st_nlink != 1:
            raise WorkspaceViolation(f"hard link is not allowed: {relative_path}")
        raise WorkspaceViolation(f"path is not a stable regular file: {relative_path}")
    if immutable and opened.st_mode & _WRITE_BITS:
        os.close(descriptor)
        raise WorkspaceViolation(f"immutable tree file is writable: {relative_path}")
    _assert_entry_identity(parent_fd, name, opened, "file")
    return descriptor, opened


def _assert_open_file_stable(
    descriptor: int,
    parent_fd: int,
    name: str,
    expected: os.stat_result,
    kind: str,
    *,
    immutable: bool,
) -> None:
    current = os.fstat(descriptor)
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or _identity(current) != _identity(expected)
    ):
        raise WorkspaceViolation(f"{kind} identity or link count changed: {name}")
    if immutable and current.st_mode & _WRITE_BITS:
        raise WorkspaceViolation(f"immutable {kind} became writable: {name}")
    _assert_entry_identity(parent_fd, name, current, kind)


def _open_absolute_directory(path: Path) -> int:
    absolute = path.absolute()
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in absolute.parts[1:]:
            child, _ = _open_directory_at(descriptor, component, "directory path component")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _validated_names(directory_fd: int) -> list[str]:
    try:
        names = os.listdir(directory_fd)
    except OSError as error:
        raise WorkspaceViolation("cannot enumerate directory descriptor") from error
    for name in names:
        _validate_utf8(name, "filename")
        if name in {"", ".", ".."} or "/" in name:
            raise WorkspaceViolation(f"invalid filename: {name!r}")
    return sorted(names, key=os.fsencode)


def _entry_exists(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _assert_directory_snapshot(
    directory_fd: int,
    names: Sequence[str],
    entries: Mapping[str, EntryState],
    kind: str,
) -> None:
    if _validated_names(directory_fd) != list(names):
        raise WorkspaceViolation(f"{kind} name set changed during operation")
    for name in names:
        try:
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as error:
            raise WorkspaceViolation(f"{kind} entry changed during operation: {name}") from error
        if _entry_state(current) != entries[name]:
            raise WorkspaceViolation(f"{kind} entry state changed during operation: {name}")


def _scan_directory_fd(directory_fd: int, *, prefix: str = "", immutable: bool = False) -> dict[str, str]:
    root_stat = os.fstat(directory_fd)
    if not stat.S_ISDIR(root_stat.st_mode):
        raise WorkspaceViolation("tree root is not a directory")
    if immutable and root_stat.st_mode & _WRITE_BITS:
        raise WorkspaceViolation("immutable tree directory is writable")
    files: dict[str, str] = {}
    initial_names = _validated_names(directory_fd)
    final_entries: dict[str, EntryState] = {}
    for name in initial_names:
        relative_path = f"{prefix}/{name}" if prefix else name
        _validate_relative_path(relative_path)
        entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISLNK(entry.st_mode):
            raise WorkspaceViolation(f"symlink is not allowed: {relative_path}")
        if stat.S_ISDIR(entry.st_mode):
            child_fd, child_stat = _open_directory_at(directory_fd, name, "tree directory")
            try:
                if immutable and child_stat.st_mode & _WRITE_BITS:
                    raise WorkspaceViolation(f"immutable tree directory is writable: {relative_path}")
                files.update(_scan_directory_fd(child_fd, prefix=relative_path, immutable=immutable))
                child_final = os.fstat(child_fd)
            finally:
                os.close(child_fd)
            _assert_entry_identity(directory_fd, name, child_stat, "tree directory")
            final_entries[name] = _entry_state(child_final)
            continue
        if not stat.S_ISREG(entry.st_mode):
            raise WorkspaceViolation(f"path is not a regular file: {relative_path}")
        file_fd, file_stat = _open_file_at(directory_fd, name, relative_path, immutable=immutable)
        try:
            files[relative_path] = _sha256_fd(file_fd)
            _assert_open_file_stable(
                file_fd,
                directory_fd,
                name,
                file_stat,
                "tree file",
                immutable=immutable,
            )
            file_final = os.fstat(file_fd)
        finally:
            os.close(file_fd)
        final_entries[name] = _entry_state(file_final)
    final_root = os.fstat(directory_fd)
    if _identity(final_root) != _identity(root_stat):
        raise WorkspaceViolation("directory identity changed during scan")
    if immutable and final_root.st_mode & _WRITE_BITS:
        raise WorkspaceViolation("immutable tree directory became writable")
    _assert_directory_snapshot(directory_fd, initial_names, final_entries, "directory")
    return files


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written == 0:
            raise WorkspaceViolation("filesystem write returned zero bytes")
        view = view[written:]


def _copy_tree_fd(
    source_fd: int,
    destination_fd: int,
    *,
    prefix: str = "",
    source_immutable: bool,
    seal_destination: bool,
) -> dict[str, str]:
    manifest: dict[str, str] = {}
    source_names = _validated_names(source_fd)
    final_source_entries: dict[str, EntryState] = {}
    final_destination_entries: dict[str, EntryState] = {}
    for name in source_names:
        relative_path = f"{prefix}/{name}" if prefix else name
        entry = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        if stat.S_ISLNK(entry.st_mode):
            raise WorkspaceViolation(f"symlink is not allowed: {relative_path}")
        if stat.S_ISDIR(entry.st_mode):
            source_child, source_stat = _open_directory_at(source_fd, name, "source directory")
            try:
                if source_immutable and source_stat.st_mode & _WRITE_BITS:
                    raise WorkspaceViolation(f"immutable tree directory is writable: {relative_path}")
                os.mkdir(name, mode=0o700, dir_fd=destination_fd)
                destination_child, destination_stat = _open_directory_at(
                    destination_fd, name, "destination directory"
                )
                try:
                    manifest.update(
                        _copy_tree_fd(
                            source_child,
                            destination_child,
                            prefix=relative_path,
                            source_immutable=source_immutable,
                            seal_destination=seal_destination,
                        )
                    )
                    os.fsync(destination_child)
                    if seal_destination:
                        os.fchmod(destination_child, 0o555)
                        os.fsync(destination_child)
                    final_destination = os.fstat(destination_child)
                    if seal_destination and final_destination.st_mode & _WRITE_BITS:
                        raise WorkspaceViolation(f"sealed destination directory is writable: {relative_path}")
                finally:
                    os.close(destination_child)
                _assert_entry_identity(destination_fd, name, destination_stat, "destination directory")
                final_destination_entries[name] = _entry_state(
                    os.stat(name, dir_fd=destination_fd, follow_symlinks=False)
                )
            finally:
                final_source = os.fstat(source_child)
                os.close(source_child)
            if source_immutable and final_source.st_mode & _WRITE_BITS:
                raise WorkspaceViolation(f"immutable source directory became writable: {relative_path}")
            _assert_entry_identity(source_fd, name, source_stat, "source directory")
            final_source_entries[name] = _entry_state(final_source)
            continue
        if not stat.S_ISREG(entry.st_mode):
            raise WorkspaceViolation(f"path is not a regular file: {relative_path}")
        source_file, source_stat = _open_file_at(source_fd, name, relative_path, immutable=source_immutable)
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            destination_file = os.open(name, flags, 0o600, dir_fd=destination_fd)
            try:
                digest = hashlib.sha256()
                os.lseek(source_file, 0, os.SEEK_SET)
                while chunk := os.read(source_file, _COPY_BUFFER_SIZE):
                    _write_all(destination_file, chunk)
                    digest.update(chunk)
                os.fsync(destination_file)
                if seal_destination:
                    os.fchmod(destination_file, 0o444)
                    os.fsync(destination_file)
                manifest[relative_path] = digest.hexdigest()
                destination_stat = os.fstat(destination_file)
                if not stat.S_ISREG(destination_stat.st_mode) or destination_stat.st_nlink != 1:
                    raise WorkspaceViolation(
                        f"destination file identity or link count changed: {relative_path}"
                    )
                if seal_destination and destination_stat.st_mode & _WRITE_BITS:
                    raise WorkspaceViolation(f"sealed destination file is writable: {relative_path}")
                _assert_entry_identity(destination_fd, name, destination_stat, "destination file")
                final_destination_entries[name] = _entry_state(destination_stat)
            finally:
                os.close(destination_file)
            _assert_open_file_stable(
                source_file,
                source_fd,
                name,
                source_stat,
                "source file",
                immutable=source_immutable,
            )
            final_source_file = os.fstat(source_file)
        finally:
            os.close(source_file)
        final_source_entries[name] = _entry_state(final_source_file)
    os.fsync(destination_fd)
    _assert_directory_snapshot(source_fd, source_names, final_source_entries, "source directory")
    _assert_directory_snapshot(
        destination_fd,
        source_names,
        final_destination_entries,
        "destination directory",
    )
    return manifest


def _mapping_tree(files: Mapping[str, bytes]) -> dict[str, Any]:
    root: dict[str, Any] = {}
    for path in sorted(files):
        _validate_relative_path(path)
        content = files[path]
        if not isinstance(content, bytes):
            raise WorkspaceViolation(f"snapshot content must be bytes: {path}")
        node = root
        segments = path.split("/")
        for segment in segments[:-1]:
            existing = node.setdefault(segment, {})
            if not isinstance(existing, dict):
                raise WorkspaceViolation(f"path collides with directory prefix: {path}")
            node = existing
        leaf = segments[-1]
        if leaf in node:
            raise WorkspaceViolation(f"path collides with directory prefix: {path}")
        node[leaf] = content
    return root


def _write_mapping_fd(directory_fd: int, tree: Mapping[str, Any], prefix: str = "") -> dict[str, str]:
    manifest: dict[str, str] = {}
    for name in sorted(tree, key=os.fsencode):
        relative_path = f"{prefix}/{name}" if prefix else name
        value = tree[name]
        if isinstance(value, dict):
            os.mkdir(name, mode=0o700, dir_fd=directory_fd)
            child_fd, child_stat = _open_directory_at(directory_fd, name, "initial directory")
            try:
                manifest.update(_write_mapping_fd(child_fd, value, relative_path))
                os.fsync(child_fd)
                os.fchmod(child_fd, 0o555)
                os.fsync(child_fd)
            finally:
                os.close(child_fd)
            _assert_entry_identity(directory_fd, name, child_stat, "initial directory")
            continue
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
        try:
            _write_all(descriptor, value)
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o444)
            os.fsync(descriptor)
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
                raise WorkspaceViolation(f"initial file identity or link count changed: {relative_path}")
            _assert_entry_identity(directory_fd, name, file_stat, "initial file")
        finally:
            os.close(descriptor)
        manifest[relative_path] = hashlib.sha256(value).hexdigest()
    os.fsync(directory_fd)
    return manifest


def _remove_entry_at(parent_fd: int, name: str) -> None:
    entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(entry.st_mode) or stat.S_ISLNK(entry.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    child_fd, child_stat = _open_directory_at(parent_fd, name, "removal directory")
    try:
        os.fchmod(child_fd, 0o700)
        for child_name in _validated_names(child_fd):
            _remove_entry_at(child_fd, child_name)
        os.fsync(child_fd)
    finally:
        os.close(child_fd)
    _assert_entry_identity(parent_fd, name, child_stat, "removal directory")
    os.rmdir(name, dir_fd=parent_fd)


def _diff_manifests(baseline: Mapping[str, str], candidate: Mapping[str, str]) -> tuple[CandidateFile, ...]:
    return tuple(
        CandidateFile(path=path, before_sha256=baseline.get(path), after_sha256=candidate.get(path))
        for path in sorted(baseline.keys() | candidate.keys())
        if baseline.get(path) != candidate.get(path)
    )


def _path_is_covered(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in prefixes)


def _validation_receipt(
    validator_id: str,
    validator: CommitValidator,
    candidate: CandidateWriteSet,
    context: ValidationContext,
) -> ValidationReceipt:
    try:
        result = validator.validate(candidate, context)
        if not isinstance(result, ValidationResult):
            return ValidationReceipt(
                validator_id=validator_id,
                accepted=False,
                reason=f"validator returned {type(result).__name__}, expected ValidationResult",
            )
        return ValidationReceipt(validator_id=validator_id, accepted=result.accepted, reason=result.reason)
    except Exception as error:
        detail = str(error)
        suffix = f": {detail}" if detail else ""
        return ValidationReceipt(
            validator_id=validator_id,
            accepted=False,
            reason=f"validator raised {type(error).__name__}{suffix}",
        )


@dataclass(frozen=True, slots=True)
class _OpenedTree:
    descriptor: int
    identity: Identity
    manifest: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class AttemptWorkspace:
    _store: SnapshotStore
    attempt_id: str
    baseline_tree_id: str

    def __post_init__(self) -> None:
        _validate_attempt_id(self.attempt_id)
        _validate_tree_id(self.baseline_tree_id, "baseline tree id")

    @property
    def root(self) -> Path:
        return self._store.root / "attempts" / self.attempt_id

    def seal(self) -> CandidateWriteSet:
        return self._store._seal_attempt(self.attempt_id, self.baseline_tree_id)

    def discard(self) -> None:
        self._store._discard_attempt(self.attempt_id)


class SnapshotStore:
    """Content-addressed snapshots rooted below a trusted parent directory.

    The store root (except one exact attempt directory granted to a task) and its
    parent anchor namespace form the POSIX trust boundary. The host must prevent
    task actors from issuing arbitrary same-UID filesystem operations outside that
    attempt capability. Portable POSIX mode bits alone cannot enforce that boundary,
    so every engine access still authenticates tree bytes and fails closed on
    detected mutation.
    """

    def __init__(self, root: Path, *, _parent_fd: int | None = None) -> None:
        _require_posix_primitives()
        self.root = Path(root).absolute()
        self._parent_fd = _parent_fd
        self._closed = False

    @classmethod
    def at(cls, parent_fd: int, name: str, *, display_root: Path) -> SnapshotStore:
        if not name or "/" in name or name in {".", ".."}:
            raise ValueError("snapshot store name must be one path component")
        root = Path(display_root)
        if root.name != name:
            raise ValueError("display root must end with the snapshot store name")
        descriptor = os.dup(parent_fd)
        try:
            return cls(root, _parent_fd=descriptor)
        except BaseException:
            os.close(descriptor)
            raise

    def close(self) -> None:
        if self._closed:
            return
        descriptor = self._parent_fd
        self._closed = True
        self._parent_fd = None
        if descriptor is not None:
            os.close(descriptor)

    def __enter__(self) -> SnapshotStore:
        if self._closed:
            raise WorkspaceViolation("snapshot store is closed")
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass

    @property
    def _lock_anchor_name(self) -> str:
        return f".{self.root.name}.snapshot-lock"

    @classmethod
    def create(cls, root: Path, initial_files: Mapping[str, bytes]) -> SnapshotStore:
        store = cls(root)
        parent_fd = _open_absolute_directory(store.root.parent)
        return store._create_at(parent_fd, initial_files)

    @classmethod
    def create_at(
        cls,
        parent_fd: int,
        name: str,
        initial_files: Mapping[str, bytes],
        *,
        display_root: Path,
    ) -> SnapshotStore:
        store = cls.at(parent_fd, name, display_root=display_root)
        try:
            return store._create_at(os.dup(parent_fd), initial_files)
        except BaseException:
            try:
                store.close()
            except BaseException:
                pass
            raise

    def _create_at(self, parent_fd: int, initial_files: Mapping[str, bytes]) -> SnapshotStore:
        store = self
        staging_name: str | None = None
        staging_fd: int | None = None
        installed = False
        try:
            staging_name = f".{store.root.name}.snapshot-init-{uuid.uuid4().hex}"
            if _entry_exists(parent_fd, store.root.name):
                raise WorkspaceViolation(f"snapshot store already exists: {store.root}")
            lock_fd, lock_stat = store._open_or_create_lock_anchor(parent_fd)
            os.close(lock_fd)
            try:
                os.mkdir(staging_name, mode=0o700, dir_fd=parent_fd)
            except FileExistsError as error:
                raise WorkspaceViolation("snapshot initialization staging collision") from error
            os.fsync(parent_fd)
            staging_fd, _ = _open_directory_at(parent_fd, staging_name, "snapshot initialization staging")
            os.mkdir("trees", mode=0o700, dir_fd=staging_fd)
            os.mkdir("attempts", mode=0o700, dir_fd=staging_fd)
            store._write_layout(staging_fd, lock_stat)
            os.fsync(staging_fd)
            tree = _mapping_tree(initial_files)
            trees_fd, _ = _open_directory_at(staging_fd, "trees", "trees directory")
            try:
                tree_staging_name, tree_staging_fd = store._create_staging(trees_fd)
                try:
                    manifest = _write_mapping_fd(tree_staging_fd, tree)
                    tree_id, opened_tree = store._finish_tree(
                        trees_fd, tree_staging_name, tree_staging_fd, manifest
                    )
                    try:
                        store._publish_head(
                            staging_fd,
                            trees_fd,
                            tree_id,
                            opened_tree,
                            None,
                        )
                    finally:
                        os.close(opened_tree.descriptor)
                finally:
                    os.close(tree_staging_fd)
            finally:
                os.close(trees_fd)
            os.fsync(staging_fd)
            os.rename(
                staging_name,
                store.root.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            installed = True
            os.fsync(parent_fd)
            return store
        except Exception:
            remove_staging = staging_name is not None and not installed
            if remove_staging:
                assert staging_name is not None
                try:
                    remove_staging = _entry_exists(parent_fd, staging_name)
                except BaseException:
                    # The exact private name is ours and installation did not happen.
                    # If existence cannot be determined, still attempt the safe rollback.
                    remove_staging = True
            if remove_staging:
                assert staging_name is not None
                if staging_fd is not None:
                    try:
                        os.close(staging_fd)
                    except BaseException:
                        pass
                    else:
                        staging_fd = None
                try:
                    _remove_entry_at(parent_fd, staging_name)
                except BaseException:
                    pass
                try:
                    os.fsync(parent_fd)
                except BaseException:
                    pass
            raise
        finally:
            active_exception = sys.exception()
            cleanup_error: BaseException | None = None
            if staging_fd is not None:
                try:
                    os.close(staging_fd)
                except BaseException as error:
                    cleanup_error = error
            try:
                os.close(parent_fd)
            except BaseException as error:
                if cleanup_error is None:
                    cleanup_error = error
            if active_exception is None and cleanup_error is not None:
                raise cleanup_error

    def _open_or_create_lock_anchor(self, parent_fd: int) -> tuple[int, os.stat_result]:
        name = self._lock_anchor_name
        try:
            descriptor = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            try:
                payload: dict[str, JSONValue] = {
                    "version": 1,
                    "store_name": self.root.name,
                    "nonce": uuid.uuid4().hex,
                }
                document: dict[str, JSONValue] = {
                    **payload,
                    "digest": canonical_digest(payload),
                }
                _write_all(descriptor, canonical_json_bytes(document))
                os.fsync(descriptor)
                os.fchmod(descriptor, 0o400)
                os.fsync(descriptor)
                anchor_stat = os.fstat(descriptor)
            except BaseException:
                os.close(descriptor)
                os.unlink(name, dir_fd=parent_fd)
                raise
            os.fsync(parent_fd)
        except FileExistsError:
            descriptor, anchor_stat = _open_file_at(
                parent_fd,
                name,
                name,
                immutable=True,
            )
        try:
            self._validate_lock_anchor(descriptor, parent_fd, anchor_stat)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor, anchor_stat

    def _validate_lock_anchor(
        self,
        descriptor: int,
        parent_fd: int,
        anchor_stat: os.stat_result,
    ) -> None:
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            content = os.read(descriptor, 4096)
            if os.read(descriptor, 1):
                raise WorkspaceViolation("commit lock anchor is too large")
            document = json.loads(content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise WorkspaceViolation("commit lock anchor is invalid") from error
        _assert_open_file_stable(
            descriptor,
            parent_fd,
            self._lock_anchor_name,
            anchor_stat,
            "commit lock anchor",
            immutable=True,
        )
        if not isinstance(document, dict) or set(document) != {
            "version",
            "store_name",
            "nonce",
            "digest",
        }:
            raise WorkspaceViolation("commit lock anchor document is invalid")
        payload: dict[str, JSONValue] = {
            "version": document["version"],
            "store_name": document["store_name"],
            "nonce": document["nonce"],
        }
        if (
            document["version"] != 1
            or document["store_name"] != self.root.name
            or not isinstance(document["nonce"], str)
            or document["digest"] != canonical_digest(payload)
        ):
            raise WorkspaceViolation("commit lock anchor authentication failed")

    def _write_layout(self, root_fd: int, lock_stat: os.stat_result) -> None:
        payload: dict[str, JSONValue] = {
            "version": 1,
            "lock_dev": lock_stat.st_dev,
            "lock_ino": lock_stat.st_ino,
        }
        document: dict[str, JSONValue] = {**payload, "digest": canonical_digest(payload)}
        descriptor = os.open(
            _LAYOUT,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_fd,
        )
        try:
            _write_all(descriptor, canonical_json_bytes(document))
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o400)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @contextmanager
    def _opened_layout(self, *, lock: bool = False) -> Iterator[tuple[int, int, int, int | None]]:
        if self._closed:
            raise WorkspaceViolation("snapshot store is closed")
        parent_fd = (
            os.dup(self._parent_fd)
            if self._parent_fd is not None
            else _open_absolute_directory(self.root.parent)
        )
        root_fd = trees_fd = attempts_fd = anchor_fd = -1
        try:
            anchor_fd, anchor_stat = _open_file_at(
                parent_fd,
                self._lock_anchor_name,
                self._lock_anchor_name,
                immutable=True,
            )
            self._validate_lock_anchor(anchor_fd, parent_fd, anchor_stat)
            if lock:
                _lock_exclusive(anchor_fd)
                _assert_open_file_stable(
                    anchor_fd,
                    parent_fd,
                    self._lock_anchor_name,
                    anchor_stat,
                    "commit lock anchor",
                    immutable=True,
                )
            root_fd, _ = _open_directory_at(parent_fd, self.root.name, "snapshot store root")
            trees_fd, _ = _open_directory_at(root_fd, "trees", "trees directory")
            attempts_fd, _ = _open_directory_at(root_fd, "attempts", "attempts directory")
            layout_fd, layout_stat = _open_file_at(root_fd, _LAYOUT, _LAYOUT, immutable=True)
            try:
                layout = json.loads(os.read(layout_fd, 4096).decode("utf-8"))
                _assert_open_file_stable(
                    layout_fd,
                    root_fd,
                    _LAYOUT,
                    layout_stat,
                    "layout file",
                    immutable=True,
                )
            finally:
                os.close(layout_fd)
            if not isinstance(layout, dict) or set(layout) != {
                "version",
                "lock_dev",
                "lock_ino",
                "digest",
            }:
                raise WorkspaceViolation("layout document is invalid")
            payload: dict[str, JSONValue] = {
                "version": layout["version"],
                "lock_dev": layout["lock_dev"],
                "lock_ino": layout["lock_ino"],
            }
            if layout["digest"] != canonical_digest(payload):
                raise WorkspaceViolation("layout digest mismatch")
            expected = layout["lock_dev"], layout["lock_ino"]
            if _identity(anchor_stat) != expected:
                raise WorkspaceViolation("commit lock inode does not match protected anchor")
            yield root_fd, trees_fd, attempts_fd, anchor_fd if lock else None
        finally:
            for descriptor in (attempts_fd, trees_fd, root_fd, anchor_fd, parent_fd):
                if descriptor >= 0:
                    os.close(descriptor)

    def _create_staging(self, trees_fd: int) -> tuple[str, int]:
        name = f".tmp-{uuid.uuid4().hex}"
        os.mkdir(name, mode=0o700, dir_fd=trees_fd)
        descriptor, _ = _open_directory_at(trees_fd, name, "tree staging directory")
        return name, descriptor

    def _open_tree(self, trees_fd: int, tree_id: str) -> _OpenedTree:
        validated = _validate_tree_id(tree_id)
        descriptor, tree_stat = _open_directory_at(trees_fd, validated, "snapshot tree")
        try:
            manifest = _scan_directory_fd(descriptor, immutable=True)
            actual = _tree_id(manifest)
            if actual != validated:
                raise WorkspaceViolation(f"snapshot tree id mismatch: expected {validated}, found {actual}")
            return _OpenedTree(descriptor, _identity(tree_stat), manifest)
        except BaseException:
            os.close(descriptor)
            raise

    def _finish_tree(
        self,
        trees_fd: int,
        staging_name: str,
        staging_fd: int,
        manifest: Mapping[str, str],
    ) -> tuple[str, _OpenedTree]:
        tree_id = _tree_id(manifest)
        if _entry_exists(trees_fd, tree_id):
            existing = self._open_tree(trees_fd, tree_id)
            _remove_entry_at(trees_fd, staging_name)
            os.fsync(trees_fd)
            return tree_id, existing
        os.fsync(staging_fd)
        os.rename(staging_name, tree_id, src_dir_fd=trees_fd, dst_dir_fd=trees_fd)
        os.fchmod(staging_fd, 0o555)
        os.fsync(staging_fd)
        os.fsync(trees_fd)
        opened = self._open_tree(trees_fd, tree_id)
        if dict(opened.manifest) != dict(manifest):
            os.close(opened.descriptor)
            raise WorkspaceViolation("installed tree differs from staged manifest")
        return tree_id, opened

    def _read_head_document(self, root_fd: int) -> dict[str, Any]:
        descriptor, head_stat = _open_file_at(root_fd, _HEAD, _HEAD, immutable=False)
        try:
            document = json.loads(os.read(descriptor, 4096).decode("utf-8"))
            _assert_open_file_stable(
                descriptor,
                root_fd,
                _HEAD,
                head_stat,
                "HEAD file",
                immutable=False,
            )
        except (UnicodeError, json.JSONDecodeError) as error:
            raise WorkspaceViolation("HEAD is unreadable or invalid") from error
        finally:
            os.close(descriptor)
        if not isinstance(document, dict) or set(document) != {
            "tree_id",
            "tree_dev",
            "tree_ino",
            "digest",
        }:
            raise WorkspaceViolation("HEAD has an invalid document shape")
        payload: dict[str, JSONValue] = {
            "tree_id": document["tree_id"],
            "tree_dev": document["tree_dev"],
            "tree_ino": document["tree_ino"],
        }
        if document["digest"] != canonical_digest(payload):
            raise WorkspaceViolation("HEAD digest mismatch")
        _validate_tree_id(document["tree_id"], "HEAD tree id")
        return document

    def _head_tree(self, root_fd: int, trees_fd: int) -> tuple[dict[str, Any], _OpenedTree]:
        document = self._read_head_document(root_fd)
        tree = self._open_tree(trees_fd, document["tree_id"])
        if tree.identity != (document["tree_dev"], document["tree_ino"]):
            os.close(tree.descriptor)
            raise WorkspaceViolation("HEAD tree identity mismatch")
        return document, tree

    def _head_bytes(self, tree_id: str, identity: Identity) -> bytes:
        payload: dict[str, JSONValue] = {
            "tree_id": tree_id,
            "tree_dev": identity[0],
            "tree_ino": identity[1],
        }
        document: dict[str, JSONValue] = {**payload, "digest": canonical_digest(payload)}
        return canonical_json_bytes(document)

    def _write_temp(self, root_fd: int, name: str, content: bytes) -> None:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_fd,
        )
        try:
            _write_all(descriptor, content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _publish_head(
        self,
        root_fd: int,
        trees_fd: int,
        tree_id: str,
        tree: _OpenedTree,
        previous: bytes | None,
    ) -> None:
        temporary = f".HEAD-{uuid.uuid4().hex}.tmp"
        self._write_temp(root_fd, temporary, self._head_bytes(tree_id, tree.identity))
        try:
            current_manifest = _scan_directory_fd(tree.descriptor, immutable=True)
            _assert_entry_identity(
                trees_fd,
                tree_id,
                os.fstat(tree.descriptor),
                "candidate tree",
            )
            if _tree_id(current_manifest) != tree_id:
                raise WorkspaceViolation("candidate tree changed before HEAD publication")
            try:
                os.replace(temporary, _HEAD, src_dir_fd=root_fd, dst_dir_fd=root_fd)
                os.fsync(root_fd)
                current_manifest = _scan_directory_fd(tree.descriptor, immutable=True)
                _assert_entry_identity(
                    trees_fd,
                    tree_id,
                    os.fstat(tree.descriptor),
                    "candidate tree",
                )
                if _tree_id(current_manifest) != tree_id:
                    raise WorkspaceViolation("candidate tree changed during HEAD publication")
            except BaseException:
                try:
                    still_previous = self._head_matches(root_fd, previous)
                except BaseException:
                    still_previous = False
                if still_previous:
                    raise
                try:
                    self._restore_previous_head(root_fd, previous)
                except BaseException as rollback_error:
                    raise HeadPublicationIndeterminate(
                        "HEAD publication outcome is indeterminate after rollback failure"
                    ) from rollback_error
                raise
        finally:
            try:
                os.unlink(temporary, dir_fd=root_fd)
            except OSError:
                pass

    def _head_matches(self, root_fd: int, expected: bytes | None) -> bool:
        if expected is None:
            return not _entry_exists(root_fd, _HEAD)
        try:
            descriptor, head_stat = _open_file_at(root_fd, _HEAD, _HEAD, immutable=False)
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                chunks: list[bytes] = []
                while chunk := os.read(descriptor, 4096):
                    chunks.append(chunk)
                _assert_open_file_stable(
                    descriptor,
                    root_fd,
                    _HEAD,
                    head_stat,
                    "HEAD file",
                    immutable=False,
                )
            finally:
                os.close(descriptor)
        except (OSError, WorkspaceViolation):
            return False
        return b"".join(chunks) == expected

    def _restore_previous_head(self, root_fd: int, previous: bytes | None) -> None:
        if previous is None:
            try:
                os.unlink(_HEAD, dir_fd=root_fd)
            except FileNotFoundError:
                pass
            os.fsync(root_fd)
            return
        rollback = f".HEAD-rollback-{uuid.uuid4().hex}.tmp"
        try:
            self._write_temp(root_fd, rollback, previous)
            os.replace(rollback, _HEAD, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            os.fsync(root_fd)
        finally:
            try:
                os.unlink(rollback, dir_fd=root_fd)
            except FileNotFoundError:
                pass

    def _begin_head_transaction(
        self,
        root_fd: int,
        previous: bytes,
        candidate_tree_id: str,
    ) -> None:
        if _entry_exists(root_fd, _HEAD_TRANSACTION):
            raise _HeadTransactionExists("unfinished HEAD transaction requires recovery")
        previous_head = json.loads(previous)
        payload: dict[str, JSONValue] = {
            "version": 1,
            "previous_head": previous_head,
            "candidate_tree_id": candidate_tree_id,
        }
        document: dict[str, JSONValue] = {**payload, "digest": canonical_digest(payload)}
        temporary = f".{_HEAD_TRANSACTION}-{uuid.uuid4().hex}.tmp"
        self._write_temp(root_fd, temporary, canonical_json_bytes(document))
        try:
            os.replace(
                temporary,
                _HEAD_TRANSACTION,
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
            )
            os.fsync(root_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=root_fd)
            except FileNotFoundError:
                pass

    def _clear_head_transaction(self, root_fd: int) -> None:
        try:
            os.unlink(_HEAD_TRANSACTION, dir_fd=root_fd)
        except FileNotFoundError:
            pass
        os.fsync(root_fd)

    def _finish_published_head_transaction(
        self,
        root_fd: int,
        *,
        previous: bytes,
        candidate_tree_id: str,
        published: bytes,
    ) -> None:
        try:
            self._clear_head_transaction(root_fd)
            return
        except BaseException as clear_error:
            if not self._head_matches(root_fd, published):
                raise HeadPublicationIndeterminate(
                    "authoritative success was published but candidate HEAD no longer matches; "
                    "journal clear cannot be reconciled"
                ) from clear_error
        try:
            self._clear_head_transaction(root_fd)
        except BaseException as retry_error:
            self._retain_head_transaction(root_fd, previous, candidate_tree_id)
            raise HeadPublicationIndeterminate(
                "authoritative success and candidate HEAD agree but journal clear is indeterminate"
            ) from retry_error

    def _retain_head_transaction(
        self,
        root_fd: int,
        previous: bytes,
        candidate_tree_id: str,
    ) -> None:
        if _entry_exists(root_fd, _HEAD_TRANSACTION):
            return
        try:
            self._begin_head_transaction(root_fd, previous, candidate_tree_id)
        except BaseException:
            # The caller raises an explicit indeterminate-publication error either way.
            # A best-effort recreation keeps the common post-unlink fsync cut recoverable.
            pass

    def recover_head_transaction(self, authoritative_tree_id: str | None) -> None:
        """Resolve an interrupted HEAD/ledger publication from authoritative ledger state."""
        with self._opened_layout(lock=True) as (root_fd, trees_fd, _attempts_fd, _lock_fd):
            if not _entry_exists(root_fd, _HEAD_TRANSACTION):
                return
            descriptor, transaction_stat = _open_file_at(
                root_fd,
                _HEAD_TRANSACTION,
                _HEAD_TRANSACTION,
                immutable=False,
            )
            try:
                raw = os.read(descriptor, 16384)
                if os.read(descriptor, 1):
                    raise WorkspaceViolation("HEAD transaction is too large")
                transaction = json.loads(raw.decode("utf-8"))
                _assert_open_file_stable(
                    descriptor,
                    root_fd,
                    _HEAD_TRANSACTION,
                    transaction_stat,
                    "HEAD transaction",
                    immutable=False,
                )
            except (UnicodeError, json.JSONDecodeError) as error:
                raise WorkspaceViolation("HEAD transaction is invalid") from error
            finally:
                os.close(descriptor)
            if not isinstance(transaction, dict) or set(transaction) != {
                "version",
                "previous_head",
                "candidate_tree_id",
                "digest",
            }:
                raise WorkspaceViolation("HEAD transaction document is invalid")
            payload: dict[str, JSONValue] = {
                "version": transaction["version"],
                "previous_head": transaction["previous_head"],
                "candidate_tree_id": transaction["candidate_tree_id"],
            }
            if transaction["version"] != 1 or transaction["digest"] != canonical_digest(payload):
                raise WorkspaceViolation("HEAD transaction authentication failed")
            if not isinstance(transaction["previous_head"], dict):
                raise WorkspaceViolation("HEAD transaction previous head is invalid")
            previous = cast(dict[str, Any], transaction["previous_head"])
            previous_tree_id = previous.get("tree_id")
            candidate_tree_id = _validate_tree_id(
                transaction["candidate_tree_id"],
                "transaction candidate tree id",
            )
            if not isinstance(previous_tree_id, str):
                raise WorkspaceViolation("HEAD transaction previous tree is invalid")
            previous_bytes = canonical_json_bytes(cast(JSONValue, previous))
            if authoritative_tree_id == candidate_tree_id:
                current, tree = self._head_tree(root_fd, trees_fd)
                os.close(tree.descriptor)
                if current["tree_id"] != candidate_tree_id:
                    raise WorkspaceViolation("authoritative candidate HEAD is not installed")
            else:
                if authoritative_tree_id is not None and authoritative_tree_id != previous_tree_id:
                    raise WorkspaceViolation("HEAD transaction disagrees with authoritative ledger")
                self._restore_previous_head(root_fd, previous_bytes)
                if not self._head_matches(root_fd, previous_bytes):
                    raise WorkspaceViolation("HEAD transaction rollback was not durable")
            self._clear_head_transaction(root_fd)

    def head_tree_id(self) -> str:
        with self._opened_layout() as (root_fd, trees_fd, _attempts_fd, _lock_fd):
            document, tree = self._head_tree(root_fd, trees_fd)
            os.close(tree.descriptor)
            return document["tree_id"]

    def read_head(self, relative_path: str) -> bytes:
        path = _validate_relative_path(relative_path)
        with self._opened_layout() as (root_fd, trees_fd, _attempts_fd, _lock_fd):
            _document, tree = self._head_tree(root_fd, trees_fd)
            try:
                return _read_opened_tree_file(tree, path, "HEAD")
            finally:
                os.close(tree.descriptor)

    def _candidate_contents(self, candidate: CandidateWriteSet) -> dict[str, bytes | None]:
        """Authenticate a sealed candidate and materialize only its declared changes."""
        with self._opened_layout(lock=True) as (_root_fd, trees_fd, _attempts_fd, _lock_fd):
            baseline = self._open_tree(trees_fd, candidate.baseline_tree_id)
            candidate_tree = self._open_tree(trees_fd, candidate.candidate_tree_id)
            try:
                actual_diff = _diff_manifests(baseline.manifest, candidate_tree.manifest)
                if actual_diff != candidate.files:
                    raise WorkspaceViolation("candidate diff does not match baseline and candidate trees")
                return {
                    item.path: (
                        None
                        if item.after_sha256 is None
                        else _read_opened_tree_file(candidate_tree, item.path, "candidate")
                    )
                    for item in candidate.files
                }
            finally:
                os.close(candidate_tree.descriptor)
                os.close(baseline.descriptor)

    def rebase_candidate(self, candidate: CandidateWriteSet, attempt_id: str) -> CandidateWriteSet:
        """Reapply an authenticated candidate diff to current HEAD and seal the result.

        The rebase is conservative: every changed path must still have the exact
        baseline hash observed by the handler. This prevents a stale candidate from
        overwriting a concurrent change even if a caller selected an unsafe wave.
        """
        contents = self._candidate_contents(candidate)
        attempt = self.create_attempt(attempt_id)
        try:
            for item in sorted(candidate.files, key=lambda value: (-value.path.count("/"), value.path)):
                if item.before_sha256 is None:
                    continue
                target = attempt.root / item.path
                if target.is_file():
                    target.unlink()
                elif target.exists():
                    raise WorkspaceViolation(f"candidate changed path is not a regular file: {item.path}")
                _remove_empty_parents(target.parent, attempt.root)

            for item in candidate.files:
                content = contents[item.path]
                if content is None:
                    continue
                target = attempt.root / item.path
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_dir():
                    try:
                        target.rmdir()
                    except OSError as error:
                        raise WorkspaceViolation(
                            f"candidate file path collides with a non-empty directory: {item.path}"
                        ) from error
                target.write_bytes(content)

            rebased = attempt.seal()
            rebased_by_path = {item.path: item for item in rebased.files}
            if set(rebased_by_path) != {item.path for item in candidate.files}:
                raise WorkspaceViolation("rebased candidate changed an unexpected path")
            for item in candidate.files:
                rebased_file = rebased_by_path[item.path]
                if (
                    rebased_file.before_sha256 != item.before_sha256
                    or rebased_file.after_sha256 != item.after_sha256
                ):
                    raise WorkspaceViolation(f"candidate path changed since handler baseline: {item.path}")
            return rebased
        finally:
            attempt.discard()

    def create_attempts(self, attempt_ids: Sequence[str]) -> tuple[AttemptWorkspace, ...]:
        validated_ids = tuple(_validate_attempt_id(attempt_id) for attempt_id in attempt_ids)
        if not validated_ids:
            raise ValueError("attempt_ids must not be empty")
        if len(set(validated_ids)) != len(validated_ids):
            raise WorkspaceViolation("attempt batch contains duplicate ids")
        with self._opened_layout(lock=True) as (root_fd, trees_fd, attempts_fd, _lock_fd):
            document, baseline = self._head_tree(root_fd, trees_fd)
            created: list[str] = []
            try:
                for validated in validated_ids:
                    try:
                        os.mkdir(validated, mode=0o700, dir_fd=attempts_fd)
                    except FileExistsError as error:
                        raise WorkspaceViolation(f"attempt already exists: {validated}") from error
                    created.append(validated)
                    destination_fd, _ = _open_directory_at(attempts_fd, validated, "attempt directory")
                    try:
                        copied = _copy_tree_fd(
                            baseline.descriptor,
                            destination_fd,
                            source_immutable=True,
                            seal_destination=False,
                        )
                        if copied != baseline.manifest:
                            raise WorkspaceViolation("attempt copy does not match its baseline")
                    finally:
                        os.close(destination_fd)
                    _attempt_batch_boundary(len(created), document["tree_id"])
                return tuple(
                    AttemptWorkspace(self, validated, document["tree_id"]) for validated in validated_ids
                )
            except BaseException:
                for validated in reversed(created):
                    _remove_entry_at(attempts_fd, validated)
                os.fsync(attempts_fd)
                raise
            finally:
                os.close(baseline.descriptor)

    def create_attempt(self, attempt_id: str) -> AttemptWorkspace:
        return self.create_attempts((attempt_id,))[0]

    def reset_attempt(self, attempt_id: str) -> AttemptWorkspace:
        """Recreate one deterministic attempt from authoritative HEAD after process loss."""
        validated = _validate_attempt_id(attempt_id)
        self._discard_attempt(validated)
        return self.create_attempt(validated)

    def _seal_attempt(self, attempt_id: str, baseline_tree_id: str) -> CandidateWriteSet:
        with self._opened_layout(lock=True) as (_root_fd, trees_fd, attempts_fd, _lock_fd):
            source_fd, _ = _open_directory_at(attempts_fd, attempt_id, "attempt directory")
            baseline = self._open_tree(trees_fd, baseline_tree_id)
            staging_name, staging_fd = self._create_staging(trees_fd)
            try:
                manifest = _copy_tree_fd(
                    source_fd,
                    staging_fd,
                    source_immutable=False,
                    seal_destination=True,
                )
                tree_id, candidate = self._finish_tree(trees_fd, staging_name, staging_fd, manifest)
                os.close(candidate.descriptor)
                return CandidateWriteSet(
                    baseline_tree_id=baseline_tree_id,
                    candidate_tree_id=tree_id,
                    files=_diff_manifests(baseline.manifest, manifest),
                )
            finally:
                if _entry_exists(trees_fd, staging_name):
                    _remove_entry_at(trees_fd, staging_name)
                    os.fsync(trees_fd)
                os.close(staging_fd)
                os.close(source_fd)
                os.close(baseline.descriptor)

    def _discard_attempt(self, attempt_id: str) -> None:
        with self._opened_layout(lock=True) as (_root_fd, _trees_fd, attempts_fd, _lock_fd):
            try:
                _remove_entry_at(attempts_fd, attempt_id)
                os.fsync(attempts_fd)
            except FileNotFoundError:
                return

    def _validate_validators(
        self,
        validators: Sequence[NamedValidator],
        context: ValidationContext | None,
        claims: ResourceClaims,
    ) -> ValidationContext | None:
        if not validators:
            return context
        if context is None:
            raise WorkspaceViolation("validation context is required when validators are selected")
        if context.resources != claims:
            raise WorkspaceViolation("validation context resources do not match commit claims")
        seen: set[str] = set()
        for validator_id, _validator in validators:
            try:
                validate_qualified_id(validator_id)
            except IdentifierError as error:
                raise WorkspaceViolation(f"invalid commit validator id: {validator_id!r}") from error
            if validator_id in seen:
                raise WorkspaceViolation(f"duplicate selected commit validator: {validator_id}")
            seen.add(validator_id)
        return context

    def commit_candidate(
        self,
        candidate: CandidateWriteSet,
        claims: ResourceClaims,
        validators: Sequence[NamedValidator] = (),
        context: ValidationContext | None = None,
    ) -> CommitResult:
        return self.finalize_candidate(
            candidate,
            claims,
            validators,
            context,
            authorize_publish=lambda: None,
            publish_success=lambda _previous_tree_id, _tree_id: None,
        )

    def finalize_candidate(
        self,
        candidate: CandidateWriteSet,
        claims: ResourceClaims,
        validators: Sequence[NamedValidator] = (),
        context: ValidationContext | None = None,
        *,
        authorize_publish: Callable[[], None],
        publish_success: Callable[[str, str], None],
    ) -> CommitResult:
        """Validate, publish, and record success under one stable commit lock.

        If success publication fails after physical HEAD publication, rollback is
        conditional on HEAD still naming this exact candidate. A newer HEAD is never
        overwritten and is reported as indeterminate for external reconciliation.
        """
        selected = tuple(validators)
        validator_context = self._validate_validators(selected, context, claims)
        with self._opened_layout(lock=True) as (root_fd, trees_fd, _attempts_fd, _lock_fd):
            head_document, baseline = self._head_tree(root_fd, trees_fd)
            candidate_tree: _OpenedTree | None = None
            try:
                current_tree_id = head_document["tree_id"]
                if candidate.baseline_tree_id != current_tree_id:
                    raise WorkspaceViolation("candidate baseline does not match current HEAD")
                for changed_file in candidate.files:
                    path = _validate_relative_path(changed_file.path)
                    if not _path_is_covered(path, claims.writes):
                        raise WorkspaceViolation(f"changed path is not covered by a write claim: {path}")
                candidate_tree = self._open_tree(trees_fd, candidate.candidate_tree_id)
                actual_diff = _diff_manifests(baseline.manifest, candidate_tree.manifest)
                if actual_diff != candidate.files:
                    raise WorkspaceViolation("candidate diff does not match baseline and candidate trees")
                receipts = tuple(
                    _validation_receipt(validator_id, validator, candidate, validator_context)
                    for validator_id, validator in selected
                    if validator_context is not None
                )
                if any(not receipt.accepted for receipt in receipts):
                    return CommitResult(committed=False, receipts=receipts)
                authorize_publish()
                previous = canonical_json_bytes(head_document)
                try:
                    self._begin_head_transaction(
                        root_fd,
                        previous,
                        candidate.candidate_tree_id,
                    )
                except _HeadTransactionExists:
                    raise
                except BaseException:
                    try:
                        self._clear_head_transaction(root_fd)
                    except BaseException as cleanup_error:
                        raise HeadPublicationIndeterminate(
                            "HEAD transaction preparation outcome is indeterminate"
                        ) from cleanup_error
                    raise
                try:
                    self._publish_head(
                        root_fd,
                        trees_fd,
                        candidate.candidate_tree_id,
                        candidate_tree,
                        previous,
                    )
                except BaseException:
                    if self._head_matches(root_fd, previous):
                        self._clear_head_transaction(root_fd)
                    raise
                published = self._head_bytes(candidate.candidate_tree_id, candidate_tree.identity)
                try:
                    _finalization_boundary("candidate_published")
                    publish_success(current_tree_id, candidate.candidate_tree_id)
                except HeadPublicationIndeterminate:
                    raise
                except BaseException as error:
                    if not self._head_matches(root_fd, published):
                        raise HeadPublicationIndeterminate(
                            "candidate success failed after HEAD advanced elsewhere; "
                            "conditional rollback refused"
                        ) from error
                    try:
                        self._restore_previous_head(root_fd, previous)
                        if not self._head_matches(root_fd, previous):
                            raise WorkspaceViolation("conditional HEAD rollback was not durable")
                        self._clear_head_transaction(root_fd)
                    except BaseException as rollback_error:
                        raise HeadPublicationIndeterminate(
                            "candidate success failed and conditional HEAD rollback is indeterminate"
                        ) from rollback_error
                    raise FinalizationRolledBack(
                        "candidate success failed; exact candidate HEAD was rolled back"
                    ) from error
                self._finish_published_head_transaction(
                    root_fd,
                    previous=previous,
                    candidate_tree_id=candidate.candidate_tree_id,
                    published=published,
                )
                return CommitResult(committed=True, receipts=receipts)
            finally:
                os.close(baseline.descriptor)
                if candidate_tree is not None:
                    os.close(candidate_tree.descriptor)


def commit_candidate(
    store: SnapshotStore,
    candidate: CandidateWriteSet,
    claims: ResourceClaims,
    validators: Sequence[NamedValidator] = (),
    context: ValidationContext | None = None,
) -> CommitResult:
    return store.commit_candidate(candidate, claims, validators, context)


def _read_opened_tree_file(tree: _OpenedTree, relative_path: str, kind: str) -> bytes:
    path = _validate_relative_path(relative_path)
    expected = tree.manifest.get(path)
    if expected is None:
        raise WorkspaceViolation(f"path is not authenticated by {kind} manifest: {path}")
    descriptor = os.dup(tree.descriptor)
    try:
        segments = path.split("/")
        for segment in segments[:-1]:
            child, _ = _open_directory_at(descriptor, segment, f"{kind} path directory")
            os.close(descriptor)
            descriptor = child
        file_fd, file_stat = _open_file_at(descriptor, segments[-1], path, immutable=True)
        try:
            digest = hashlib.sha256()
            chunks: list[bytes] = []
            while chunk := os.read(file_fd, _COPY_BUFFER_SIZE):
                chunks.append(chunk)
                digest.update(chunk)
            _assert_open_file_stable(
                file_fd,
                descriptor,
                segments[-1],
                file_stat,
                f"{kind} file",
                immutable=True,
            )
            if digest.hexdigest() != expected:
                raise WorkspaceViolation(f"{kind} file hash does not match manifest: {path}")
            return b"".join(chunks)
        finally:
            os.close(file_fd)
    finally:
        os.close(descriptor)


def _remove_empty_parents(path: Path, stop: Path) -> None:
    while path != stop:
        try:
            path.rmdir()
        except OSError:
            return
        path = path.parent


def _lock_exclusive(descriptor: int) -> None:
    try:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except ImportError as error:
        raise WorkspaceViolation("snapshot commits require POSIX advisory file locking") from error


def _finalization_boundary(name: str) -> None:
    del name


def _attempt_batch_boundary(created_count: int, baseline_tree_id: str) -> None:
    del created_count, baseline_tree_id


__all__ = [
    "AttemptWorkspace",
    "FinalizationRolledBack",
    "HeadPublicationIndeterminate",
    "SnapshotStore",
    "WorkspaceViolation",
    "commit_candidate",
]
