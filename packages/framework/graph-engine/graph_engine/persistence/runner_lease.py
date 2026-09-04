from __future__ import annotations

from dataclasses import dataclass
import errno
import fcntl
import json
import os
import stat
import threading
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from graph_engine.canonical import JSONValue, canonical_json_bytes
from graph_engine.errors import GraphEngineError


_LEASES_DIR = "runner-leases"
_LOCK_NAME = "runner.lock"
_META_NAME = "runner.json"
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
_OTHER_WRITE = stat.S_IWOTH
_PROCESS_GUARD = threading.Lock()
_PROCESS_HELD: set[tuple[int, int]] = set()


class RunnerLeaseError(GraphEngineError):
    """Raised when local runner-lease storage is unsafe or unusable."""


class RunnerConflict(RunnerLeaseError):
    """Raised when another runner already owns the Invocation lease."""


class StaleFencingToken(RunnerLeaseError):
    """Raised when a fencing token is older than the current ownership epoch."""


@dataclass(frozen=True, slots=True)
class RunnerLease:
    invocation_id: str
    owner_id: str
    fencing_token: int

    def __post_init__(self) -> None:
        _require_token(self.fencing_token)
        _require_path_segment(self.invocation_id, "invocation id")
        _require_owner_id(self.owner_id)


class InvocationRunnerLeasePort(Protocol):
    async def acquire(self, invocation_id: str, *, owner_id: str) -> RunnerLease: ...
    async def release(self, lease: RunnerLease) -> None: ...
    async def assert_current(self, invocation_id: str, fencing_token: int) -> None: ...
    def current(self, invocation_id: str) -> RunnerLease: ...


class LocalInvocationRunnerLease:
    def __init__(self, control_dir: Path) -> None:
        if not isinstance(control_dir, Path):
            raise TypeError("control directory must be a Path")
        self._control_dir = control_dir
        self._held: dict[str, int] = {}

    async def acquire(self, invocation_id: str, *, owner_id: str) -> RunnerLease:
        invocation_id = _require_path_segment(invocation_id, "invocation id")
        owner_id = _require_owner_id(owner_id)
        with _PROCESS_GUARD:
            return self._acquire_locked(invocation_id, owner_id)

    async def release(self, lease: RunnerLease) -> None:
        if not isinstance(lease, RunnerLease):
            raise TypeError("lease must be a RunnerLease")
        await self.assert_current(lease.invocation_id, lease.fencing_token)
        with _PROCESS_GUARD:
            self._close_held(lease.invocation_id)

    async def assert_current(self, invocation_id: str, fencing_token: int) -> None:
        invocation_id = _require_path_segment(invocation_id, "invocation id")
        token = _require_token(fencing_token)
        record = self._read_record(invocation_id)
        if record is None or record.fencing_token != token:
            raise StaleFencingToken("fencing token is stale")

    def current(self, invocation_id: str) -> RunnerLease:
        invocation_id = _require_path_segment(invocation_id, "invocation id")
        record = self._read_record(invocation_id)
        if record is None:
            raise ValueError("fencing token")
        return record

    def simulate_process_exit_for_test(self, lease: RunnerLease) -> None:
        if not isinstance(lease, RunnerLease):
            raise TypeError("lease must be a RunnerLease")
        with _PROCESS_GUARD:
            self._close_held(lease.invocation_id)

    def _acquire_locked(self, invocation_id: str, owner_id: str) -> RunnerLease:
        if invocation_id in self._held:
            raise RunnerConflict("invocation runner lease is held")
        control_fd = _open_owned_directory(self._control_dir)
        primary: BaseException | None = None
        lock_fd: int | None = None
        try:
            leases_fd = _ensure_child_directory(control_fd, _LEASES_DIR)
            try:
                invocation_fd = _ensure_child_directory(leases_fd, invocation_id)
                try:
                    lock_fd = _open_or_create_regular(invocation_fd, _LOCK_NAME, _LOCK_FLAGS)
                    lock_key = _file_identity(os.fstat(lock_fd))
                    if lock_key in _PROCESS_HELD:
                        raise RunnerConflict("invocation runner lease is held")
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as error:
                        raise RunnerConflict("invocation runner lease is held") from error
                    current = _read_metadata(invocation_fd)
                    next_token = 1 if current is None else current.fencing_token + 1
                    record = RunnerLease(
                        invocation_id=invocation_id,
                        owner_id=owner_id,
                        fencing_token=next_token,
                    )
                    _write_metadata(invocation_fd, record)
                    _PROCESS_HELD.add(lock_key)
                    self._held[invocation_id] = lock_fd
                    lock_fd = None
                    return record
                finally:
                    os.close(invocation_fd)
            finally:
                os.close(leases_fd)
        except BaseException as error:
            primary = error
            raise
        finally:
            if lock_fd is not None:
                _close_preserving_primary(lock_fd, primary, "runner lock")
            _close_preserving_primary(control_fd, primary, "control directory")

    def _close_held(self, invocation_id: str) -> None:
        lock_fd = self._held.pop(invocation_id, None)
        if lock_fd is None:
            return
        try:
            _PROCESS_HELD.discard(_file_identity(os.fstat(lock_fd)))
        finally:
            os.close(lock_fd)

    def _read_record(self, invocation_id: str) -> RunnerLease | None:
        control_fd = _open_owned_directory(self._control_dir)
        primary: BaseException | None = None
        try:
            leases_fd = _open_child_directory(control_fd, _LEASES_DIR)
            try:
                invocation_fd = _open_child_directory(leases_fd, invocation_id)
                try:
                    return _read_metadata(invocation_fd)
                finally:
                    os.close(invocation_fd)
            finally:
                os.close(leases_fd)
        except FileNotFoundError:
            return None
        except BaseException as error:
            primary = error
            raise
        finally:
            _close_preserving_primary(control_fd, primary, "control directory")


