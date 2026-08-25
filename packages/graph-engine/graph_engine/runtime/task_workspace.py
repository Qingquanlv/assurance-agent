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
from collections.abc import Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, cast

from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.errors import GraphEngineError
from graph_engine.plugin_api import (
    PromotionReceipt,
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


def _scan_directory(path: Path, prefix: str) -> dict[str, str]:
    _require_directory(path, "directory")
    files: dict[str, str] = {}
    try:
        entries = sorted(path.iterdir(), key=lambda entry: os.fsencode(entry.name))
    except OSError as error:
        raise TaskWorkspaceViolation(f"cannot enumerate directory: {prefix}") from error
    for entry in entries:
        logical_path = f"{prefix}/{entry.name}" if prefix else entry.name
        value = _lstat(entry, "directory entry")
        if stat.S_ISLNK(value.st_mode):
            raise TaskWorkspaceViolation(f"symlink is not allowed: {logical_path}")
        if stat.S_ISDIR(value.st_mode):
            files.update(_scan_directory(entry, logical_path))
        elif stat.S_ISREG(value.st_mode):
            _contents, digest = _read_regular(entry, logical_path)
            files[logical_path] = digest
        else:
            raise TaskWorkspaceViolation(f"path is not a regular file: {logical_path}")
    return files


def _manifest_for_claims(project_root: Path, claims: Sequence[str]) -> dict[str, str]:
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
        if stat.S_ISDIR(entry.st_mode):
            scanned = _scan_directory(target, claim)
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


def _read_regular_at(parent_fd: int, name: str, logical_path: str) -> tuple[bytes, str]:
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
        if not _same_inode(opened, final) or final.st_size != opened.st_size:
            raise TaskWorkspaceViolation(f"regular file changed during read: {logical_path}")
        return b"".join(chunks), digest.hexdigest()
    finally:
        os.close(descriptor)


def _scan_directory_fd(directory_fd: int, prefix: str) -> dict[str, str]:
    files: dict[str, str] = {}
    try:
        names = sorted(os.listdir(directory_fd), key=os.fsencode)
    except OSError as error:
        raise TaskWorkspaceViolation(f"cannot enumerate directory: {prefix}") from error
    for name in names:
        logical_path = f"{prefix}/{name}" if prefix else name
        entry = _stat_at(directory_fd, name, "directory entry")
        if stat.S_ISLNK(entry.st_mode):
            raise TaskWorkspaceViolation(f"symlink is not allowed: {logical_path}")
        if stat.S_ISDIR(entry.st_mode):
            child = _open_directory_at(directory_fd, name, "directory entry")
            try:
                files.update(_scan_directory_fd(child, logical_path))
            finally:
                os.close(child)
        elif stat.S_ISREG(entry.st_mode):
            _contents, digest = _read_regular_at(directory_fd, name, logical_path)
            files[logical_path] = digest
        else:
            raise TaskWorkspaceViolation(f"path is not a regular file: {logical_path}")
    return files


def _manifest_for_claims_fd(root_fd: int, claims: Sequence[str]) -> dict[str, str]:
    manifest: dict[str, str] = {}
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
            if stat.S_ISDIR(entry.st_mode):
                child = _open_directory_at(parent, name, "claim target")
                try:
                    scanned = _scan_directory_fd(child, claim)
                finally:
                    os.close(child)
            elif stat.S_ISREG(entry.st_mode):
                _contents, digest = _read_regular_at(parent, name, claim)
                scanned = {claim: digest}
            else:
                raise TaskWorkspaceViolation(f"path is not a regular file: {claim}")
            manifest.update(scanned)
        finally:
            os.close(parent)
    return manifest


def _atomic_write_at(parent_fd: int, name: str, content: bytes) -> None:
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
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
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except OSError:
            pass
        raise


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
        self._project_fd = _open_pinned_directory(self.project_root, "project root")
        self._attempts_fd = _open_pinned_directory(self.attempts_root, "attempts root")
        self._receipts_fd = _open_pinned_directory(self.receipts_root, "receipts root")

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
            os.close(write_fd)
        finally:
            os.close(task_fd)
        return task_root, write_root

    def _authenticate_identity(self, identity: TaskWorkspaceIdentity) -> None:
        if identity.project_digest != _path_digest(self.project_root):
            raise TaskWorkspaceViolation("task workspace project identity drifted")
        if identity.attempt_id != _attempt_id(identity.attempt):
            raise TaskWorkspaceViolation("task workspace attempt id does not match its attempt")
        expected_write_root = self.attempts_root / _safe_task_id(identity.task_id) / identity.attempt_id
        if identity.write_root_digest != _path_digest(expected_write_root):
            raise TaskWorkspaceViolation("task workspace write-root identity drifted")
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
            if file.after_sha256 is None or not _is_covered(file.path, identity.output_paths):
                raise TaskWorkspaceViolation("staged write set contains an undeclared file")

    def begin(
        self,
        *,
        task_id: str,
        attempt: int,
        output_paths: Sequence[str],
    ) -> TaskWorkspaceBinding:
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
                os.close(write_fd)
                return TaskWorkspaceBinding(recorded, self.project_root, write_root)
            try:
                _stat_at(task_fd, attempt_id, "attempt write root")
            except FileNotFoundError:
                pass
            else:
                raise TaskWorkspaceViolation("attempt write root exists without an authenticated identity")
            baseline = _manifest_for_claims_fd(self._project_fd, claims)
            baseline_files = tuple(
                StagedFile(path=path, before_sha256=digest) for path, digest in sorted(baseline.items())
            )
            os.mkdir(attempt_id, mode=0o700, dir_fd=task_fd)
            os.fsync(task_fd)
            payload: dict[str, Any] = {
                "task_id": task_id,
                "attempt": attempt,
                "attempt_id": attempt_id,
                "output_paths": list(claims),
                "baseline_files": [file.model_dump(mode="json") for file in baseline_files],
                "project_digest": _path_digest(self.project_root),
                "write_root_digest": _path_digest(write_root),
                "layout_schema_version": "1",
            }
            identity = TaskWorkspaceIdentity(identity_digest=canonical_digest(payload), **payload)
            _atomic_write_at(task_fd, identity_name, canonical_json_bytes(identity.model_dump(mode="json")))
            return TaskWorkspaceBinding(identity, self.project_root, write_root)
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
        baseline = {file.path: file.before_sha256 for file in identity.baseline_files}
        files = tuple(
            StagedFile(path=path, before_sha256=baseline.get(path), after_sha256=digest)
            for path, digest in sorted(manifest.items())
        )
        payload = {
            "identity_digest": identity.identity_digest,
            "files": [file.model_dump(mode="json") for file in files],
        }
        return StagedWriteSet(staged_digest=canonical_digest(payload), **payload)

    def _read_identity_at(self, parent_fd: int, name: str) -> TaskWorkspaceIdentity:
        try:
            content, _digest = _read_regular_at(parent_fd, name, "task workspace identity")
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
            content, _digest = _read_regular_at(self._receipts_fd, name, "promotion receipt")
            return PromotionReceipt.model_validate_json(content)
        except FileNotFoundError:
            raise
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise TaskWorkspaceViolation("cannot authenticate promotion receipt") from error

    def _verify_target_baseline(self, identity: TaskWorkspaceIdentity) -> None:
        current = _manifest_for_claims_fd(self._project_fd, identity.output_paths)
        expected = {file.path: file.before_sha256 for file in identity.baseline_files}
        if current != expected:
            raise TaskWorkspaceViolation("target drift since task workspace begin")

    def _staged_matches_root(self, identity: TaskWorkspaceIdentity, staged: StagedWriteSet) -> None:
        current = self.seal(identity)
        if current != staged:
            raise TaskWorkspaceViolation("staged write root drifted after sealing")

    def _target_state(self, file: StagedFile) -> str | None:
        try:
            parent, name = _open_parent_at(self._project_fd, file.path, create=False, label="target parent")
        except FileNotFoundError:
            return None
        try:
            try:
                _contents, digest = _read_regular_at(parent, name, file.path)
            except FileNotFoundError:
                return None
            return digest
        finally:
            os.close(parent)

    def _targets_match_staged(self, staged: StagedWriteSet) -> bool:
        for file in staged.files:
            assert file.after_sha256 is not None
            try:
                digest = self._target_state(file)
            except (FileNotFoundError, TaskWorkspaceViolation):
                return False
            if digest != file.after_sha256:
                return False
        return True

    def _replace_file(self, identity: TaskWorkspaceIdentity, file: StagedFile) -> None:
        assert file.after_sha256 is not None
        task_fd, write_fd = self._open_write_root(identity)
        try:
            source_parent, source_name = _open_parent_at(
                write_fd, file.path, create=False, label="staged parent"
            )
            try:
                contents, digest = _read_regular_at(source_parent, source_name, file.path)
            finally:
                os.close(source_parent)
            if digest != file.after_sha256:
                raise TaskWorkspaceViolation(f"staged file drifted before promotion: {file.path}")
            target_parent, target_name = _open_parent_at(
                self._project_fd, file.path, create=True, label="target parent"
            )
            try:
                temporary = f".{target_name}.{uuid.uuid4().hex}.tmp"
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=target_parent,
                )
                try:
                    view = memoryview(contents)
                    while view:
                        written = os.write(descriptor, view)
                        if written == 0:
                            raise TaskWorkspaceViolation("filesystem write returned zero bytes")
                        view = view[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                try:
                    current = self._target_state_at(target_parent, target_name, file.path)
                    if current != file.before_sha256:
                        raise TaskWorkspaceViolation(f"target drift immediately before replace: {file.path}")
                    os.replace(temporary, target_name, src_dir_fd=target_parent, dst_dir_fd=target_parent)
                    os.fsync(target_parent)
                except BaseException:
                    try:
                        os.unlink(temporary, dir_fd=target_parent)
                    except OSError:
                        pass
                    raise
            finally:
                os.close(target_parent)
        finally:
            os.close(write_fd)
            os.close(task_fd)

    def _target_state_at(self, parent_fd: int, name: str, logical_path: str) -> str | None:
        try:
            _contents, digest = _read_regular_at(parent_fd, name, logical_path)
        except FileNotFoundError:
            return None
        return digest

    def promote(self, identity: TaskWorkspaceIdentity, staged: StagedWriteSet) -> PromotionReceipt:
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
            self._staged_matches_root(identity, staged)
            if not self._targets_match_staged(staged):
                raise TaskWorkspaceViolation("completed promotion replay does not match staged targets")
            return existing
        pending_name = f".{identity.identity_digest}.pending.json"
        try:
            pending = self._read_receipt_at(pending_name)
        except FileNotFoundError:
            pending = None
        if pending is not None:
            if pending != expected_receipt:
                raise TaskWorkspaceViolation("existing pending promotion conflicts with this replay")
            self._staged_matches_root(identity, staged)
            remaining: list[StagedFile] = []
            for file in staged.files:
                state = self._target_state(file)
                if state == file.after_sha256:
                    continue
                if state == file.before_sha256:
                    remaining.append(file)
                    continue
                raise TaskWorkspaceViolation("prior multi-file promotion is incomplete; refusing retry")
        else:
            self._verify_target_baseline(identity)
            self._staged_matches_root(identity, staged)
            _atomic_write_at(
                self._receipts_fd,
                pending_name,
                canonical_json_bytes(expected_receipt.model_dump(mode="json")),
            )
            remaining = list(staged.files)
        for file in remaining:
            self._replace_file(identity, file)
        if not self._targets_match_staged(staged):
            raise TaskWorkspaceViolation("promotion targets do not match staged write set")
        _atomic_write_at(
            self._receipts_fd, receipt_name, canonical_json_bytes(expected_receipt.model_dump(mode="json"))
        )
        os.unlink(pending_name, dir_fd=self._receipts_fd)
        os.fsync(self._receipts_fd)
        return expected_receipt
