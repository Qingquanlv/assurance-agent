from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import stat
import sys
import uuid
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from graph_engine.canonical import canonical_digest, canonical_json_bytes
from graph_engine.composition import InvocationLock
from graph_engine.errors import GraphEngineError


_LOCK_NAME = "invocation.lock.json"
_START_INTENT_NAME = "invocation.start.json"
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
_WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


class InvocationDrift(GraphEngineError):
    """Raised when persisted invocation identity differs from the selected composition."""


class InvocationStartIntent(BaseModel):
    """Canonical immutable identity of the one selected invocation entrypoint."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = "1"
    lock_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    entrypoint: str = Field(min_length=1)

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


def install_invocation_lock_at(invocation_fd: int, lock: InvocationLock) -> None:
    """Install one immutable canonical lock below an already authenticated directory."""

    if not isinstance(lock, InvocationLock):
        raise TypeError("invocation lock must be an InvocationLock")
    _install_immutable_record_at(
        invocation_fd,
        _LOCK_NAME,
        lock.canonical_bytes,
        kind="invocation lock",
        boundary=_invocation_lock_boundary,
    )


def install_invocation_start_intent_at(
    invocation_fd: int,
    *,
    lock_digest: str,
    entrypoint: str,
) -> InvocationStartIntent:
    """Install the immutable selected entrypoint before publishing the invocation."""

    intent = InvocationStartIntent(lock_digest=lock_digest, entrypoint=entrypoint)
    _install_immutable_record_at(
        invocation_fd,
        _START_INTENT_NAME,
        intent.canonical_bytes,
        kind="invocation start intent",
        boundary=_invocation_start_intent_boundary,
    )
    return intent


def _install_immutable_record_at(
    invocation_fd: int,
    name: str,
    content: bytes,
    *,
    kind: str,
    boundary: Callable[[str], None],
) -> None:
    _require_directory_descriptor(invocation_fd)
    directory_lock_fd = os.open(".", _DIRECTORY_FLAGS, dir_fd=invocation_fd)
    primary: BaseException | None = None
    try:
        _require_same_directory(invocation_fd, directory_lock_fd)
        fcntl.flock(directory_lock_fd, fcntl.LOCK_EX)
        if _entry_exists(invocation_fd, name):
            actual = _read_immutable_record_at(invocation_fd, name, kind=kind)
            if actual != content:
                raise InvocationDrift(f"{kind} differs from the requested invocation identity")
            os.fsync(invocation_fd)
            return

        pending = f".{name}.pending-{uuid.uuid4().hex}"
        pending_fd: int | None = None
        pending_present = False
        try:
            pending_fd = os.open(pending, _WRITE_FLAGS, 0o600, dir_fd=invocation_fd)
            pending_present = True
            write_primary: BaseException | None = None
            try:
                _write_all(pending_fd, content)
                os.fchmod(pending_fd, 0o400)
                boundary("before_file_fsync")
                os.fsync(pending_fd)
                boundary("after_file_fsync")
            except BaseException as error:
                write_primary = error
                raise
            finally:
                if pending_fd is not None:
                    descriptor_to_close = pending_fd
                    pending_fd = None
                    _close_preserving_primary(
                        descriptor_to_close,
                        write_primary,
                        f"pending {kind} descriptor",
                    )

            boundary("before_rename")
            try:
                _rename_no_replace_at(invocation_fd, pending, name)
            except FileExistsError:
                actual = _read_immutable_record_at(invocation_fd, name, kind=kind)
                if actual != content:
                    raise InvocationDrift(f"{kind} differs from the requested invocation identity")
            else:
                pending_present = False
            boundary("after_rename")
            boundary("before_directory_fsync")
            os.fsync(invocation_fd)
            boundary("after_directory_fsync")
            actual = _read_immutable_record_at(invocation_fd, name, kind=kind)
            if actual != content:
                raise InvocationDrift(f"{kind} differs from the requested invocation identity")
        except BaseException as error:
            primary = error
            raise
        finally:
            if pending_fd is not None:
                _close_preserving_primary(pending_fd, primary, f"pending {kind} descriptor")
            if pending_present:
                _unlink_preserving_primary(invocation_fd, pending, primary)
    except BaseException as error:
        primary = error
        raise
    finally:
        _close_preserving_primary(directory_lock_fd, primary, "invocation directory descriptor")


def read_invocation_lock_at(invocation_fd: int) -> bytes:
    """Read exact immutable lock bytes through one stable descriptor-relative binding."""

    return _read_immutable_record_at(invocation_fd, _LOCK_NAME, kind="invocation lock")


def _read_immutable_record_at(invocation_fd: int, name: str, *, kind: str) -> bytes:
    _require_directory_descriptor(invocation_fd)
    try:
        enumerated = os.stat(name, dir_fd=invocation_fd, follow_symlinks=False)
    except FileNotFoundError as error:
        raise InvocationDrift(f"{kind} is missing") from error
    except OSError as error:
        raise InvocationDrift(f"cannot inspect the {kind}") from error
    _require_immutable_regular(enumerated, kind=kind)

    descriptor: int | None = None
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=invocation_fd)
    except OSError as error:
        raise InvocationDrift(f"cannot safely open the {kind}") from error

    primary: BaseException | None = None
    try:
        opened = os.fstat(descriptor)
        _require_immutable_regular(opened, kind=kind)
        _require_same_file(opened, enumerated, kind=kind)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        final_opened = os.fstat(descriptor)
        final_enumerated = os.stat(name, dir_fd=invocation_fd, follow_symlinks=False)
        _require_immutable_regular(final_opened, kind=kind)
        _require_immutable_regular(final_enumerated, kind=kind)
        if _file_state(opened) != _file_state(final_opened):
            raise InvocationDrift(f"{kind} changed while it was read")
        _require_same_file(final_opened, final_enumerated, kind=kind)
        return b"".join(chunks)
    except BaseException as error:
        if isinstance(error, InvocationDrift):
            primary = error
        elif isinstance(error, OSError):
            primary = InvocationDrift(f"cannot read the {kind}")
            primary.__cause__ = error
        else:
            primary = error
        raise primary
    finally:
        assert descriptor is not None
        try:
            os.close(descriptor)
        except BaseException as close_error:
            if primary is not None:
                primary.add_note(f"{kind} descriptor close failed: {close_error}")
            else:
                raise InvocationDrift(f"cannot close the {kind} descriptor") from close_error


def authenticate_invocation_lock(invocation_fd: int, expected: InvocationLock) -> None:
    """Require byte-exact equality with the selected canonical invocation lock."""

    if not isinstance(expected, InvocationLock):
        raise TypeError("expected invocation lock must be an InvocationLock")
    actual = read_invocation_lock_at(invocation_fd)
    if actual != expected.canonical_bytes:
        raise InvocationDrift("invocation lock differs from the resolved composition")


def authenticate_invocation_start_intent(
    invocation_fd: int,
    *,
    lock_digest: str,
    entrypoint: str | None = None,
) -> InvocationStartIntent:
    """Authenticate the canonical start intent before a claim or append."""

    actual = _read_immutable_record_at(
        invocation_fd,
        _START_INTENT_NAME,
        kind="invocation start intent",
    )
    try:
        intent = InvocationStartIntent.model_validate_json(actual, strict=True)
    except ValidationError as error:
        raise InvocationDrift("invocation start intent is corrupt") from error
    if intent.canonical_bytes != actual:
        raise InvocationDrift("invocation start intent is not canonical")
    if intent.lock_digest != lock_digest:
        raise InvocationDrift("invocation start intent lock digest differs from its lock")
    if entrypoint is not None and intent.entrypoint != entrypoint:
        raise InvocationDrift("invocation start intent entrypoint differs from the requested entrypoint")
    return intent


def _require_directory_descriptor(descriptor: int) -> None:
    try:
        opened = os.fstat(descriptor)
    except OSError as error:
        raise InvocationDrift("invocation directory descriptor is invalid") from error
    if not stat.S_ISDIR(opened.st_mode):
        raise InvocationDrift("invocation lock parent is not a directory")


def _require_same_directory(expected_fd: int, actual_fd: int) -> None:
    expected = os.fstat(expected_fd)
    actual = os.fstat(actual_fd)
    if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
        raise InvocationDrift("invocation directory identity changed")


def _require_immutable_regular(opened: os.stat_result, *, kind: str) -> None:
    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or opened.st_mode & _WRITE_BITS:
        raise InvocationDrift(f"{kind} is not a stable immutable regular file with one link")


def _require_same_file(first: os.stat_result, second: os.stat_result, *, kind: str) -> None:
    if (first.st_dev, first.st_ino) != (second.st_dev, second.st_ino):
        raise InvocationDrift(f"{kind} lost its stable file identity")


def _file_state(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _entry_exists(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written == 0:
            raise OSError("immutable invocation record write returned zero bytes")
        view = view[written:]


def _rename_no_replace_at(directory_fd: int, source: str, destination: str) -> None:
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        result = libc.renameatx_np(
            directory_fd,
            ctypes.c_char_p(source_bytes),
            directory_fd,
            ctypes.c_char_p(destination_bytes),
            ctypes.c_uint(0x00000004),  # RENAME_EXCL
        )
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        result = libc.renameat2(
            directory_fd,
            ctypes.c_char_p(source_bytes),
            directory_fd,
            ctypes.c_char_p(destination_bytes),
            ctypes.c_uint(1),  # RENAME_NOREPLACE
        )
    else:  # pragma: no cover - supported CI/production POSIX platforms use native rename.
        os.link(
            source,
            destination,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(source, dir_fd=directory_fd)
        return
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number), destination)
    raise OSError(error_number, os.strerror(error_number), destination)


def _close_preserving_primary(
    descriptor: int,
    primary: BaseException | None,
    kind: str,
) -> None:
    try:
        os.close(descriptor)
    except BaseException as close_error:
        if primary is None:
            raise
        primary.add_note(f"{kind} close failed: {close_error}")


def _unlink_preserving_primary(
    parent_fd: int,
    name: str,
    primary: BaseException | None,
) -> None:
    try:
        os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    except BaseException as cleanup_error:
        if primary is None:
            raise
        primary.add_note(f"pending invocation lock cleanup failed: {cleanup_error}")


def _invocation_lock_boundary(phase: str) -> None:
    del phase


def _invocation_start_intent_boundary(phase: str) -> None:
    del phase


__all__ = [
    "InvocationDrift",
    "InvocationStartIntent",
    "authenticate_invocation_lock",
    "authenticate_invocation_start_intent",
    "install_invocation_lock_at",
    "install_invocation_start_intent_at",
    "read_invocation_lock_at",
]
