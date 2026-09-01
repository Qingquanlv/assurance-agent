"""Generic dual-root task staging and authenticated promotion.

The task actor receives an empty ``write_root``.  The project root is never
copied into that directory: only declared project outputs are fingerprinted and
later checked immediately before their explicitly enumerated replacements.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, cast

from graph_engine.attempts.keys import AttemptKey
from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.errors import GraphEngineError
from graph_engine.plugin_api import (
    DirectoryIdentity,
    PreparedWorkspaceRef,
    PromotionReceipt,
    ResourceClaims,
    SealedFile,
    SealedWriteSet,
    StagedFile,
    StagedWriteSet,
    TaskWorkspaceBinding,
    TaskWorkspaceIdentity,
)

_COPY_BUFFER_SIZE = 1024 * 1024
_FILE_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


class TaskWorkspaceViolation(GraphEngineError):
    """Raised when an untrusted task workspace fails closed."""


class PromotionPublicationIndeterminate(TaskWorkspaceViolation):
    """Raised when a prepared promotion has no safely terminal outcome yet."""


def _path_digest(path: Path) -> str:
    return canonical_digest({"path": str(path)})


def _safe_task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not task_id:
        raise TaskWorkspaceViolation("task id must be non-empty text")
    windows_path = PureWindowsPath(task_id)
    if task_id.startswith("/") or windows_path.is_absolute() or bool(windows_path.drive):
        raise TaskWorkspaceViolation("task id must not contain an absolute host path")
    try:
        return hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    except UnicodeError as error:
        raise TaskWorkspaceViolation("task id must be valid UTF-8") from error


def _attempt_id(attempt: int) -> str:
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise TaskWorkspaceViolation("attempt must be a positive integer")
    return f"attempt-{attempt}"


def _validate_logical_path(value: object) -> str:
    if not isinstance(value, str):
        raise TaskWorkspaceViolation("output path must be a string")
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
        raise TaskWorkspaceViolation(f"invalid project-relative output path: {value!r}")
    return value


def _normalise_output_paths(output_paths: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(_validate_logical_path(value) for value in output_paths)
    if len(normalized) != len(set(normalized)):
        raise TaskWorkspaceViolation("output paths must be a tuple of unique paths")
    return normalized


def _is_covered(path: str, claims: Sequence[str]) -> bool:
    return any(path == claim or path.startswith(f"{claim}/") for claim in claims)


def _lstat(path: Path, label: str) -> os.stat_result:
    try:
        return path.lstat()
    except FileNotFoundError:
        raise
    except OSError as error:
        raise TaskWorkspaceViolation(f"cannot inspect {label}: {path.name}") from error


def _require_directory(path: Path, label: str) -> os.stat_result:
    value = _lstat(path, label)
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        raise TaskWorkspaceViolation(f"{label} must be a real directory")
    return value


def _read_regular(path: Path, logical_path: str) -> tuple[bytes, str]:
    before = _lstat(path, "regular file")
    if stat.S_ISLNK(before.st_mode):
        raise TaskWorkspaceViolation(f"symlink is not allowed: {logical_path}")
    if not stat.S_ISREG(before.st_mode):
        raise TaskWorkspaceViolation(f"path is not a regular file: {logical_path}")
    if before.st_nlink != 1:
        raise TaskWorkspaceViolation(f"regular file must have one link: {logical_path}")
    try:
        descriptor = os.open(path, _FILE_READ_FLAGS)
    except OSError as error:
        raise TaskWorkspaceViolation(f"cannot open regular file: {logical_path}") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise TaskWorkspaceViolation(f"regular file identity changed while opening: {logical_path}")
        chunks: list[bytes] = []
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, _COPY_BUFFER_SIZE):
            chunks.append(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        final = _lstat(path, "regular file")
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) or (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ):
            raise TaskWorkspaceViolation(f"regular file changed during read: {logical_path}")
        return b"".join(chunks), digest.hexdigest()
    finally:
        os.close(descriptor)


def _claim_target(project_root: Path, claim: str) -> Path:
    current = project_root
    segments = claim.split("/")
    for index, segment in enumerate(segments):
        candidate = current / segment
        try:
            entry = _lstat(candidate, "claim path")
        except FileNotFoundError:
            return candidate
        if stat.S_ISLNK(entry.st_mode):
            raise TaskWorkspaceViolation(f"symlink is not allowed in claim path: {claim}")
        if index == len(segments) - 1:
            return candidate
        else:
            if not stat.S_ISDIR(entry.st_mode):
                raise TaskWorkspaceViolation(f"claim path parent is not a directory: {claim}")
            current = candidate
    raise AssertionError("validated claim path has at least one segment")


def _directory_identity(path: Path) -> tuple[int, int]:
    value = _require_directory(path, "claim-scan exclusion")
    return (value.st_dev, value.st_ino)


def _is_excluded_directory(entry: os.stat_result, excluded: frozenset[tuple[int, int]]) -> bool:
    return stat.S_ISDIR(entry.st_mode) and (entry.st_dev, entry.st_ino) in excluded


def _scan_directory(
    path: Path,
    prefix: str,
    excluded: frozenset[tuple[int, int]] = frozenset(),
) -> dict[str, str]:
    _require_directory(path, "directory")
    files: dict[str, str] = {}
    try:
        entries = sorted(path.iterdir(), key=lambda entry: os.fsencode(entry.name))
    except OSError as error:
        raise TaskWorkspaceViolation(f"cannot enumerate directory: {prefix}") from error
    for entry in entries:
        logical_path = f"{prefix}/{entry.name}" if prefix else entry.name
        value = _lstat(entry, "directory entry")
        if _is_excluded_directory(value, excluded):
            continue
        if stat.S_ISLNK(value.st_mode):
            raise TaskWorkspaceViolation(f"symlink is not allowed: {logical_path}")
        if stat.S_ISDIR(value.st_mode):
            files.update(_scan_directory(entry, logical_path, excluded))
        elif stat.S_ISREG(value.st_mode):
            _contents, digest = _read_regular(entry, logical_path)
            files[logical_path] = digest
        else:
            raise TaskWorkspaceViolation(f"path is not a regular file: {logical_path}")
    return files


def _manifest_for_claims(
    project_root: Path,
    claims: Sequence[str],
    excluded: frozenset[tuple[int, int]] = frozenset(),
) -> dict[str, str]:
    _require_directory(project_root, "project root")
    manifest: dict[str, str] = {}
    for claim in claims:
        target = _claim_target(project_root, claim)
        try:
            entry = _lstat(target, "claim target")
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(entry.st_mode):
            raise TaskWorkspaceViolation(f"symlink is not allowed: {claim}")
        if _is_excluded_directory(entry, excluded):
            continue
        if stat.S_ISDIR(entry.st_mode):
            scanned = _scan_directory(target, claim, excluded)
        elif stat.S_ISREG(entry.st_mode):
            _contents, digest = _read_regular(target, claim)
            scanned = {claim: digest}
        else:
            raise TaskWorkspaceViolation(f"path is not a regular file: {claim}")
        for path, digest in scanned.items():
            previous = manifest.setdefault(path, digest)
            if previous != digest:
                raise TaskWorkspaceViolation(f"claim scan changed while reading: {path}")
    return manifest


def _scan_staged_root(write_root: Path, claims: Sequence[str]) -> dict[str, str]:
    _require_directory(write_root, "attempt write root")
    staged = _scan_directory(write_root, "")
    for path in staged:
        if not _is_covered(path, claims):
            raise TaskWorkspaceViolation(f"staged path is not covered by a write claim: {path}")
    return staged


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except OSError as error:
        raise TaskWorkspaceViolation(f"cannot open directory for fsync: {path.name}") from error
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, content: bytes) -> None:
    parent = path.parent
    _require_directory(parent, "atomic-write parent")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary = Path(temporary_name)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise TaskWorkspaceViolation("filesystem write returned zero bytes")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        _fsync_directory(parent)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _receipt(identity_digest: str, staged_digest: str) -> PromotionReceipt:
    payload = {
        "identity_digest": identity_digest,
        "staged_digest": staged_digest,
        "layout_schema_version": "1",
    }
    return PromotionReceipt(
        identity_digest=identity_digest,
        staged_digest=staged_digest,
        receipt_digest=canonical_digest(cast("JSONValue", payload)),
    )


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, stat.S_IFMT(left.st_mode)) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
    )


def _open_pinned_directory(path: Path, label: str) -> int:
    try:
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except OSError as error:
        raise TaskWorkspaceViolation(f"cannot open {label}") from error
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode):
        os.close(descriptor)
        raise TaskWorkspaceViolation(f"{label} must be a real directory")
    return descriptor


def _stat_at(parent_fd: int, name: str, label: str) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise TaskWorkspaceViolation(f"cannot inspect {label}: {name}") from error


def _open_directory_at(parent_fd: int, name: str, label: str) -> int:
    before = _stat_at(parent_fd, name, label)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise TaskWorkspaceViolation(f"{label} must be a real directory")
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        raise TaskWorkspaceViolation(f"cannot open {label}: {name}") from error
    opened = os.fstat(descriptor)
    try:
        final = _stat_at(parent_fd, name, label)
    except BaseException:
        os.close(descriptor)
        raise
    if not stat.S_ISDIR(opened.st_mode) or not _same_inode(before, opened) or not _same_inode(opened, final):
        os.close(descriptor)
        raise TaskWorkspaceViolation(f"{label} changed while opening: {name}")
    return descriptor


def _open_or_create_directory_at(parent_fd: int, name: str, label: str) -> int:
    try:
        return _open_directory_at(parent_fd, name, label)
    except FileNotFoundError:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileExistsError:
            pass
        except OSError as error:
            raise TaskWorkspaceViolation(f"cannot create {label}: {name}") from error
        return _open_directory_at(parent_fd, name, label)


def _open_parent_at(root_fd: int, logical_path: str, *, create: bool, label: str) -> tuple[int, str]:
    segments = _validate_logical_path(logical_path).split("/")
    descriptor = os.dup(root_fd)
    try:
        for segment in segments[:-1]:
            child = (
                _open_or_create_directory_at(descriptor, segment, label)
                if create
                else _open_directory_at(descriptor, segment, label)
            )
            os.close(descriptor)
            descriptor = child
        return descriptor, segments[-1]
    except BaseException:
        os.close(descriptor)
        raise


def _read_regular_at(parent_fd: int, name: str, logical_path: str) -> tuple[bytes, str, int]:
    before = _stat_at(parent_fd, name, "regular file")
    if stat.S_ISLNK(before.st_mode):
        raise TaskWorkspaceViolation(f"symlink is not allowed: {logical_path}")
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise TaskWorkspaceViolation(f"path is not a private regular file: {logical_path}")
    try:
        descriptor = os.open(name, _FILE_READ_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        raise TaskWorkspaceViolation(f"cannot open regular file: {logical_path}") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or not _same_inode(before, opened):
            raise TaskWorkspaceViolation(f"regular file changed while opening: {logical_path}")
        chunks: list[bytes] = []
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, _COPY_BUFFER_SIZE):
            chunks.append(chunk)
            digest.update(chunk)
        final = _stat_at(parent_fd, name, "regular file")
        if (
            not _same_inode(opened, final)
            or final.st_size != opened.st_size
            or final.st_mode != opened.st_mode
            or final.st_mtime_ns != opened.st_mtime_ns
            or final.st_ctime_ns != opened.st_ctime_ns
        ):
            raise TaskWorkspaceViolation(f"regular file changed during read: {logical_path}")
        return b"".join(chunks), digest.hexdigest(), stat.S_IMODE(opened.st_mode)
    finally:
        os.close(descriptor)


def _scan_directory_fd(
    directory_fd: int,
    prefix: str,
    excluded: frozenset[tuple[int, int]] = frozenset(),
) -> dict[str, tuple[str, int]]:
    return {
        path: (digest, mode)
        for path, (_contents, digest, mode) in _scan_directory_fd_complete(
            directory_fd, prefix, excluded
        ).items()
    }


def _scan_directory_fd_complete(
    directory_fd: int,
    prefix: str,
    excluded: frozenset[tuple[int, int]] = frozenset(),
) -> dict[str, tuple[bytes, str, int]]:
    files: dict[str, tuple[bytes, str, int]] = {}
    try:
        names = sorted(os.listdir(directory_fd), key=os.fsencode)
    except OSError as error:
        raise TaskWorkspaceViolation(f"cannot enumerate directory: {prefix}") from error
    for name in names:
        logical_path = f"{prefix}/{name}" if prefix else name
        entry = _stat_at(directory_fd, name, "directory entry")
        if _is_excluded_directory(entry, excluded):
            continue
        if stat.S_ISLNK(entry.st_mode):
            raise TaskWorkspaceViolation(f"symlink is not allowed: {logical_path}")
        if stat.S_ISDIR(entry.st_mode):
            child = _open_directory_at(directory_fd, name, "directory entry")
            try:
                files.update(_scan_directory_fd_complete(child, logical_path, excluded))
            finally:
                os.close(child)
        elif stat.S_ISREG(entry.st_mode):
            contents, digest, mode = _read_regular_at(directory_fd, name, logical_path)
            files[logical_path] = (contents, digest, mode)
        else:
            raise TaskWorkspaceViolation(f"path is not a regular file: {logical_path}")
    return files


def _sealed_digest(identity_digest: str, files: Sequence[SealedFile]) -> str:
    return canonical_digest(
        {
            "identity_digest": identity_digest,
            "files": [
                {
                    "path": item.path,
                    "before_sha256": item.before_sha256,
                    "before_mode": item.before_mode,
                    "after_sha256": item.after_sha256,
                    "after_mode": item.after_mode,
                }
                for item in files
            ],
        }
    )


def _manifest_for_claims_fd(
    root_fd: int,
    claims: Sequence[str],
    excluded: frozenset[tuple[int, int]] = frozenset(),
) -> dict[str, tuple[str, int]]:
    manifest: dict[str, tuple[str, int]] = {}
    for claim in claims:
        try:
            parent, name = _open_parent_at(root_fd, claim, create=False, label="claim parent")
        except FileNotFoundError:
            continue
        try:
            try:
                entry = _stat_at(parent, name, "claim target")
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(entry.st_mode):
                raise TaskWorkspaceViolation(f"symlink is not allowed: {claim}")
            if _is_excluded_directory(entry, excluded):
                continue
            if stat.S_ISDIR(entry.st_mode):
                child = _open_directory_at(parent, name, "claim target")
                try:
                    scanned = _scan_directory_fd(child, claim, excluded)
                finally:
                    os.close(child)
            elif stat.S_ISREG(entry.st_mode):
                _contents, digest, mode = _read_regular_at(parent, name, claim)
                scanned = {claim: (digest, mode)}
            else:
                raise TaskWorkspaceViolation(f"path is not a regular file: {claim}")
            manifest.update(scanned)
        finally:
            os.close(parent)
    return manifest


def _atomic_write_at(parent_fd: int, name: str, content: bytes) -> None:
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise TaskWorkspaceViolation("filesystem write returned zero bytes")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except BaseException:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except OSError:
            pass
        raise


def _write_named_file_at(parent_fd: int, name: str, content: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise TaskWorkspaceViolation("filesystem write returned zero bytes")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.fsync(parent_fd)
    except BaseException:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.unlink(name, dir_fd=parent_fd)
        except OSError:
            pass
        raise


def _open_transaction_parent_at(
    root_fd: int,
    logical_path: str,
    created_directories: list[str],
) -> tuple[int, str]:
    segments = _validate_logical_path(logical_path).split("/")
    descriptor = os.dup(root_fd)
    traversed: list[str] = []
    try:
        for segment in segments[:-1]:
            traversed.append(segment)
            try:
                child = _open_directory_at(descriptor, segment, "promotion target parent")
            except FileNotFoundError:
                os.mkdir(segment, mode=0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                created_directories.append("/".join(traversed))
                child = _open_directory_at(descriptor, segment, "promotion target parent")
            os.close(descriptor)
            descriptor = child
        return descriptor, segments[-1]
    except BaseException:
        os.close(descriptor)
        raise


def _remove_created_directories(root_fd: int, created_directories: Sequence[str]) -> None:
    for logical_path in reversed(created_directories):
        try:
            parent_fd, name = _open_parent_at(
                root_fd,
                logical_path,
                create=False,
                label="created promotion directory",
            )
        except FileNotFoundError:
            continue
        try:
            os.rmdir(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(parent_fd)


def _promotion_transaction_cut(name: str) -> None:
    del name


@dataclass(slots=True)
class _PreparedPromotionFile:
    file: StagedFile
    parent_fd: int
    target_name: str
    temporary_name: str
    backup_name: str


class TaskWorkspaceStore:
    """Binds an immutable project baseline to an empty per-task write root."""

    def __init__(self, project_root: Path, attempts_root: Path, receipts_root: Path) -> None:
        self.project_root = Path(project_root).resolve(strict=True)
        self.attempts_root = Path(attempts_root).resolve()
        self.receipts_root = Path(receipts_root).resolve()
        _require_directory(self.project_root, "project root")
        self.attempts_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.receipts_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _require_directory(self.attempts_root, "attempts root")
        _require_directory(self.receipts_root, "receipts root")
        self._excluded_claim_identities = frozenset(
            {
                _directory_identity(self.attempts_root),
                _directory_identity(self.receipts_root.parent),
            }
        )
        self._project_fd = _open_pinned_directory(self.project_root, "project root")
        self._attempts_fd = _open_pinned_directory(self.attempts_root, "attempts root")
        self._receipts_fd = _open_pinned_directory(self.receipts_root, "receipts root")
        try:
            self.project_root_identity = DirectoryIdentity.capture(
                self.project_root, descriptor=self._project_fd
            )
            self.attempts_root_identity = DirectoryIdentity.capture(
                self.attempts_root, descriptor=self._attempts_fd
            )
            self.receipts_root_identity = DirectoryIdentity.capture(
                self.receipts_root, descriptor=self._receipts_fd
            )
        except BaseException:
            self.close()
            raise

    def _authenticate_roots_current(self) -> None:
        try:
            current = (
                DirectoryIdentity.capture(self.project_root, descriptor=self._project_fd),
                DirectoryIdentity.capture(self.attempts_root, descriptor=self._attempts_fd),
                DirectoryIdentity.capture(self.receipts_root, descriptor=self._receipts_fd),
            )
        except ValueError as error:
            raise TaskWorkspaceViolation("task workspace root entry was replaced") from error
        expected = (
            self.project_root_identity,
            self.attempts_root_identity,
            self.receipts_root_identity,
        )
        if current != expected:
            raise TaskWorkspaceViolation("task workspace root identity drifted")

    def close(self) -> None:
        for attribute in ("_project_fd", "_attempts_fd", "_receipts_fd"):
            descriptor = getattr(self, attribute, None)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, attribute, None)

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass

    def _open_task_root(self, identity: TaskWorkspaceIdentity) -> int:
        return _open_directory_at(self._attempts_fd, _safe_task_id(identity.task_id), "task attempts root")

    def _open_write_root(self, identity: TaskWorkspaceIdentity) -> tuple[int, int]:
        task_fd = self._open_task_root(identity)
        try:
            write_fd = _open_directory_at(task_fd, identity.attempt_id, "attempt write root")
        except BaseException:
            os.close(task_fd)
            raise
        return task_fd, write_fd

    def _binding_paths(self, identity: TaskWorkspaceIdentity) -> tuple[Path, Path]:
        self._authenticate_roots_current()
        self._authenticate_identity(identity)
        safe_task_id = _safe_task_id(identity.task_id)
        task_root = self.attempts_root / safe_task_id
        write_root = task_root / identity.attempt_id
        if write_root.parent != task_root or task_root.parent != self.attempts_root:
            raise TaskWorkspaceViolation("attempt write root escaped its trusted namespace")
        task_fd = self._open_task_root(identity)
        try:
            recorded = self._read_identity_at(task_fd, f".{identity.attempt_id}.identity.json")
            if recorded != identity:
                raise TaskWorkspaceViolation("task workspace identity drifted from its recorded binding")
            write_fd = _open_directory_at(task_fd, identity.attempt_id, "attempt write root")
            try:
                write_identity = DirectoryIdentity.capture(write_root, descriptor=write_fd)
            finally:
                os.close(write_fd)
            if write_identity.identity_digest != identity.write_root_digest:
                raise TaskWorkspaceViolation("attempt write-root identity was replaced")
        finally:
            os.close(task_fd)
        return task_root, write_root

    def _authenticate_identity(self, identity: TaskWorkspaceIdentity) -> None:
        if identity.project_digest != self.project_root_identity.identity_digest:
            raise TaskWorkspaceViolation("task workspace project identity drifted")
        if identity.attempt_id != _attempt_id(identity.attempt):
            raise TaskWorkspaceViolation("task workspace attempt id does not match its attempt")
        expected = canonical_digest(identity.model_dump(mode="json", exclude={"identity_digest"}))
        if identity.identity_digest != expected:
            raise TaskWorkspaceViolation("task workspace identity digest is not canonical")

    def _authenticate_staged(self, identity: TaskWorkspaceIdentity, staged: StagedWriteSet) -> None:
        if staged.identity_digest != identity.identity_digest:
            raise TaskWorkspaceViolation("staged write set belongs to another task workspace identity")
        expected = canonical_digest(
            {
                "identity_digest": staged.identity_digest,
                "files": [file.model_dump(mode="json") for file in staged.files],
            }
        )
        if staged.staged_digest != expected:
            raise TaskWorkspaceViolation("staged write-set digest is not canonical")
        for file in staged.files:
            if (
                file.after_sha256 is None
                or file.after_mode is None
                or not _is_covered(file.path, identity.output_paths)
            ):
                raise TaskWorkspaceViolation("staged write set contains an undeclared file")

    def begin(
        self,
        *,
        task_id: str,
        attempt: int,
        output_paths: Sequence[str],
    ) -> TaskWorkspaceBinding:
        self._authenticate_roots_current()
        claims = _normalise_output_paths(output_paths)
        safe_task_id = _safe_task_id(task_id)
        attempt_id = _attempt_id(attempt)
        task_root = self.attempts_root / safe_task_id
        write_root = task_root / attempt_id
        task_fd = _open_or_create_directory_at(self._attempts_fd, safe_task_id, "task attempts root")
        identity_name = f".{attempt_id}.identity.json"
        try:
            try:
                recorded = self._read_identity_at(task_fd, identity_name)
            except FileNotFoundError:
                recorded = None
            if recorded is not None:
                self._authenticate_identity(recorded)
                if (
                    recorded.task_id != task_id
                    or recorded.attempt != attempt
                    or recorded.output_paths != claims
                ):
                    raise TaskWorkspaceViolation("attempt root already belongs to another identity")
                write_fd = _open_directory_at(task_fd, attempt_id, "attempt write root")
                try:
                    write_identity = DirectoryIdentity.capture(write_root, descriptor=write_fd)
                finally:
                    os.close(write_fd)
                if write_identity.identity_digest != recorded.write_root_digest:
                    raise TaskWorkspaceViolation("attempt write-root identity was replaced")
                return TaskWorkspaceBinding(
                    recorded,
                    self.project_root,
                    write_root,
                    self.project_root_identity,
                    write_identity,
                )
            try:
                _stat_at(task_fd, attempt_id, "attempt write root")
            except FileNotFoundError:
                pass
            else:
                raise TaskWorkspaceViolation("attempt write root exists without an authenticated identity")
            baseline = _manifest_for_claims_fd(
                self._project_fd,
                claims,
                self._excluded_claim_identities,
            )
            baseline_files = tuple(
                StagedFile(path=path, before_sha256=digest, before_mode=mode)
                for path, (digest, mode) in sorted(baseline.items())
            )
            os.mkdir(attempt_id, mode=0o700, dir_fd=task_fd)
            os.fsync(task_fd)
            write_fd = _open_directory_at(task_fd, attempt_id, "attempt write root")
            try:
                write_identity = DirectoryIdentity.capture(write_root, descriptor=write_fd)
            finally:
                os.close(write_fd)
            payload: dict[str, Any] = {
                "task_id": task_id,
                "attempt": attempt,
                "attempt_id": attempt_id,
                "output_paths": list(claims),
                "baseline_files": [file.model_dump(mode="json") for file in baseline_files],
                "project_digest": self.project_root_identity.identity_digest,
                "write_root_digest": write_identity.identity_digest,
                "layout_schema_version": "1",
            }
            identity = TaskWorkspaceIdentity(identity_digest=canonical_digest(payload), **payload)
            _atomic_write_at(task_fd, identity_name, canonical_json_bytes(identity.model_dump(mode="json")))
            return TaskWorkspaceBinding(
                identity,
                self.project_root,
                write_root,
                self.project_root_identity,
                write_identity,
            )
        finally:
            os.close(task_fd)

    def seal(self, identity: TaskWorkspaceIdentity) -> StagedWriteSet:
        self._binding_paths(identity)
        task_fd, write_fd = self._open_write_root(identity)
        try:
            manifest = _scan_directory_fd(write_fd, "")
        finally:
            os.close(write_fd)
            os.close(task_fd)
        for path in manifest:
            if not _is_covered(path, identity.output_paths):
                raise TaskWorkspaceViolation(f"staged path is not covered by a write claim: {path}")
        baseline = {file.path: (file.before_sha256, file.before_mode) for file in identity.baseline_files}
        files = tuple(
            StagedFile(
                path=path,
                before_sha256=(baseline[path][0] if path in baseline else None),
                before_mode=(baseline[path][1] if path in baseline else None),
                after_sha256=digest,
                after_mode=mode,
            )
            for path, (digest, mode) in sorted(manifest.items())
        )
        payload = {
            "identity_digest": identity.identity_digest,
            "files": [file.model_dump(mode="json") for file in files],
        }
        return StagedWriteSet(staged_digest=canonical_digest(payload), **payload)

    def _read_identity_at(self, parent_fd: int, name: str) -> TaskWorkspaceIdentity:
        try:
            content, _digest, _mode = _read_regular_at(parent_fd, name, "task workspace identity")
            return TaskWorkspaceIdentity.model_validate_json(content)
        except FileNotFoundError:
            raise
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise TaskWorkspaceViolation("cannot authenticate recorded task workspace identity") from error

    def _receipt_path(self, identity: TaskWorkspaceIdentity) -> Path:
        return self.receipts_root / f"{identity.identity_digest}.json"

    def _pending_path(self, identity: TaskWorkspaceIdentity) -> Path:
        return self.receipts_root / f".{identity.identity_digest}.pending.json"

    def _read_receipt_at(self, name: str) -> PromotionReceipt:
        try:
            content, _digest, _mode = _read_regular_at(self._receipts_fd, name, "promotion receipt")
            return PromotionReceipt.model_validate_json(content)
        except FileNotFoundError:
            raise
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise TaskWorkspaceViolation("cannot authenticate promotion receipt") from error

    def _verify_target_baseline(self, identity: TaskWorkspaceIdentity) -> None:
        current = _manifest_for_claims_fd(
            self._project_fd,
            identity.output_paths,
            self._excluded_claim_identities,
        )
        expected = {file.path: (file.before_sha256, file.before_mode) for file in identity.baseline_files}
        if current != expected:
            raise TaskWorkspaceViolation("target drift since task workspace begin")

    def _staged_matches_root(self, identity: TaskWorkspaceIdentity, staged: StagedWriteSet) -> None:
        current = self.seal(identity)
        if current != staged:
            raise TaskWorkspaceViolation("staged write root drifted after sealing")

    def _target_state(self, file: StagedFile) -> tuple[str, int] | None:
        try:
            parent, name = _open_parent_at(self._project_fd, file.path, create=False, label="target parent")
        except FileNotFoundError:
            return None
        try:
            try:
                _contents, digest, mode = _read_regular_at(parent, name, file.path)
            except FileNotFoundError:
                return None
            return digest, mode
        finally:
            os.close(parent)

    def _targets_match_staged(self, staged: StagedWriteSet) -> bool:
        for file in staged.files:
            assert file.after_sha256 is not None and file.after_mode is not None
            try:
                digest = self._target_state(file)
            except (FileNotFoundError, TaskWorkspaceViolation):
                return False
            if digest != (file.after_sha256, file.after_mode):
                return False
        return True

    @staticmethod
    def _before_state(file: StagedFile) -> tuple[str, int] | None:
        if file.before_sha256 is None:
            return None
        assert file.before_mode is not None
        return file.before_sha256, file.before_mode

    @staticmethod
    def _after_state(file: StagedFile) -> tuple[str, int]:
        assert file.after_sha256 is not None and file.after_mode is not None
        return file.after_sha256, file.after_mode

    @staticmethod
    def _transaction_names(
        file: StagedFile,
        *,
        index: int,
        identity: TaskWorkspaceIdentity,
        staged: StagedWriteSet,
    ) -> tuple[str, str]:
        target_name = file.path.rsplit("/", 1)[-1]
        token = canonical_digest(
            {
                "identity_digest": identity.identity_digest,
                "staged_digest": staged.staged_digest,
                "index": index,
                "path": file.path,
            }
        )[:24]
        return f".{target_name}.{token}.tmp", f".{target_name}.{token}.rollback"

    @staticmethod
    def _ensure_named_file(
        parent_fd: int,
        name: str,
        content: bytes,
        digest: str,
        mode: int,
        logical_path: str,
    ) -> None:
        try:
            existing_content, existing_digest, existing_mode = _read_regular_at(parent_fd, name, logical_path)
        except FileNotFoundError:
            _write_named_file_at(parent_fd, name, content, mode)
            return
        if existing_content != content or existing_digest != digest or existing_mode != mode:
            raise TaskWorkspaceViolation(f"promotion transaction file conflicts: {logical_path}")

    @staticmethod
    def _unlink_transaction_name(parent_fd: int, name: str) -> None:
        try:
            os.unlink(name, dir_fd=parent_fd)
        except FileNotFoundError:
            return

    def _cleanup_transaction_files(self, prepared: Sequence[_PreparedPromotionFile]) -> None:
        for item in prepared:
            self._unlink_transaction_name(item.parent_fd, item.temporary_name)
            self._unlink_transaction_name(item.parent_fd, item.backup_name)
            os.fsync(item.parent_fd)

    def _rollback_transaction(
        self,
        prepared: Sequence[_PreparedPromotionFile],
        created_directories: Sequence[str],
    ) -> None:
        failures: list[BaseException] = []
        for item in reversed(prepared):
            try:
                current = self._target_state_at(item.parent_fd, item.target_name, item.file.path)
                before = self._before_state(item.file)
                after = self._after_state(item.file)
                if current == after:
                    if before is None:
                        os.unlink(item.target_name, dir_fd=item.parent_fd)
                    else:
                        _contents, backup_digest, backup_mode = _read_regular_at(
                            item.parent_fd,
                            item.backup_name,
                            f"rollback:{item.file.path}",
                        )
                        if (backup_digest, backup_mode) != before:
                            raise TaskWorkspaceViolation(
                                f"promotion rollback backup drifted: {item.file.path}"
                            )
                        os.replace(
                            item.backup_name,
                            item.target_name,
                            src_dir_fd=item.parent_fd,
                            dst_dir_fd=item.parent_fd,
                        )
                    os.fsync(item.parent_fd)
                elif current != before:
                    raise TaskWorkspaceViolation(
                        f"promotion rollback encountered target drift: {item.file.path}"
                    )
            except BaseException as error:
                failures.append(error)
        for item in prepared:
            try:
                current = self._target_state_at(item.parent_fd, item.target_name, item.file.path)
                if current != self._before_state(item.file):
                    failures.append(
                        TaskWorkspaceViolation(
                            f"promotion rollback could not prove the baseline: {item.file.path}"
                        )
                    )
            except BaseException as error:
                failures.append(error)
        if failures:
            failure = PromotionPublicationIndeterminate("promotion rollback is indeterminate")
            for error in failures:
                failure.add_note(f"rollback failure: {type(error).__name__}: {error}")
            raise failure
        self._cleanup_transaction_files(prepared)
        _remove_created_directories(self._project_fd, created_directories)

    def _execute_promotion_transaction(
        self,
        identity: TaskWorkspaceIdentity,
        staged: StagedWriteSet,
        *,
        contents: Mapping[str, bytes] | None = None,
    ) -> None:
        prepared: list[_PreparedPromotionFile] = []
        created_directories: list[str] = []
        task_fd: int | None = None
        write_fd: int | None = None
        if contents is None:
            task_fd, write_fd = self._open_write_root(identity)
        try:
            try:
                for index, file in enumerate(staged.files):
                    if contents is not None:
                        file_bytes = contents[file.path]
                        digest = hashlib.sha256(file_bytes).hexdigest()
                        mode = self._after_state(file)[1]
                        if (digest, mode) != self._after_state(file):
                            raise TaskWorkspaceViolation(f"sealed file drifted before promotion: {file.path}")
                    else:
                        assert write_fd is not None
                        source_parent, source_name = _open_parent_at(
                            write_fd,
                            file.path,
                            create=False,
                            label="staged parent",
                        )
                        try:
                            file_bytes, digest, mode = _read_regular_at(source_parent, source_name, file.path)
                        finally:
                            os.close(source_parent)
                        if (digest, mode) != self._after_state(file):
                            raise TaskWorkspaceViolation(f"staged file drifted before promotion: {file.path}")
                    target_parent, target_name = _open_transaction_parent_at(
                        self._project_fd,
                        file.path,
                        created_directories,
                    )
                    temporary_name, backup_name = self._transaction_names(
                        file,
                        index=index,
                        identity=identity,
                        staged=staged,
                    )
                    item = _PreparedPromotionFile(
                        file=file,
                        parent_fd=target_parent,
                        target_name=target_name,
                        temporary_name=temporary_name,
                        backup_name=backup_name,
                    )
                    prepared.append(item)
                    current = self._target_state_at(target_parent, target_name, file.path)
                    before = self._before_state(file)
                    after = self._after_state(file)
                    if current not in {before, after}:
                        raise TaskWorkspaceViolation(f"prior multi-file promotion is incomplete: {file.path}")
                    if before is not None:
                        if current == before:
                            baseline_contents, baseline_digest, baseline_mode = _read_regular_at(
                                target_parent, target_name, file.path
                            )
                            self._ensure_named_file(
                                target_parent,
                                backup_name,
                                baseline_contents,
                                baseline_digest,
                                baseline_mode,
                                f"rollback:{file.path}",
                            )
                        else:
                            try:
                                _backup, backup_digest, backup_mode = _read_regular_at(
                                    target_parent,
                                    backup_name,
                                    f"rollback:{file.path}",
                                )
                            except FileNotFoundError as error:
                                raise TaskWorkspaceViolation(
                                    f"promoted target lacks rollback evidence: {file.path}"
                                ) from error
                            if (backup_digest, backup_mode) != before:
                                raise TaskWorkspaceViolation(
                                    f"promotion rollback backup drifted: {file.path}"
                                )
                    else:
                        try:
                            _stat_at(target_parent, backup_name, "unexpected rollback backup")
                        except FileNotFoundError:
                            pass
                        else:
                            raise TaskWorkspaceViolation(
                                f"new promotion target has an unexpected rollback backup: {file.path}"
                            )
                    if current == before:
                        self._ensure_named_file(
                            target_parent,
                            temporary_name,
                            file_bytes,
                            digest,
                            mode,
                            f"temporary:{file.path}",
                        )

                for item in prepared:
                    current = self._target_state_at(item.parent_fd, item.target_name, item.file.path)
                    if current == self._after_state(item.file):
                        continue
                    if current != self._before_state(item.file):
                        raise TaskWorkspaceViolation(
                            f"target drift immediately before replace: {item.file.path}"
                        )
                    os.replace(
                        item.temporary_name,
                        item.target_name,
                        src_dir_fd=item.parent_fd,
                        dst_dir_fd=item.parent_fd,
                    )
                    os.fsync(item.parent_fd)
                    if item is prepared[0] and len(prepared) > 1:
                        _promotion_transaction_cut("during_multi_file_promotion")
            except BaseException as error:
                try:
                    self._rollback_transaction(prepared, created_directories)
                except PromotionPublicationIndeterminate as rollback_error:
                    raise rollback_error from error
                except BaseException as cleanup_error:
                    error.add_note(
                        "rollback restored every canonical target but cleanup failed: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
                raise
        finally:
            for item in prepared:
                os.close(item.parent_fd)
            if write_fd is not None:
                os.close(write_fd)
            if task_fd is not None:
                os.close(task_fd)

    def _target_state_at(self, parent_fd: int, name: str, logical_path: str) -> tuple[str, int] | None:
        try:
            _contents, digest, mode = _read_regular_at(parent_fd, name, logical_path)
        except FileNotFoundError:
            return None
        return digest, mode

    def _cleanup_replay_artifacts(
        self,
        identity: TaskWorkspaceIdentity,
        staged: StagedWriteSet,
    ) -> None:
        for index, file in enumerate(staged.files):
            try:
                parent_fd, _target_name = _open_parent_at(
                    self._project_fd,
                    file.path,
                    create=False,
                    label="promotion target parent",
                )
            except FileNotFoundError:
                continue
            try:
                temporary_name, backup_name = self._transaction_names(
                    file,
                    index=index,
                    identity=identity,
                    staged=staged,
                )
                self._unlink_transaction_name(parent_fd, temporary_name)
                self._unlink_transaction_name(parent_fd, backup_name)
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)

    @staticmethod
    def _publication_prepared_name(expected_receipt: PromotionReceipt, purpose: str) -> str:
        return f".{expected_receipt.identity_digest}.{expected_receipt.receipt_digest}.{purpose}.prepared.tmp"

    @staticmethod
    def _publication_construction_prefix(expected_receipt: PromotionReceipt, purpose: str) -> str:
        return (
            f".{expected_receipt.identity_digest}.{expected_receipt.receipt_digest}.{purpose}.construction."
        )

    @staticmethod
    def _legacy_receipt_temporary_name(expected_receipt: PromotionReceipt) -> str:
        return f".{expected_receipt.identity_digest}.{expected_receipt.receipt_digest}.receipt.tmp"

    @staticmethod
    def _legacy_pending_construction_prefix(expected_receipt: PromotionReceipt) -> str:
        # Fix Round 2 passed a leading-dot pending name to ``_atomic_write_at``,
        # which prepended its own dot to the construction name.
        return f"..{expected_receipt.identity_digest}.pending.json."

    @staticmethod
    def _is_construction_name(name: str, prefix: str) -> bool:
        if not name.startswith(prefix) or not name.endswith(".tmp"):
            return False
        token = name[len(prefix) : -len(".tmp")]
        return len(token) == 32 and all(character in "0123456789abcdef" for character in token)

    def _publication_temporary_state(
        self,
        name: str,
        expected_receipt: PromotionReceipt,
        content: bytes,
        digest: str,
        *,
        label: str,
    ) -> str:
        try:
            existing_content, existing_digest, existing_mode = _read_regular_at(
                self._receipts_fd,
                name,
                label,
            )
        except FileNotFoundError:
            return "missing"
        if existing_content == content and existing_digest == digest and existing_mode == 0o600:
            return "expected"
        try:
            authenticated = PromotionReceipt.model_validate_json(existing_content)
        except ValueError:
            return "incomplete"
        if authenticated != expected_receipt:
            raise TaskWorkspaceViolation(f"authenticated {label} belongs to a different promotion intent")
        return "incomplete"

    def _cleanup_construction_files(
        self,
        expected_receipt: PromotionReceipt,
        content: bytes,
        digest: str,
        *,
        purpose: str,
    ) -> None:
        prefixes = [self._publication_construction_prefix(expected_receipt, purpose)]
        if purpose == "pending":
            prefixes.append(self._legacy_pending_construction_prefix(expected_receipt))
        try:
            names = sorted(os.listdir(self._receipts_fd), key=os.fsencode)
        except OSError as error:
            raise TaskWorkspaceViolation("cannot enumerate promotion construction files") from error
        removed = False
        for name in names:
            if not any(self._is_construction_name(name, prefix) for prefix in prefixes):
                continue
            state = self._publication_temporary_state(
                name,
                expected_receipt,
                content,
                digest,
                label=f"{purpose} construction temporary",
            )
            if state == "missing":
                continue
            self._unlink_transaction_name(self._receipts_fd, name)
            removed = True
        if removed:
            os.fsync(self._receipts_fd)

    def _recover_publication_prepared(
        self,
        expected_receipt: PromotionReceipt,
        content: bytes,
        digest: str,
        *,
        purpose: str,
    ) -> str | None:
        prepared_name = self._publication_prepared_name(expected_receipt, purpose)
        candidates = [prepared_name]
        if purpose == "receipt":
            candidates.append(self._legacy_receipt_temporary_name(expected_receipt))
        for name in candidates:
            state = self._publication_temporary_state(
                name,
                expected_receipt,
                content,
                digest,
                label=f"{purpose} prepared temporary",
            )
            if state == "missing":
                continue
            if state == "expected":
                if name != prepared_name:
                    os.replace(
                        name,
                        prepared_name,
                        src_dir_fd=self._receipts_fd,
                        dst_dir_fd=self._receipts_fd,
                    )
                    os.fsync(self._receipts_fd)
                return prepared_name
            self._unlink_transaction_name(self._receipts_fd, name)
            os.fsync(self._receipts_fd)
        return None

    def _prepare_publication_file(
        self,
        expected_receipt: PromotionReceipt,
        *,
        purpose: str,
    ) -> str:
        content = canonical_json_bytes(expected_receipt.model_dump(mode="json"))
        digest = hashlib.sha256(content).hexdigest()
        self._cleanup_construction_files(
            expected_receipt,
            content,
            digest,
            purpose=purpose,
        )
        prepared_name = self._recover_publication_prepared(
            expected_receipt,
            content,
            digest,
            purpose=purpose,
        )
        if prepared_name is not None:
            return prepared_name
        construction_name = (
            f"{self._publication_construction_prefix(expected_receipt, purpose)}{uuid.uuid4().hex}.tmp"
        )
        _write_named_file_at(self._receipts_fd, construction_name, content, 0o600)
        prepared_name = self._publication_prepared_name(expected_receipt, purpose)
        os.replace(
            construction_name,
            prepared_name,
            src_dir_fd=self._receipts_fd,
            dst_dir_fd=self._receipts_fd,
        )
        os.fsync(self._receipts_fd)
        return prepared_name

    def _cleanup_publication_temporaries(
        self,
        expected_receipt: PromotionReceipt,
        *,
        purpose: str,
    ) -> None:
        content = canonical_json_bytes(expected_receipt.model_dump(mode="json"))
        digest = hashlib.sha256(content).hexdigest()
        self._cleanup_construction_files(
            expected_receipt,
            content,
            digest,
            purpose=purpose,
        )
        names = [self._publication_prepared_name(expected_receipt, purpose)]
        if purpose == "receipt":
            names.append(self._legacy_receipt_temporary_name(expected_receipt))
        removed = False
        for name in names:
            state = self._publication_temporary_state(
                name,
                expected_receipt,
                content,
                digest,
                label=f"{purpose} prepared temporary",
            )
            if state == "missing":
                continue
            self._unlink_transaction_name(self._receipts_fd, name)
            removed = True
        if removed:
            os.fsync(self._receipts_fd)

    def _install_completed_receipt(
        self,
        expected_receipt: PromotionReceipt,
        *,
        receipt_name: str,
    ) -> None:
        temporary_name = self._prepare_publication_file(expected_receipt, purpose="receipt")
        os.replace(
            temporary_name,
            receipt_name,
            src_dir_fd=self._receipts_fd,
            dst_dir_fd=self._receipts_fd,
        )
        os.fsync(self._receipts_fd)
        installed = self._read_receipt_at(receipt_name)
        if installed != expected_receipt:
            raise TaskWorkspaceViolation("installed promotion receipt failed authentication")

    def _install_pending_receipt(
        self,
        expected_receipt: PromotionReceipt,
        *,
        pending_name: str,
    ) -> None:
        temporary_name = self._prepare_publication_file(expected_receipt, purpose="pending")
        os.replace(
            temporary_name,
            pending_name,
            src_dir_fd=self._receipts_fd,
            dst_dir_fd=self._receipts_fd,
        )
        os.fsync(self._receipts_fd)
        installed = self._read_receipt_at(pending_name)
        if installed != expected_receipt:
            raise TaskWorkspaceViolation("installed pending promotion receipt failed authentication")

    def _cleanup_completed_promotion(
        self,
        identity: TaskWorkspaceIdentity,
        staged: StagedWriteSet,
        expected_receipt: PromotionReceipt,
        *,
        pending_name: str,
    ) -> None:
        cleanup_steps = (
            lambda: self._cleanup_replay_artifacts(identity, staged),
            lambda: self._cleanup_publication_temporaries(
                expected_receipt,
                purpose="receipt",
            ),
            lambda: self._cleanup_publication_temporaries(
                expected_receipt,
                purpose="pending",
            ),
            lambda: self._remove_pending_receipt(pending_name),
        )
        for cleanup in cleanup_steps:
            try:
                cleanup()
            except Exception:
                # A durable completed receipt is the terminal authority.  Residual
                # authenticated transaction files are retried on the next replay.
                continue

    def _remove_pending_receipt(self, pending_name: str) -> None:
        try:
            os.unlink(pending_name, dir_fd=self._receipts_fd)
        except FileNotFoundError:
            pass
        os.fsync(self._receipts_fd)

    def _publish_completed_receipt(
        self,
        expected_receipt: PromotionReceipt,
        *,
        receipt_name: str,
    ) -> None:
        try:
            self._install_completed_receipt(
                expected_receipt,
                receipt_name=receipt_name,
            )
        except PromotionPublicationIndeterminate:
            raise
        except Exception as error:
            raise PromotionPublicationIndeterminate(
                "promotion receipt publication is indeterminate"
            ) from error

    def _confirm_promotion_source(
        self,
        identity: TaskWorkspaceIdentity,
        staged: StagedWriteSet,
        contents: Mapping[str, bytes] | None,
    ) -> None:
        if contents is None:
            self._staged_matches_root(identity, staged)
            return
        for file in staged.files:
            digest = hashlib.sha256(contents[file.path]).hexdigest()
            if file.after_sha256 is None or digest != file.after_sha256:
                raise TaskWorkspaceViolation(f"sealed candidate bytes drifted after sealing: {file.path}")

    def promote(
        self,
        identity: TaskWorkspaceIdentity,
        staged: StagedWriteSet,
        *,
        contents: Mapping[str, bytes] | None = None,
    ) -> PromotionReceipt:
        self._authenticate_roots_current()
        self._authenticate_identity(identity)
        self._authenticate_staged(identity, staged)
        self._binding_paths(identity)
        receipt_name = f"{identity.identity_digest}.json"
        expected_receipt = _receipt(identity.identity_digest, staged.staged_digest)
        try:
            existing = self._read_receipt_at(receipt_name)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if existing != expected_receipt:
                raise TaskWorkspaceViolation("existing promotion receipt conflicts with this replay")
            self._confirm_promotion_source(identity, staged, contents)
            if not self._targets_match_staged(staged):
                raise TaskWorkspaceViolation("completed promotion replay does not match staged targets")
            try:
                os.fsync(self._receipts_fd)
            except Exception as error:
                raise PromotionPublicationIndeterminate(
                    "promotion receipt durability is indeterminate"
                ) from error
            self._cleanup_completed_promotion(
                identity,
                staged,
                expected_receipt,
                pending_name=f".{identity.identity_digest}.pending.json",
            )
            return existing
        pending_name = f".{identity.identity_digest}.pending.json"
        try:
            pending = self._read_receipt_at(pending_name)
        except FileNotFoundError:
            pending = None
        if pending is not None:
            if pending != expected_receipt:
                raise TaskWorkspaceViolation("existing pending promotion conflicts with this replay")
            self._confirm_promotion_source(identity, staged, contents)
            for file in staged.files:
                state = self._target_state(file)
                if state in {self._before_state(file), self._after_state(file)}:
                    continue
                raise PromotionPublicationIndeterminate(
                    "prior multi-file promotion is incomplete with an unauthenticated target state"
                )
            if self._targets_match_staged(staged):
                self._publish_completed_receipt(
                    expected_receipt,
                    receipt_name=receipt_name,
                )
                self._cleanup_completed_promotion(
                    identity,
                    staged,
                    expected_receipt,
                    pending_name=pending_name,
                )
                return expected_receipt
        else:
            self._verify_target_baseline(identity)
            self._confirm_promotion_source(identity, staged, contents)
            self._install_pending_receipt(
                expected_receipt,
                pending_name=pending_name,
            )
        self._execute_promotion_transaction(identity, staged, contents=contents)
        if not self._targets_match_staged(staged):
            raise PromotionPublicationIndeterminate(
                "promotion targets cannot be proven to match the staged write set"
            )
        self._publish_completed_receipt(
            expected_receipt,
            receipt_name=receipt_name,
        )
        self._cleanup_completed_promotion(
            identity,
            staged,
            expected_receipt,
            pending_name=pending_name,
        )
        return expected_receipt

    def seal_complete(self, binding: TaskWorkspaceBinding) -> SealedWriteSet:
        self._binding_paths(binding.identity)
        if binding.write_root_identity.identity_digest != binding.identity.write_root_digest:
            raise TaskWorkspaceViolation("attempt write-root identity was replaced")
        task_fd, write_fd = self._open_write_root(binding.identity)
        try:
            manifest = _scan_directory_fd_complete(write_fd, "")
        finally:
            os.close(write_fd)
            os.close(task_fd)
        for path in manifest:
            if not _is_covered(path, binding.identity.output_paths):
                raise TaskWorkspaceViolation(f"staged path is not covered by a write claim: {path}")
        baseline = {
            file.path: (file.before_sha256, file.before_mode) for file in binding.identity.baseline_files
        }
        files = tuple(
            SealedFile(
                path=path,
                before_sha256=(baseline[path][0] if path in baseline else None),
                before_mode=(baseline[path][1] if path in baseline else None),
                after_sha256=digest,
                after_mode=mode,
                content=content,
            )
            for path, (content, digest, mode) in sorted(manifest.items())
        )
        return SealedWriteSet(
            files=files, sealed_digest=_sealed_digest(binding.identity.identity_digest, files)
        )

    def _staged_from_sealed(self, identity: TaskWorkspaceIdentity, sealed: SealedWriteSet) -> StagedWriteSet:
        files = tuple(
            StagedFile(
                path=item.path,
                before_sha256=item.before_sha256,
                before_mode=item.before_mode,
                after_sha256=item.after_sha256,
                after_mode=item.after_mode,
            )
            for item in sealed.files
        )
        payload = {
            "identity_digest": identity.identity_digest,
            "files": [file.model_dump(mode="json") for file in files],
        }
        return StagedWriteSet(staged_digest=canonical_digest(payload), **payload)

    def _prepared_directory_name(self, identity: TaskWorkspaceIdentity, sealed: SealedWriteSet) -> str:
        return f".{identity.identity_digest}.{sealed.sealed_digest}.prepared"

    def _read_prepared_files(
        self, identity: TaskWorkspaceIdentity, sealed: SealedWriteSet
    ) -> dict[str, tuple[bytes, str, int]]:
        name = self._prepared_directory_name(identity, sealed)
        prepared_fd = _open_directory_at(self._receipts_fd, name, "prepared write set")
        try:
            return _scan_directory_fd_complete(prepared_fd, "")
        finally:
            os.close(prepared_fd)

    def _persist_prepared(
        self, identity: TaskWorkspaceIdentity, sealed: SealedWriteSet
    ) -> dict[str, tuple[bytes, str, int]]:
        name = self._prepared_directory_name(identity, sealed)
        prepared_fd = _open_or_create_directory_at(self._receipts_fd, name, "prepared write set")
        try:
            for item in sealed.files:
                parent_fd, basename = _open_parent_at(
                    prepared_fd, item.path, create=True, label="prepared parent"
                )
                try:
                    try:
                        existing, digest, mode = _read_regular_at(parent_fd, basename, item.path)
                    except FileNotFoundError:
                        _write_named_file_at(parent_fd, basename, item.content, item.after_mode)
                    else:
                        if existing != item.content or digest != item.after_sha256 or mode != item.after_mode:
                            raise TaskWorkspaceViolation(f"prepared write set conflicts: {item.path}")
                finally:
                    os.close(parent_fd)
            persisted = _scan_directory_fd_complete(prepared_fd, "")
        finally:
            os.close(prepared_fd)
        expected = {item.path: (item.content, item.after_sha256, item.after_mode) for item in sealed.files}
        if persisted != expected:
            raise TaskWorkspaceViolation("prepared write set failed authentication")
        return persisted

    def _prepared_digest(
        self,
        identity: TaskWorkspaceIdentity,
        sealed: SealedWriteSet,
        persisted: Mapping[str, tuple[bytes, str, int]],
    ) -> str:
        return canonical_digest(
            {
                "identity_digest": identity.identity_digest,
                "sealed_digest": sealed.sealed_digest,
                "files": [
                    {"path": path, "after_sha256": digest, "after_mode": mode}
                    for path, (_content, digest, mode) in sorted(persisted.items())
                ],
            }
        )

    def prepare_sealed(self, binding: TaskWorkspaceBinding, sealed: SealedWriteSet) -> PreparedWorkspaceRef:
        current = self.seal_complete(binding)
        if current != sealed:
            raise TaskWorkspaceViolation("staged write root drifted after sealing")
        persisted = self._persist_prepared(binding.identity, sealed)
        return PreparedWorkspaceRef(
            identity=binding.identity,
            sealed=sealed,
            prepared_digest=self._prepared_digest(binding.identity, sealed, persisted),
        )

    def promote_prepared(self, prepared: PreparedWorkspaceRef) -> PromotionReceipt:
        persisted = self._read_prepared_files(prepared.identity, prepared.sealed)
        expected = {
            item.path: (item.content, item.after_sha256, item.after_mode) for item in prepared.sealed.files
        }
        if persisted != expected:
            raise TaskWorkspaceViolation("prepared write set failed authentication")
        if self._prepared_digest(prepared.identity, prepared.sealed, persisted) != prepared.prepared_digest:
            raise TaskWorkspaceViolation("prepared write-set digest is not canonical")
        staged = self._staged_from_sealed(prepared.identity, prepared.sealed)
        contents = {path: content for path, (content, _digest, _mode) in persisted.items()}
        return self.promote(prepared.identity, staged, contents=contents)


class TaskWorkspaceProvider:
    """Descriptor-pinned WorkspaceProvider over a TaskWorkspaceStore."""

    def __init__(self, store: TaskWorkspaceStore) -> None:
        self.store = store

    async def open_or_create(self, attempt_key: AttemptKey, claims: ResourceClaims) -> TaskWorkspaceBinding:
        return self.store.begin(
            task_id=attempt_key.digest,
            attempt=1,
            output_paths=claims.writes,
        )

    async def seal(self, binding: TaskWorkspaceBinding) -> SealedWriteSet:
        return self.store.seal_complete(binding)

    async def prepare(self, binding: TaskWorkspaceBinding, sealed: SealedWriteSet) -> PreparedWorkspaceRef:
        return self.store.prepare_sealed(binding, sealed)

    async def promote(self, prepared: PreparedWorkspaceRef) -> PromotionReceipt:
        return self.store.promote_prepared(prepared)

    async def recover_promotion(self, prepared: PreparedWorkspaceRef) -> PromotionReceipt:
        return self.store.promote_prepared(prepared)
