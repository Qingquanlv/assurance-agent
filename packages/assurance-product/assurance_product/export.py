from __future__ import annotations

import hashlib
import os
import stat
import uuid
from pathlib import Path, PurePosixPath
from typing import cast

from graph_engine.canonical import JSONValue, canonical_digest, canonical_json_bytes
from graph_engine.errors import GraphEngineError
from graph_engine.runtime.engine import Engine, EngineError
from graph_engine.runtime.events import EventEnvelope, InvocationStarted
from graph_engine.runtime.invocation_lock import (
    InvocationDrift,
    authenticate_invocation_start_intent,
    read_invocation_lock_at,
)
from graph_engine.runtime.ledger import Ledger, LedgerIntegrityError
from graph_engine.runtime.models import InvocationProjection, fold_events
from graph_engine.runtime.secret_sources import (
    InvocationRuntimeAuthorization,
    RuntimeAuthorizationError,
    resolve_secret_source,
)
from graph_engine.runtime.seed import workspace_tree_id
from graph_engine.runtime.tree_io import SnapshotExportError, materialize_snapshot
from graph_engine.runtime.workspace import SnapshotStore, WorkspaceViolation

from assurance_product.models import ExportedArtifactV1, ResultExportV1, StatusV1
from assurance_product.status import render_status

_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
_LIVE_ACTIVITY_STATES = frozenset({"prepared", "dispatch_started", "bound"})
_ARTIFACT_PREFIXES = ("qa/",)
_MEDIA_TYPES = {
    ".json": "application/json",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}


class ResultExportError(GraphEngineError):
    """Raised when an invocation result tree cannot be exported exactly."""


def digest_directory(root: Path) -> str:
    files = _directory_file_digests(root)
    return f"sha256:{workspace_tree_id(files)}"


