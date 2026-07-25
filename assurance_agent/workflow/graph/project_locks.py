"""Cross-process advisory locks for synchronized project resources."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import stat
import threading
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, Protocol

from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.graph_types import ErrorKind
from assurance_agent.workflow.graph.leases import Clock, SystemClock

_LOCKS_RELPATH = Path("qa") / ".graph-runtime" / "locks"
_PUBLICATIONS_RELPATH = Path("qa") / ".graph-runtime" / "publications"
_DEFAULT_POLL_SECONDS = 0.01


class ProjectResourceConflict(AaError):
    """A synchronized project resource stayed busy until the acquisition deadline."""

    error_kind: ErrorKind = "conflict"

    def __init__(self, message: str, *, token: str | None = None) -> None:
        super().__init__(message)
        self.token = token


class ProjectLockPathError(AaError):
    """The on-disk lock directory is not safely confined to the project root."""


class ProjectPublicationError(AaError):
    """A durable synchronized-publication marker is missing or malformed."""


@dataclass(frozen=True)
class ProjectPublication:
    publication_id: str
    invocation_id: str
    write_set_ids: tuple[str, ...]
    tokens: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.publication_id) != 64 or any(
            char not in "0123456789abcdef" for char in self.publication_id
        ):
            raise ValueError("publication_id must be a lowercase SHA-256 digest")
        if not self.invocation_id:
            raise ValueError("publication invocation_id must not be empty")
        if not self.write_set_ids:
            raise ValueError("publication must identify at least one write-set")
        if len(set(self.write_set_ids)) != len(self.write_set_ids):
            raise ValueError("publication write_set_ids must be unique")
        if tuple(sorted(set(self.tokens))) != self.tokens or not self.tokens:
            raise ValueError("publication tokens must be non-empty, sorted, and unique")
        if any(not token.startswith("project:") for token in self.tokens):
            raise ValueError("publication tokens must use project:*")


def _prepare_confined_directory(project_root: Path, relpath: Path) -> Path:
    """Create every directory component without accepting symlinks or escapes."""
    current = project_root
    for part in relpath.parts:
        candidate = current / part
        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError:
            try:
                candidate.mkdir()
            except FileExistsError:
                # A concurrent creator won the race; validate what it created.
                pass
            try:
                mode = candidate.lstat().st_mode
            except FileNotFoundError as exc:
                raise ProjectLockPathError(
                    f"project lock directory disappeared during creation: {candidate}"
                ) from exc
        if stat.S_ISLNK(mode):
            raise ProjectLockPathError(f"project lock directory cannot be a symlink: {candidate}")
        if not stat.S_ISDIR(mode):
            raise ProjectLockPathError(f"project lock directory is not a directory: {candidate}")
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(project_root)
        except (OSError, ValueError) as exc:
            raise ProjectLockPathError(
                f"project lock directory escapes project root: {candidate}"
            ) from exc
        current = candidate
    return current


class ProjectPublicationStore:
    """Project-global write-ahead acknowledgements for synchronized publication."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root.resolve()
        self._root = self._project_root / _PUBLICATIONS_RELPATH

    def assert_no_prepared(
        self,
        tokens: Sequence[str],
        *,
        allowed_publication_id: str | None = None,
    ) -> None:
        root = self._prepare_root()
        requested = set(tokens)
        for path in sorted(root.glob("*.json")):
            publication, status = self._load(path)
            if status != "prepared" or publication.publication_id == allowed_publication_id:
                continue
            overlap = sorted(requested.intersection(publication.tokens))
            if overlap:
                token = overlap[0]
                raise ProjectResourceConflict(
                    f"synchronized project resource {token} has an unacknowledged publication",
                    token=token,
                )

    def prepare(self, publication: ProjectPublication) -> Literal["prepared", "applied"]:
        path = self._path(publication.publication_id)
        if path.exists() or path.is_symlink():
            existing, status = self._load(path)
            if existing != publication:
                raise ProjectPublicationError(
                    f"publication identity mismatch for {publication.publication_id}"
                )
            return status
        self._write(path, publication, "prepared")
        return "prepared"

    def acknowledge(self, publication: ProjectPublication) -> None:
        path = self._path(publication.publication_id)
        existing, status = self._load(path)
        if existing != publication:
            raise ProjectPublicationError(
                f"publication identity mismatch for {publication.publication_id}"
            )
        if status == "applied":
            return
        self._write(path, publication, "applied")

    def _prepare_root(self) -> Path:
        return _prepare_confined_directory(self._project_root, _PUBLICATIONS_RELPATH)

    def _path(self, publication_id: str) -> Path:
        if len(publication_id) != 64 or any(
            char not in "0123456789abcdef" for char in publication_id
        ):
            raise ProjectPublicationError("unsafe synchronized publication identity")
        return self._prepare_root() / f"{publication_id}.json"

    def _load(self, path: Path) -> tuple[ProjectPublication, Literal["prepared", "applied"]]:
        try:
            if path.is_symlink():
                raise ProjectPublicationError(f"publication marker cannot be a symlink: {path}")
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or set(payload) != {
                "schema_version",
                "publication_id",
                "invocation_id",
                "write_set_ids",
                "tokens",
                "status",
            }:
                raise ValueError("unexpected publication marker shape")
            if payload["schema_version"] != 1:
                raise ValueError("unsupported publication marker schema")
            raw_write_sets = payload["write_set_ids"]
            raw_tokens = payload["tokens"]
            if not isinstance(raw_write_sets, list) or not all(
                isinstance(value, str) for value in raw_write_sets
            ):
                raise ValueError("invalid publication write_set_ids")
            if not isinstance(raw_tokens, list) or not all(
                isinstance(value, str) for value in raw_tokens
            ):
                raise ValueError("invalid publication tokens")
            publication = ProjectPublication(
                publication_id=str(payload["publication_id"]),
                invocation_id=str(payload["invocation_id"]),
                write_set_ids=tuple(raw_write_sets),
                tokens=tuple(raw_tokens),
            )
            status = payload["status"]
            if status not in ("prepared", "applied"):
                raise ValueError("invalid publication status")
        except ProjectPublicationError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ProjectPublicationError(f"invalid synchronized publication marker: {path}") from exc
        return publication, status

    def _write(
        self,
        path: Path,
        publication: ProjectPublication,
        status: Literal["prepared", "applied"],
    ) -> None:
        payload = {
            "schema_version": 1,
            "publication_id": publication.publication_id,
            "invocation_id": publication.invocation_id,
            "write_set_ids": list(publication.write_set_ids),
            "tokens": list(publication.tokens),
            "status": status,
        }
        raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
        try:
            with tmp.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise ProjectPublicationError(
                f"failed to persist synchronized publication marker: {path}"
            ) from exc
        finally:
            tmp.unlink(missing_ok=True)


