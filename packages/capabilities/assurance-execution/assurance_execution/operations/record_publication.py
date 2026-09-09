"""Crash-atomic, exclusive publication of fixed host record bytes."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import stat
import sys
import tempfile


def _publish_exclusive(source: Path, destination: Path) -> None:
    """Atomic no-replace rename; final records never have a partial or two-link state."""
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = rename(os.fsencode(source), os.fsencode(destination), 4)  # RENAME_EXCL
    elif sys.platform == "linux":
        rename = library.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # RENAME_NOREPLACE
    else:
        raise OSError("atomic exclusive journal publication requires Linux or macOS")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def _authenticate_existing(destination: Path, data: bytes) -> None:
    fd = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        details = os.fstat(fd)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise ValueError("host record must be a regular single-link file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            if stream.read(len(data) + 1) != data:
                raise ValueError("existing host record bytes differ")
    finally:
        os.close(fd)


def publish_record(destination: Path, data: bytes) -> None:
    """Publish complete bytes once; an existing identical winner is idempotent."""
    if destination.exists() or destination.is_symlink():
        _authenticate_existing(destination, data)
    else:
        fd, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}-", suffix=".tmp", dir=destination.parent
        )
        try:
            os.fchmod(fd, 0o400)
            remaining = memoryview(data)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("host record write made no progress")
                remaining = remaining[written:]
            os.fsync(fd)
            try:
                _publish_exclusive(Path(temporary), destination)
            except FileExistsError:
                _authenticate_existing(destination, data)
        finally:
            os.close(fd)
            Path(temporary).unlink(missing_ok=True)
    directory = os.open(destination.parent, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
