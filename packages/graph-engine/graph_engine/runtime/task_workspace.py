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

    def _binding_paths(self, identity: TaskWorkspaceIdentity) -> tuple[Path, Path]:
        self._authenticate_identity(identity)
        safe_task_id = _safe_task_id(identity.task_id)
        task_root = self.attempts_root / safe_task_id
        write_root = task_root / identity.attempt_id
        if write_root.parent != task_root or task_root.parent != self.attempts_root:
            raise TaskWorkspaceViolation("attempt write root escaped its trusted namespace")
        identity_path = task_root / f".{identity.attempt_id}.identity.json"
        recorded = self._read_identity(identity_path)
        if recorded != identity:
            raise TaskWorkspaceViolation("task workspace identity drifted from its recorded binding")
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
        task_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _require_directory(task_root, "task attempts root")
        identity_path = task_root / f".{attempt_id}.identity.json"
        if identity_path.exists():
            identity = self._read_identity(identity_path)
            self._authenticate_identity(identity)
            if identity.task_id != task_id or identity.attempt != attempt or identity.output_paths != claims:
                raise TaskWorkspaceViolation("attempt root already belongs to another identity")
            _require_directory(write_root, "attempt write root")
            return TaskWorkspaceBinding(identity, self.project_root, write_root)
        if write_root.exists():
            raise TaskWorkspaceViolation("attempt write root exists without an authenticated identity")
        baseline = _manifest_for_claims(self.project_root, claims)
        baseline_files = tuple(
            StagedFile(path=path, before_sha256=digest) for path, digest in sorted(baseline.items())
        )
        write_root.mkdir(mode=0o700)
        _fsync_directory(task_root)
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
        try:
            _atomic_write(identity_path, canonical_json_bytes(identity.model_dump(mode="json")))
        except BaseException:
            try:
                write_root.rmdir()
                _fsync_directory(task_root)
            except OSError:
                pass
            raise
        return TaskWorkspaceBinding(identity, self.project_root, write_root)

    def seal(self, identity: TaskWorkspaceIdentity) -> StagedWriteSet:
        _task_root, write_root = self._binding_paths(identity)
        manifest = _scan_staged_root(write_root, identity.output_paths)
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

    def _read_identity(self, path: Path) -> TaskWorkspaceIdentity:
        try:
            return TaskWorkspaceIdentity.model_validate_json(path.read_bytes())
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise TaskWorkspaceViolation("cannot authenticate recorded task workspace identity") from error

    def _receipt_path(self, identity: TaskWorkspaceIdentity) -> Path:
        return self.receipts_root / f"{identity.identity_digest}.json"

    def _pending_path(self, identity: TaskWorkspaceIdentity) -> Path:
        return self.receipts_root / f".{identity.identity_digest}.pending.json"

    def _read_receipt(self, path: Path) -> PromotionReceipt:
        try:
            return PromotionReceipt.model_validate_json(path.read_bytes())
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise TaskWorkspaceViolation("cannot authenticate promotion receipt") from error

    def _verify_target_baseline(self, identity: TaskWorkspaceIdentity) -> None:
        current = _manifest_for_claims(self.project_root, identity.output_paths)
        expected = {file.path: file.before_sha256 for file in identity.baseline_files}
        if current != expected:
            raise TaskWorkspaceViolation("target drift since task workspace begin")

    def _staged_matches_root(self, identity: TaskWorkspaceIdentity, staged: StagedWriteSet) -> None:
        current = self.seal(identity)
        if current != staged:
            raise TaskWorkspaceViolation("staged write root drifted after sealing")

    def _targets_match_staged(self, staged: StagedWriteSet) -> bool:
        for file in staged.files:
            assert file.after_sha256 is not None
            target = self.project_root.joinpath(*file.path.split("/"))
            try:
                _contents, digest = _read_regular(target, file.path)
            except (FileNotFoundError, TaskWorkspaceViolation):
                return False
            if digest != file.after_sha256:
                return False
        return True

    def _ensure_target_parent(self, logical_path: str) -> Path:
        parent = self.project_root
        for segment in logical_path.split("/")[:-1]:
            child = parent / segment
            try:
                entry = _lstat(child, "target parent")
            except FileNotFoundError:
                child.mkdir(mode=0o700)
                _fsync_directory(parent)
            else:
                if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
                    raise TaskWorkspaceViolation(f"target parent is not a real directory: {logical_path}")
            parent = child
        _require_directory(parent, "target parent")
        return parent

    def _replace_file(self, write_root: Path, file: StagedFile) -> None:
        assert file.after_sha256 is not None
        source = write_root.joinpath(*file.path.split("/"))
        contents, digest = _read_regular(source, file.path)
        if digest != file.after_sha256:
            raise TaskWorkspaceViolation(f"staged file drifted before promotion: {file.path}")
        parent = self._ensure_target_parent(file.path)
        target = parent / file.path.split("/")[-1]
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=parent)
        temporary = Path(temporary_name)
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
            os.replace(temporary, target)
            _fsync_directory(parent)
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def promote(self, identity: TaskWorkspaceIdentity, staged: StagedWriteSet) -> PromotionReceipt:
        self._authenticate_identity(identity)
        self._authenticate_staged(identity, staged)
        receipt_path = self._receipt_path(identity)
        expected_receipt = _receipt(identity.identity_digest, staged.staged_digest)
        if receipt_path.exists():
            existing = self._read_receipt(receipt_path)
            if existing != expected_receipt:
                raise TaskWorkspaceViolation("existing promotion receipt conflicts with this replay")
            return existing
        pending_path = self._pending_path(identity)
        if pending_path.exists():
            pending = self._read_receipt(pending_path)
            if pending != expected_receipt:
                raise TaskWorkspaceViolation("existing pending promotion conflicts with this replay")
            if not self._targets_match_staged(staged):
                raise TaskWorkspaceViolation("prior multi-file promotion is incomplete; refusing retry")
            _atomic_write(receipt_path, canonical_json_bytes(expected_receipt.model_dump(mode="json")))
            pending_path.unlink(missing_ok=True)
            _fsync_directory(self.receipts_root)
            return expected_receipt
        self._verify_target_baseline(identity)
        self._staged_matches_root(identity, staged)
        _atomic_write(pending_path, canonical_json_bytes(expected_receipt.model_dump(mode="json")))
        _task_root, write_root = self._binding_paths(identity)
        for file in staged.files:
            self._replace_file(write_root, file)
        _atomic_write(receipt_path, canonical_json_bytes(expected_receipt.model_dump(mode="json")))
        pending_path.unlink(missing_ok=True)
        _fsync_directory(self.receipts_root)
        return expected_receipt
