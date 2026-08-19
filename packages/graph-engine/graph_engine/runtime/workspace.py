from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

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
NamedValidator = tuple[str, CommitValidator]


class WorkspaceViolation(GraphEngineError):
    """Raised when workspace state could escape or violate snapshot invariants."""


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
    try:
        _validate_utf8(value, "attempt id")
    except UnicodeError as error:
        raise WorkspaceViolation("invalid attempt id") from error
    if (
        value in {"", ".", ".."}
        or "/" in value
        or "\\" in value
        or Path(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or bool(PureWindowsPath(value).drive)
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


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _tree_id(files: Mapping[str, str]) -> str:
    pairs: JSONValue = [[path, files[path]] for path in sorted(files)]
    return canonical_digest(pairs)


def _lstat_directory(path: Path, kind: str) -> os.stat_result:
    try:
        result = path.lstat()
    except OSError as error:
        raise WorkspaceViolation(f"{kind} does not exist: {path}") from error
    if stat.S_ISLNK(result.st_mode) or not stat.S_ISDIR(result.st_mode):
        raise WorkspaceViolation(f"{kind} must be a real directory: {path}")
    return result


def _read_regular_file(path: Path, relative_path: str) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise WorkspaceViolation(f"cannot safely read regular file {relative_path!r}") from error
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise WorkspaceViolation(f"path is not a regular file: {relative_path}")
        if file_stat.st_nlink != 1:
            raise WorkspaceViolation(f"hard link is not allowed: {relative_path}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, _COPY_BUFFER_SIZE):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _scan_tree(root: Path, kind: str) -> dict[str, str]:
    root_stat = _lstat_directory(root, kind)
    files: dict[str, str] = {}

    def visit(directory: Path, prefix: str) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: os.fsencode(item.name))
        except OSError as error:
            raise WorkspaceViolation(f"cannot scan {kind}: {directory}") from error
        for entry in entries:
            name = entry.name
            _validate_utf8(name, "filename")
            relative_path = f"{prefix}/{name}" if prefix else name
            _validate_relative_path(relative_path)
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise WorkspaceViolation(f"cannot inspect path: {relative_path}") from error
            if stat.S_ISLNK(entry_stat.st_mode):
                raise WorkspaceViolation(f"symlink is not allowed: {relative_path}")
            if stat.S_ISDIR(entry_stat.st_mode):
                visit(Path(entry.path), relative_path)
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                raise WorkspaceViolation(f"path is not a regular file: {relative_path}")
            if entry_stat.st_nlink != 1:
                raise WorkspaceViolation(f"hard link is not allowed: {relative_path}")
            content = _read_regular_file(Path(entry.path), relative_path)
            files[relative_path] = _sha256_bytes(content)

    visit(root, "")
    try:
        final_stat = root.lstat()
    except OSError as error:
        raise WorkspaceViolation(f"{kind} changed while being scanned") from error
    if (root_stat.st_dev, root_stat.st_ino) != (final_stat.st_dev, final_stat.st_ino):
        raise WorkspaceViolation(f"{kind} changed while being scanned")
    return files


def _write_files(root: Path, files: Mapping[str, bytes]) -> None:
    paths = sorted(files)
    for path in paths:
        _validate_relative_path(path)
        if not isinstance(files[path], bytes):
            raise WorkspaceViolation(f"snapshot content must be bytes: {path}")
    for index, path in enumerate(paths):
        for other in paths[index + 1 :]:
            if other.startswith(f"{path}/"):
                raise WorkspaceViolation(f"path collides with directory prefix: {path}")
    for relative_path in paths:
        destination = root.joinpath(*relative_path.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with destination.open("xb") as stream:
                stream.write(files[relative_path])
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as error:
            raise WorkspaceViolation(f"cannot create snapshot path: {relative_path}") from error


def _copy_manifest(source: Path, destination: Path, manifest: Mapping[str, str]) -> None:
    for relative_path in sorted(manifest):
        source_path = source.joinpath(*relative_path.split("/"))
        content = _read_regular_file(source_path, relative_path)
        if _sha256_bytes(content) != manifest[relative_path]:
            raise WorkspaceViolation(f"file changed while being copied: {relative_path}")
        destination_path = destination.joinpath(*relative_path.split("/"))
        destination_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with destination_path.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as error:
            raise WorkspaceViolation(f"cannot copy snapshot path: {relative_path}") from error


def _make_tree_read_only(root: Path) -> None:
    directories = [root]
    for current_root, child_directories, filenames in os.walk(root):
        current = Path(current_root)
        directories.extend(current / name for name in child_directories)
        for filename in filenames:
            (current / filename).chmod(0o444)
    for directory in reversed(directories):
        directory.chmod(0o555)


def _make_attempt_writable(root: Path) -> None:
    for current_root, child_directories, filenames in os.walk(root):
        current = Path(current_root)
        current.chmod(0o700)
        for directory in child_directories:
            (current / directory).chmod(0o700)
        for filename in filenames:
            (current / filename).chmod(0o600)


def _diff_manifests(baseline: Mapping[str, str], candidate: Mapping[str, str]) -> tuple[CandidateFile, ...]:
    return tuple(
        CandidateFile(
            path=path,
            before_sha256=baseline.get(path),
            after_sha256=candidate.get(path),
        )
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
        return ValidationReceipt(
            validator_id=validator_id,
            accepted=result.accepted,
            reason=result.reason,
        )
    except Exception as error:  # Plugin failures are data, not engine control flow.
        detail = str(error)
        suffix = f": {detail}" if detail else ""
        return ValidationReceipt(
            validator_id=validator_id,
            accepted=False,
            reason=f"validator raised {type(error).__name__}{suffix}",
        )


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
        self._store._validate_layout()
        attempt_manifest = _scan_tree(self.root, "attempt directory")
        baseline_manifest = self._store._verify_tree(self.baseline_tree_id)
        candidate_tree_id = self._store._publish_tree(self.root, attempt_manifest)
        return CandidateWriteSet(
            baseline_tree_id=self.baseline_tree_id,
            candidate_tree_id=candidate_tree_id,
            files=_diff_manifests(baseline_manifest, attempt_manifest),
        )

    def discard(self) -> None:
        self._store._validate_layout()
        path = self.root
        try:
            path_stat = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(path_stat.st_mode):
            path.unlink()
            return
        if not stat.S_ISDIR(path_stat.st_mode):
            raise WorkspaceViolation(f"attempt directory is not a directory: {self.attempt_id}")
        shutil.rmtree(path)


class SnapshotStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).absolute()

    @classmethod
    def create(cls, root: Path, initial_files: Mapping[str, bytes]) -> SnapshotStore:
        store = cls(root)
        if store.root.exists() or store.root.is_symlink():
            raise WorkspaceViolation(f"snapshot store already exists: {store.root}")
        store.root.mkdir(parents=True, mode=0o700)
        store._trees.mkdir(mode=0o700)
        store._attempts.mkdir(mode=0o700)
        store._lock_path.touch(mode=0o600, exist_ok=False)
        staging = store._new_tree_staging_directory()
        try:
            _write_files(staging, initial_files)
            manifest = _scan_tree(staging, "initial snapshot")
            initial_tree_id = store._finish_tree(staging, manifest)
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        store._write_head(initial_tree_id)
        return store

    @property
    def _trees(self) -> Path:
        return self.root / "trees"

    @property
    def _attempts(self) -> Path:
        return self.root / "attempts"

    @property
    def _head_path(self) -> Path:
        return self.root / "HEAD.json"

    @property
    def _lock_path(self) -> Path:
        return self.root / ".commit.lock"

    def _validate_layout(self) -> None:
        _lstat_directory(self.root, "snapshot store")
        _lstat_directory(self._trees, "trees directory")
        _lstat_directory(self._attempts, "attempts directory")
        try:
            lock_stat = self._lock_path.lstat()
        except OSError as error:
            raise WorkspaceViolation("snapshot store lock does not exist") from error
        if stat.S_ISLNK(lock_stat.st_mode) or not stat.S_ISREG(lock_stat.st_mode):
            raise WorkspaceViolation("snapshot store lock must be a regular file")

    def _tree_path(self, tree_id: str) -> Path:
        return self._trees / _validate_tree_id(tree_id)

    def _new_tree_staging_directory(self) -> Path:
        staging = self._trees / f".tmp-{uuid.uuid4().hex}"
        staging.mkdir(mode=0o700)
        return staging

    def _finish_tree(self, staging: Path, manifest: Mapping[str, str]) -> str:
        tree_id = _tree_id(manifest)
        target = self._tree_path(tree_id)
        if target.exists() or target.is_symlink():
            existing_manifest = self._verify_tree(tree_id)
            if dict(existing_manifest) != dict(manifest):
                raise WorkspaceViolation(f"content-addressed tree collision: {tree_id}")
            shutil.rmtree(staging)
            return tree_id
        _make_tree_read_only(staging)
        # macOS requires write permission on a directory while renaming it.
        # Its contents are already sealed; make the root read-only after publish.
        staging.chmod(0o700)
        try:
            os.rename(staging, target)
        except FileExistsError:
            existing_manifest = self._verify_tree(tree_id)
            if dict(existing_manifest) != dict(manifest):
                raise WorkspaceViolation(f"content-addressed tree collision: {tree_id}") from None
            _make_attempt_writable(staging)
            shutil.rmtree(staging)
        target.chmod(0o555)
        return tree_id

    def _publish_tree(self, source: Path, manifest: Mapping[str, str]) -> str:
        staging = self._new_tree_staging_directory()
        try:
            _copy_manifest(source, staging, manifest)
            copied_manifest = _scan_tree(staging, "candidate tree staging directory")
            if copied_manifest != dict(manifest):
                raise WorkspaceViolation("candidate changed while being sealed")
            return self._finish_tree(staging, copied_manifest)
        except BaseException:
            if staging.exists():
                staging.chmod(0o700)
                _make_attempt_writable(staging)
                shutil.rmtree(staging)
            raise

    def _verify_tree(self, tree_id: str) -> dict[str, str]:
        path = self._tree_path(tree_id)
        manifest = _scan_tree(path, "snapshot tree")
        actual_tree_id = _tree_id(manifest)
        if actual_tree_id != tree_id:
            raise WorkspaceViolation(f"snapshot tree id mismatch: expected {tree_id}, found {actual_tree_id}")
        return manifest

    def head_tree_id(self) -> str:
        self._validate_layout()
        try:
            head_stat = self._head_path.lstat()
            if stat.S_ISLNK(head_stat.st_mode) or not stat.S_ISREG(head_stat.st_mode):
                raise WorkspaceViolation("HEAD must be a regular file")
            if head_stat.st_nlink != 1:
                raise WorkspaceViolation("HEAD must not be a hard link")
            document: Any = json.loads(self._head_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise WorkspaceViolation("HEAD is unreadable or invalid") from error
        if not isinstance(document, dict) or set(document) != {"tree_id", "digest"}:
            raise WorkspaceViolation("HEAD has an invalid document shape")
        tree_id = _validate_tree_id(document.get("tree_id"), "HEAD tree id")
        digest = document.get("digest")
        if not isinstance(digest, str) or digest != canonical_digest({"tree_id": tree_id}):
            raise WorkspaceViolation("HEAD digest mismatch")
        self._verify_tree(tree_id)
        return tree_id

    def read_head(self, relative_path: str) -> bytes:
        path = _validate_relative_path(relative_path)
        tree_id = self.head_tree_id()
        manifest = self._verify_tree(tree_id)
        if path not in manifest:
            raise WorkspaceViolation(f"path does not exist in HEAD: {path}")
        content = _read_regular_file(self._tree_path(tree_id).joinpath(*path.split("/")), path)
        if _sha256_bytes(content) != manifest[path]:
            raise WorkspaceViolation(f"HEAD path changed while being read: {path}")
        return content

    def create_attempt(self, attempt_id: str) -> AttemptWorkspace:
        self._validate_layout()
        validated_attempt_id = _validate_attempt_id(attempt_id)
        baseline_tree_id = self.head_tree_id()
        manifest = self._verify_tree(baseline_tree_id)
        destination = self._attempts / validated_attempt_id
        try:
            destination.mkdir(mode=0o700)
        except FileExistsError as error:
            raise WorkspaceViolation(f"attempt already exists: {validated_attempt_id}") from error
        try:
            _copy_manifest(self._tree_path(baseline_tree_id), destination, manifest)
            copied_manifest = _scan_tree(destination, "attempt directory")
            if copied_manifest != manifest:
                raise WorkspaceViolation("attempt copy does not match its baseline")
            _make_attempt_writable(destination)
        except BaseException:
            if destination.exists() and not destination.is_symlink():
                shutil.rmtree(destination)
            raise
        return AttemptWorkspace(self, validated_attempt_id, baseline_tree_id)

    def _validate_candidate(
        self, candidate: CandidateWriteSet, current_tree_id: str, claims: ResourceClaims
    ) -> None:
        if candidate.baseline_tree_id != current_tree_id:
            raise WorkspaceViolation(
                f"candidate baseline {candidate.baseline_tree_id!r} does not match current HEAD "
                f"{current_tree_id!r}"
            )
        _validate_tree_id(candidate.candidate_tree_id, "candidate tree id")
        for changed_file in candidate.files:
            path = _validate_relative_path(changed_file.path)
            if not _path_is_covered(path, claims.writes):
                raise WorkspaceViolation(f"changed path is not covered by a write claim: {path}")
        baseline_manifest = self._verify_tree(current_tree_id)
        candidate_manifest = self._verify_tree(candidate.candidate_tree_id)
        actual_diff = _diff_manifests(baseline_manifest, candidate_manifest)
        if actual_diff != candidate.files:
            raise WorkspaceViolation("candidate diff does not match baseline and candidate trees")

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
        self._validate_layout()
        selected_validators = tuple(validators)
        validator_context = self._validate_validators(selected_validators, context, claims)
        with self._lock_path.open("rb") as lock:
            _lock_exclusive(lock.fileno())
            current_tree_id = self.head_tree_id()
            self._validate_candidate(candidate, current_tree_id, claims)
            receipts = tuple(
                _validation_receipt(validator_id, validator, candidate, validator_context)
                for validator_id, validator in selected_validators
                if validator_context is not None
            )
            if any(not receipt.accepted for receipt in receipts):
                return CommitResult(committed=False, receipts=receipts)
            self._write_head(candidate.candidate_tree_id)
            return CommitResult(committed=True, receipts=receipts)

    def _write_head(self, tree_id: str) -> None:
        self._verify_tree(tree_id)
        payload: dict[str, JSONValue] = {"tree_id": tree_id}
        document: dict[str, JSONValue] = {**payload, "digest": canonical_digest(payload)}
        temporary = self.root / f".HEAD-{uuid.uuid4().hex}.tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                content = canonical_json_bytes(document)
                view = memoryview(content)
                while view:
                    written = os.write(descriptor, view)
                    if written == 0:
                        raise WorkspaceViolation("failed to write temporary HEAD document")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, self._head_path)
            _flush_directory(self.root)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def commit_candidate(
    store: SnapshotStore,
    candidate: CandidateWriteSet,
    claims: ResourceClaims,
    validators: Sequence[NamedValidator] = (),
    context: ValidationContext | None = None,
) -> CommitResult:
    return store.commit_candidate(candidate, claims, validators, context)


def _lock_exclusive(descriptor: int) -> None:
    try:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except ImportError as error:  # pragma: no cover - graph-engine currently targets POSIX runtimes.
        raise WorkspaceViolation("snapshot commits require POSIX advisory file locking") from error


def _flush_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "AttemptWorkspace",
    "SnapshotStore",
    "WorkspaceViolation",
    "commit_candidate",
]