def _require_path_segment(value: str, kind: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."} or "/" in value or "\0" in value:
        raise ValueError(f"{kind} must be a single path segment")
    if value.startswith("."):
        raise ValueError(f"{kind} must be a single path segment")
    return value


def _require_owner_id(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("owner id must be nonempty")
    return value


def _require_token(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("fencing token must be a positive integer")
    return value


def _open_owned_directory(path: Path) -> int:
    try:
        enumerated = path.lstat()
    except OSError as error:
        raise RunnerLeaseError("control directory is missing") from error
    _require_owned_directory(enumerated, "control directory")
    try:
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except OSError as error:
        raise RunnerLeaseError("cannot open control directory") from error
    try:
        opened = os.fstat(descriptor)
        _require_owned_directory(opened, "control directory")
        if _file_identity(opened) != _file_identity(enumerated):
            raise RunnerLeaseError("control directory identity changed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _ensure_child_directory(parent_fd: int, name: str) -> int:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    return _open_child_directory(parent_fd, name)


def _open_child_directory(parent_fd: int, name: str) -> int:
    try:
        enumerated = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise RunnerLeaseError(f"cannot inspect {name}") from error
    _require_owned_directory(enumerated, name)
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        raise RunnerLeaseError(f"cannot open {name}") from error
    try:
        opened = os.fstat(descriptor)
        _require_owned_directory(opened, name)
        if _file_identity(opened) != _file_identity(enumerated):
            raise RunnerLeaseError(f"{name} identity changed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_or_create_regular(parent_fd: int, name: str, flags: int) -> int:
    try:
        enumerated = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        enumerated = None
    except OSError as error:
        raise RunnerLeaseError(f"cannot inspect {name}") from error
    if enumerated is not None:
        _require_regular_file(enumerated, name)
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
    except OSError as error:
        raise RunnerLeaseError(f"cannot open {name}") from error
    try:
        opened = os.fstat(descriptor)
        _require_regular_file(opened, name)
        if enumerated is not None and _file_identity(opened) != _file_identity(enumerated):
            raise RunnerLeaseError(f"{name} identity changed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_metadata(invocation_fd: int) -> RunnerLease | None:
    try:
        enumerated = os.stat(_META_NAME, dir_fd=invocation_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RunnerLeaseError("cannot inspect runner lease metadata") from error
    _require_regular_file(enumerated, _META_NAME)
    descriptor = _open_or_create_regular(invocation_fd, _META_NAME, _READ_FLAGS)
    try:
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        final = os.fstat(descriptor)
        if _file_identity(final) != _file_identity(enumerated) or final.st_size != enumerated.st_size:
            raise RunnerLeaseError("runner lease metadata changed while it was read")
        payload = b"".join(chunks)
        document = json.loads(payload.decode("utf-8"))
        if not isinstance(document, dict):
            raise RunnerLeaseError("runner lease metadata is not an object")
        record = RunnerLease(
            invocation_id=str(document["invocation_id"]),
            owner_id=str(document["owner_id"]),
            fencing_token=int(document["fencing_token"]),
        )
        if canonical_json_bytes(_record_projection(record)) != payload:
            raise RunnerLeaseError("runner lease metadata is not canonical")
        return record
    finally:
        os.close(descriptor)


def _write_metadata(invocation_fd: int, record: RunnerLease) -> None:
    payload = canonical_json_bytes(_record_projection(record))
    pending = f".{_META_NAME}.pending-{uuid4().hex}"
    descriptor: int | None = None
    pending_present = True
    try:
        descriptor = os.open(pending, _WRITE_FLAGS, 0o600, dir_fd=invocation_fd)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise OSError(errno.EIO, "runner lease metadata write returned zero bytes")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(pending, _META_NAME, src_dir_fd=invocation_fd, dst_dir_fd=invocation_fd)
        pending_present = False
        os.fsync(invocation_fd)
        stored = _read_metadata(invocation_fd)
        if stored != record:
            raise RunnerLeaseError("runner lease metadata drifted after write")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if pending_present:
            try:
                os.unlink(pending, dir_fd=invocation_fd)
            except FileNotFoundError:
                pass


def _record_projection(record: RunnerLease) -> dict[str, JSONValue]:
    return {
        "fencing_token": record.fencing_token,
        "invocation_id": record.invocation_id,
        "owner_id": record.owner_id,
    }


def _require_owned_directory(opened: os.stat_result, kind: str) -> None:
    if not stat.S_ISDIR(opened.st_mode) or stat.S_ISLNK(opened.st_mode):
        raise RunnerLeaseError(f"{kind} is not a directory")
    if opened.st_uid != os.getuid():
        raise RunnerLeaseError(f"{kind} is not owned by the current user")
    if opened.st_mode & _OTHER_WRITE:
        raise RunnerLeaseError(f"{kind} is world-writable")


def _require_regular_file(opened: os.stat_result, name: str) -> None:
    if not stat.S_ISREG(opened.st_mode) or stat.S_ISLNK(opened.st_mode):
        raise RunnerLeaseError(f"{name} is not a regular file")
    if opened.st_nlink != 1:
        raise RunnerLeaseError(f"{name} is not a single-link regular file")
    if opened.st_uid != os.getuid():
        raise RunnerLeaseError(f"{name} is not owned by the current user")


def _file_identity(opened: os.stat_result) -> tuple[int, int]:
    return (opened.st_dev, opened.st_ino)


def _close_preserving_primary(descriptor: int, primary: BaseException | None, kind: str) -> None:
    try:
        os.close(descriptor)
    except BaseException as close_error:
        if primary is None:
            raise
        primary.add_note(f"{kind} close failed: {close_error}")


__all__ = [
    "InvocationRunnerLeasePort",
    "LocalInvocationRunnerLease",
    "RunnerConflict",
    "RunnerLease",
    "RunnerLeaseError",
    "StaleFencingToken",
]
