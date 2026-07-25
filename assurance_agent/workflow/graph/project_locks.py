"""Cross-process advisory locks for synchronized project resources."""

from __future__ import annotations

import errno
import fcntl
import hashlib
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import BinaryIO, Protocol

from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.graph_types import ErrorKind
from assurance_agent.workflow.graph.leases import Clock, SystemClock

_LOCKS_RELPATH = Path("qa") / ".graph-runtime" / "locks"
_DEFAULT_POLL_SECONDS = 0.01


class ProjectResourceConflict(AaError):
    """A synchronized project resource stayed busy until the acquisition deadline."""

    error_kind: ErrorKind = "conflict"


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

        self._locks_root.mkdir(parents=True, exist_ok=True)
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
                        f"timed out acquiring synchronized project resource {token}"
                    ) from None
                self._clock.sleep(min(self._poll_interval_seconds, deadline - now))


__all__ = ["ProjectLockManager", "ProjectResourceConflict", "ProjectResourceLockManager"]
