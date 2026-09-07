"""Persistent host-owned sealing authority for project-external runtime receipts."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import os
import secrets
import stat
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from graph_engine.errors import GraphEngineError


_AUTHORITY_ROOT_ENV = "ASSURANCE_AGENT_HOST_AUTHORITY_ROOT"
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_FILE_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
_KEY_BYTES = 32
_KEY_NAME = "selection-authority-v1.key"
_LOCK_NAME = ".selection-authority.lock"


class HostAuthorityError(GraphEngineError):
    """Raised when the external host sealing authority is unavailable or invalid."""


@dataclass(frozen=True, slots=True)
class HostSealingAuthority:
    """HMAC authority whose key lives outside every project workspace."""

    root: Path
    authority_id: str
    authority_version: str
    _key: bytes = field(repr=False, compare=False)

    @classmethod
    def open_for_project(
        cls,
        project_root: Path,
        *,
        authority_root: Path | None = None,
    ) -> HostSealingAuthority:
        project = Path(project_root)
        if project.resolve(strict=True) != project or not project.is_dir():
            raise HostAuthorityError("host sealing authority requires a canonical project root")
        root = Path(authority_root) if authority_root is not None else _default_authority_root()
        if not root.is_absolute():
            raise HostAuthorityError("host sealing authority root must be absolute")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.resolve(strict=True) != root or root == project or project in root.parents:
            raise HostAuthorityError("host sealing authority must be outside the project workspace")
        root_fd = os.open(root, _DIRECTORY_FLAGS)
        try:
            metadata = os.fstat(root_fd)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                raise HostAuthorityError("host sealing authority root is not private")
            key = _load_or_create_key(root_fd)
        finally:
            os.close(root_fd)
        authority_id = hashlib.sha256(b"assurance-agent.selection.v1\0" + key).hexdigest()
        return cls(root=root, authority_id=authority_id, authority_version="1", _key=key)

    def sign(self, payload: bytes) -> str:
        """Seal canonical receipt bytes without exposing key material."""

        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()

    def verify(
        self,
        payload: bytes,
        *,
        authority_id: str,
        authority_version: str,
        signature: str,
    ) -> None:
        """Authenticate canonical receipt bytes against this host authority."""

        if authority_id != self.authority_id or authority_version != self.authority_version:
            raise HostAuthorityError("host selection belongs to another sealing authority")
        expected = self.sign(payload)
        if not hmac.compare_digest(signature, expected):
            raise HostAuthorityError("host selection signature is not authentic")


def _default_authority_root() -> Path:
    configured = os.environ.get(_AUTHORITY_ROOT_ENV)
    if configured:
        return Path(configured).expanduser().absolute()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "assurance-agent" / "host-authority"
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base.absolute() / "assurance-agent" / "host-authority"


def _load_or_create_key(root_fd: int) -> bytes:
    lock_fd = os.open(
        _LOCK_NAME, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=root_fd
    )
    try:
        _require_private_regular(lock_fd, "host authority lock")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            try:
                key_fd = os.open(_KEY_NAME, _FILE_READ_FLAGS, dir_fd=root_fd)
            except FileNotFoundError:
                _install_key(root_fd)
                key_fd = os.open(_KEY_NAME, _FILE_READ_FLAGS, dir_fd=root_fd)
            try:
                opened = _require_private_regular(key_fd, "host authority key")
                key = _read_all(key_fd)
                final = os.fstat(key_fd)
                if (
                    final.st_nlink != 1
                    or (final.st_dev, final.st_ino) != (opened.st_dev, opened.st_ino)
                    or final.st_size != opened.st_size
                    or final.st_mtime_ns != opened.st_mtime_ns
                ):
                    raise HostAuthorityError("host sealing authority key changed while opening")
            finally:
                os.close(key_fd)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError as error:
        raise HostAuthorityError("host sealing authority cannot be opened") from error
    finally:
        os.close(lock_fd)
    if len(key) != _KEY_BYTES:
        raise HostAuthorityError("host sealing authority key is invalid")
    return key


def _install_key(root_fd: int) -> None:
    temporary = f".selection-authority-{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, _FILE_WRITE_FLAGS, 0o600, dir_fd=root_fd)
    installed = False
    try:
        _write_all(descriptor, secrets.token_bytes(_KEY_BYTES))
        os.fsync(descriptor)
        _require_private_regular(descriptor, "host authority key")
    finally:
        os.close(descriptor)
    try:
        try:
            os.link(
                temporary,
                _KEY_NAME,
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
                follow_symlinks=False,
            )
            installed = True
            os.fsync(root_fd)
        except FileExistsError:
            pass
    finally:
        os.unlink(temporary, dir_fd=root_fd)
    if not installed:
        return


def _require_private_regular(descriptor: int, kind: str) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise HostAuthorityError(f"{kind} is not private")
    return metadata


def _read_all(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 4096):
        chunks.append(chunk)
    return b"".join(chunks)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written == 0:
            raise HostAuthorityError("host authority write failed")
        view = view[written:]


__all__ = ["HostAuthorityError", "HostSealingAuthority"]