class ProjectLockManager(Protocol):
    def acquire(
        self, tokens: Sequence[str], timeout_seconds: float
    ) -> AbstractContextManager[None]: ...


class ProjectResourceLockManager:
    """Acquire deterministic ``fcntl`` locks for sorted ``project:*`` tokens."""

    def __init__(
        self,
        project_root: Path,
        *,
        clock: Clock | None = None,
        poll_interval_seconds: float = _DEFAULT_POLL_SECONDS,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._project_root = project_root.resolve()
        self._locks_root = self._project_root / _LOCKS_RELPATH
        self._clock = clock or SystemClock()
        self._poll_interval_seconds = poll_interval_seconds

    def _path_for(self, token: str) -> Path:
        identity = f"{self._project_root}\0{token}".encode()
        return self._locks_root / hashlib.sha256(identity).hexdigest()

    def _prepare_locks_root(self) -> None:
        """Create each lock parent without following a pre-existing symlink."""
        _prepare_confined_directory(self._project_root, _LOCKS_RELPATH)

    @contextmanager
    def acquire(self, tokens: Sequence[str], timeout_seconds: float) -> Iterator[None]:
        """Acquire sorted project tokens without blocking in the kernel; always release."""
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        ordered = tuple(sorted(set(tokens)))
        for token in ordered:
            if not token.startswith("project:") or not token.removeprefix("project:").strip():
                raise ValueError(f"project resource lock token must use project:*: {token!r}")
        if not ordered:
            yield
            return

        self._prepare_locks_root()
        deadline = self._clock.monotonic() + timeout_seconds
        acquired: list[BinaryIO] = []
        try:
            for token in ordered:
                handle = self._path_for(token).open("a+b")
                try:
                    self._acquire_one(handle, token=token, deadline=deadline)
                except BaseException:
                    handle.close()
                    raise
                acquired.append(handle)
            yield
        finally:
            for handle in reversed(acquired):
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()

    def _acquire_one(self, handle: BinaryIO, *, token: str, deadline: float) -> None:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                now = self._clock.monotonic()
                if now >= deadline:
                    raise ProjectResourceConflict(
                        f"timed out acquiring synchronized project resource {token}",
                        token=token,
                    ) from None
                self._clock.sleep(min(self._poll_interval_seconds, deadline - now))


__all__ = [
    "ProjectLockManager",
    "ProjectLockPathError",
    "ProjectPublication",
    "ProjectPublicationError",
    "ProjectPublicationStore",
    "ProjectResourceConflict",
    "ProjectResourceLockManager",
]