def export_invocation(
    engine: Engine,
    invocation_id: str,
    destination: Path,
    *,
    authorization: InvocationRuntimeAuthorization,
) -> ResultExportV1:
    if not isinstance(authorization, InvocationRuntimeAuthorization):
        raise TypeError("export requires InvocationRuntimeAuthorization")
    _validate_invocation_id(invocation_id)
    engine_root = _engine_root(engine)
    invocation_root = engine_root / "invocations" / invocation_id
    absolute_destination = destination.absolute()
    _reject_destination(absolute_destination, engine_root=engine_root, invocation_root=invocation_root)

    invocation_fd = _open_invocation(engine_root, invocation_id)
    staging_name: str | None = None
    parent_fd: int | None = None
    installed = False
    try:
        lock_digest, envelopes, projection, status = _authenticate_invocation(
            invocation_fd,
            invocation_id,
            authorization,
        )
        _reject_incomplete_export(status, projection)
        with SnapshotStore(invocation_root / "workspace") as store:
            head_tree_id = store.head_tree_id()
            expected_head = projection.head_tree_id or status.initial_tree_id
            if head_tree_id != expected_head or head_tree_id != status.current_head_tree_id:
                raise ResultExportError("final head disagrees with the authenticated projection")
            parent_fd = _open_parent_directory(absolute_destination)
            _clear_empty_destination(parent_fd, absolute_destination)
            staging_name = f".result-export-staging-{uuid.uuid4().hex}"
            os.mkdir(staging_name, 0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
            staging_path = absolute_destination.parent / staging_name
            materialize_snapshot(store, head_tree_id, staging_path / "result-tree")
            result_tree_digest = digest_directory(staging_path / "result-tree")
            artifact_index = _artifact_index(staging_path / "result-tree", projection)
            event_stream_digest = canonical_digest(
                [[envelope.seq, envelope.event_sha256] for envelope in envelopes]
            )
            exported = ResultExportV1(
                schema_version="1",
                invocation_id=invocation_id,
                lock_digest=lock_digest,
                event_stream_digest=event_stream_digest,
                result_tree_digest=result_tree_digest,
                status=status,
                artifact_index=artifact_index,
            )
            _write_export_documents(staging_path, exported)
            _reject_resolved_secrets(staging_path, authorization)
            _fsync_tree(staging_path)
            os.rename(staging_name, absolute_destination.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            installed = True
            os.fsync(parent_fd)
            return exported
    except ResultExportError:
        raise
    except (
        InvocationDrift,
        LedgerIntegrityError,
        SnapshotExportError,
        WorkspaceViolation,
        EngineError,
        OSError,
        ValueError,
        RuntimeAuthorizationError,
    ) as error:
        raise ResultExportError(str(error)) from error
    finally:
        if staging_name is not None and parent_fd is not None and not installed:
            _remove_entry(parent_fd, staging_name)
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(invocation_fd)


def _validate_invocation_id(invocation_id: str) -> None:
    if not invocation_id or invocation_id in {".", ".."} or "/" in invocation_id or "\\" in invocation_id:
        raise ResultExportError("invocation_id must be one path component")


def _engine_root(engine: Engine) -> Path:
    root = getattr(engine, "_root", None)
    if not isinstance(root, Path):
        raise ResultExportError("engine root is unavailable")
    return root


def _open_invocation(engine_root: Path, invocation_id: str) -> int:
    invocations = engine_root / "invocations"
    if not invocations.is_dir():
        raise ResultExportError(f"invocation is missing: {invocation_id}")
    parent_fd = os.open(invocations, _DIRECTORY_FLAGS)
    try:
        return os.open(invocation_id, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError as error:
        raise ResultExportError(f"invocation is missing: {invocation_id}") from error
    except OSError as error:
        raise ResultExportError(f"cannot open invocation: {invocation_id}") from error
    finally:
        os.close(parent_fd)


def _authenticate_invocation(
    invocation_fd: int,
    invocation_id: str,
    authorization: InvocationRuntimeAuthorization,
) -> tuple[str, tuple[EventEnvelope, ...], InvocationProjection, StatusV1]:
    lock_bytes = read_invocation_lock_at(invocation_fd)
    lock_digest = hashlib.sha256(lock_bytes).hexdigest()
    intent = authenticate_invocation_start_intent(invocation_fd, lock_digest=lock_digest)
    if authorization.digest != intent.runtime_authorization_digest:
        raise ResultExportError("authorization digest does not match the invocation")
    ledger = Ledger.at(invocation_fd, "ledger", display_root=Path(invocation_id) / "ledger")
    envelopes = ledger.read_all()
    if not envelopes:
        raise ResultExportError("invocation has no ledger bootstrap")
    started = envelopes[0].event
    if not isinstance(started, InvocationStarted):
        raise ResultExportError("invocation ledger lacks its canonical bootstrap")
    if started.lock_digest != lock_digest:
        raise ResultExportError("lock digest drifted")
    if started.runtime_authorization_digest != authorization.digest:
        raise ResultExportError("authorization digest does not match the invocation")
    if started.invocation_id != invocation_id:
        raise ResultExportError("invocation ledger identity does not match its path")
    for envelope in envelopes:
        if not envelope.has_valid_digest():
            raise ResultExportError("event stream is corrupt")
    projection = fold_events(envelopes)
    if projection.lock_digest != lock_digest:
        raise ResultExportError("lock digest drifted")
    status = render_status(
        projection,
        root_input_digest=intent.root_input_digest,
        initial_tree_id=intent.initial_tree_id,
    )
    return lock_digest, envelopes, projection, status


def _reject_incomplete_export(
    status: StatusV1,
    projection: InvocationProjection,
) -> None:
    if status.status != "completed":
        raise ResultExportError(f"cannot export a {status.status} invocation")
    if projection.status != "succeeded" or projection.pending_interrupt is not None:
        raise ResultExportError(f"cannot export a {status.status} invocation")
    for activation in projection.activations:
        if activation.status in {"active"}:
            raise ResultExportError("cannot export a running invocation")
        for attempt in activation.attempts:
            activity = attempt.activity
            if activity is None:
                continue
            if activity.state in _LIVE_ACTIVITY_STATES:
                raise ResultExportError("cannot export live indeterminate activity")
            if activity.state != "terminal_observed" and activity.dispatch_fingerprint_digest is not None:
                raise ResultExportError("missing terminal receipt")


def _reject_destination(destination: Path, *, engine_root: Path, invocation_root: Path) -> None:
    if destination.name in {"", ".", ".."}:
        raise ResultExportError("export destination must name one path component")
    resolved_parent = destination.parent.resolve()
    candidate = resolved_parent / destination.name
    if _is_within(candidate, engine_root) or _is_within(candidate, invocation_root):
        raise ResultExportError("destination escapes into the invocation")
    try:
        info = destination.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise ResultExportError("destination is a symlink")
    if stat.S_ISREG(info.st_mode):
        if info.st_nlink != 1:
            raise ResultExportError("destination is a hard link")
        raise ResultExportError("destination already exists")
    if not stat.S_ISDIR(info.st_mode):
        raise ResultExportError("destination already exists")


def _clear_empty_destination(parent_fd: int, destination: Path) -> None:
    name = destination.name
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise ResultExportError("destination is a symlink")
    if stat.S_ISREG(info.st_mode):
        if info.st_nlink != 1:
            raise ResultExportError("destination is a hard link")
        raise ResultExportError("destination already exists")
    if not stat.S_ISDIR(info.st_mode):
        raise ResultExportError("destination already exists")
    child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        entries = os.listdir(child_fd)
    finally:
        os.close(child_fd)
    if entries:
        raise ResultExportError("destination is not empty")
    os.rmdir(name, dir_fd=parent_fd)


def _open_parent_directory(destination: Path) -> int:
    parent = destination.parent
    if not parent.exists():
        raise ResultExportError("export destination parent does not exist")
    try:
        return os.open(parent, _DIRECTORY_FLAGS)
    except OSError as error:
        raise ResultExportError("cannot open export destination parent") from error


def _write_export_documents(staging: Path, exported: ResultExportV1) -> None:
    staging_fd = os.open(staging, _DIRECTORY_FLAGS)
    try:
        _write_json(
            staging_fd,
            "manifest.json",
            cast(JSONValue, exported.model_dump(mode="json")),
        )
        _write_json(
            staging_fd,
            "status.json",
            cast(JSONValue, exported.status.model_dump(mode="json")),
        )
        _write_json(
            staging_fd,
            "artifact-index.json",
            cast(JSONValue, [item.model_dump(mode="json") for item in exported.artifact_index]),
        )
        os.fsync(staging_fd)
    finally:
        os.close(staging_fd)


def _write_json(directory_fd: int, name: str, value: JSONValue) -> None:
    payload = canonical_json_bytes(value) + b"\n"
    descriptor = os.open(name, _FILE_WRITE_FLAGS, 0o600, dir_fd=directory_fd)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise ResultExportError(f"failed to write {name}")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _artifact_index(result_tree: Path, projection: InvocationProjection) -> tuple[ExportedArtifactV1, ...]:
    del projection
    artifacts: list[ExportedArtifactV1] = []
    files = _directory_file_digests(result_tree)
    for relative_path in sorted(files):
        if not relative_path.startswith(_ARTIFACT_PREFIXES):
            continue
        artifacts.append(
            ExportedArtifactV1(
                artifact_id=relative_path,
                relative_path=relative_path,
                media_type=_media_type(relative_path),
                sha256=files[relative_path],
            )
        )
    return tuple(artifacts)


def _media_type(relative_path: str) -> str:
    suffix = PurePosixPath(relative_path).suffix.lower()
    return _MEDIA_TYPES.get(suffix, "application/octet-stream")


def _reject_resolved_secrets(root: Path, authorization: InvocationRuntimeAuthorization) -> None:
    needles: list[bytes] = []
    for binding in authorization.secret_sources:
        value = resolve_secret_source(binding)
        if value:
            needles.append(value)
    if not needles:
        return
    for path, _digest in _walk_regular_files(root):
        content = path.read_bytes()
        if any(needle in content for needle in needles):
            raise ResultExportError("export contains resolved secret bytes")


def _directory_file_digests(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path, relative in _walk_regular_files(root):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files[relative] = digest
    return files


def _walk_regular_files(root: Path) -> list[tuple[Path, str]]:
    if root.is_symlink():
        raise ResultExportError("symlink is not allowed")
    if not root.is_dir():
        raise ResultExportError("export root is not a directory")
    found: list[tuple[Path, str]] = []
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        if current_path.is_symlink():
            raise ResultExportError("symlink is not allowed")
        for name in list(dirnames):
            child = current_path / name
            if child.is_symlink():
                raise ResultExportError(f"symlink is not allowed: {name}")
        for name in filenames:
            child = current_path / name
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ResultExportError(f"symlink is not allowed: {name}")
            if not stat.S_ISREG(info.st_mode):
                raise ResultExportError(f"path is not a regular file: {name}")
            if info.st_nlink != 1:
                raise ResultExportError(f"hard link is not allowed: {name}")
            relative = child.relative_to(root).as_posix()
            found.append((child, relative))
    return found


def _fsync_tree(root: Path) -> None:
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in filenames:
            descriptor = os.open(current_path / name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        directory_fd = os.open(current_path, _DIRECTORY_FLAGS)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        del dirnames


def _remove_entry(parent_fd: int, name: str) -> None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        try:
            for entry in os.listdir(child_fd):
                _remove_entry(child_fd, entry)
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=parent_fd)
        return
    os.unlink(name, dir_fd=parent_fd)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
