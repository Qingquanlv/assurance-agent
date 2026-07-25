"""Unique process-local + cross-process commit boundary for strict audit writes.

Callers stage file writes, strict events, and optional state updates inside a
``transaction``; ``__exit__`` applies them in a fixed order under a composite
advisory lock. Best-effort telemetry must NOT use this module.

Kill/power-loss may leave a legal apply prefix on disk; domain operations own
prefix detection and reconciliation. This module only guarantees that
catchable in-process failures either fully apply or fully restore the
snapshotted files.
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from pydantic import BaseModel

from assurance_agent.artifacts.models import WorkflowState
from assurance_agent.exceptions import AaError
from assurance_agent.workflow.core.events import Ledger, append_event_strict, read_events
from assurance_agent.workflow.core.graph_events import SuperstepCommittedEvent
from assurance_agent.workflow.core.snapshot import capture_files, restore_files
from assurance_agent.workflow.core.state import read_state, state_file, state_guard, write_state

LOCK_FILENAME = ".progression.lock"
_RUNTIME_REL_PREFIX = ".graph-runtime/"
_RESERVED_RELS = frozenset({"events.jsonl", "workflow-state.yaml", LOCK_FILENAME})
# Shared with lease registry; nested subgraphs + 50ms test heartbeats can contend.
_DEFAULT_LOCK_TIMEOUT_S = 5.0
_LOCK_POLL_S = 0.01

# Process-local locks keyed by resolved change_dir path.
# Also used by workflow.graph.leases so lease/progression serialize before fcntl.
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class ProgressionError(AaError):
    """Infrastructure failure; CLI adapters map this to EXIT_ERROR (40)."""


class ProgressionCommitError(ProgressionError):
    """Apply failed and all snapshotted files were restored; cause in __cause__."""


class ProgressionRollbackError(ProgressionError):
    """Apply failed and restore also failed — disk may be partially committed."""

    def __init__(
        self,
        message: str,
        *,
        commit_cause: BaseException,
        rollback_cause: BaseException,
        affected: list[str],
    ) -> None:
        super().__init__(message)
        self.commit_cause = commit_cause
        self.rollback_cause = rollback_cause
        self.affected = affected


class ProgressionLockTimeout(ProgressionError):
    """Could not acquire the per-change advisory lock before the deadline."""


def thread_lock_for(change_dir: Path) -> threading.Lock:
    """Process-local mutex for a change_dir (shared by progression + leases)."""
    key = str(change_dir.resolve())
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


def _thread_lock_for(change_dir: Path) -> threading.Lock:
    return thread_lock_for(change_dir)


def _lock_path(change_dir: Path) -> Path:
    return change_dir / LOCK_FILENAME


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class ProgressionTxn:
    def __init__(self, change_dir: Path) -> None:
        self._change_dir = change_dir
        self._files: list[tuple[str, bytes]] = []
        self._events: list[Mapping[str, object]] = []
        self._next_state: WorkflowState | None = None
        self._state_set = False
        self._state_projection: bytes | None = None

    def read_state(self) -> WorkflowState:
        return read_state(self._change_dir)

    def read_events(self) -> list[dict[str, object]]:
        return read_events(self._change_dir)

    @property
    def ledger(self) -> Ledger:
        return Ledger(self._change_dir)

    def current_state_guard(self) -> str:
        return state_guard(self._change_dir)

    def write_file(self, rel: str, content: bytes | str) -> None:
        resolved = self._resolve_rel(rel)
        data = content.encode("utf-8") if isinstance(content, str) else content
        # Last write for the same rel wins within one txn.
        self._files = [(r, c) for r, c in self._files if r != rel]
        self._files.append((rel, data))
        _ = resolved  # validated

    def write_runtime_file(self, rel: str, content: bytes | str) -> None:
        """graph 运行时专属：stage coordinator 所有的 ``.graph-runtime/`` 文件。

        与 ``write_file`` 共用同一份路径安全校验，仍拒绝保留文件
        （``events.jsonl``/``workflow-state.yaml``/锁文件）、绝对路径与
        目录穿越；额外限制只能落在 ``.graph-runtime/`` 之内。
        """
        resolved = self._resolve_rel(rel)
        normalized = Path(rel).as_posix()
        if not normalized.startswith(_RUNTIME_REL_PREFIX):
            raise ValueError(f"write_runtime_file path must stay under .graph-runtime/: {rel!r}")
        data = content.encode("utf-8") if isinstance(content, str) else content
        self._files = [(r, c) for r, c in self._files if r != rel]
        self._files.append((rel, data))
        _ = resolved  # validated

    def append_strict(self, event: Mapping[str, object] | BaseModel) -> None:
        if isinstance(event, BaseModel):
            dumped = event.model_dump(mode="json", by_alias=True, exclude_none=True)
            self._events.append(dumped)
        else:
            self._events.append(dict(event))

    def set_state(self, state: WorkflowState) -> None:
        if self._state_projection is not None:
            raise ValueError(
                "set_state cannot be mixed with set_workflow_state_projection in one transaction"
            )
        if self._state_set:
            raise ValueError("set_state may be called at most once per transaction")
        self._state_set = True
        self._next_state = state

    def set_workflow_state_projection(self, content: bytes | str) -> None:
        """graph 路径专属：stage 保留文件 ``workflow-state.yaml`` 的投影字节。

        与 ``write_file`` 分开存放（保留文件走独立槽位），随同一事务
        capture/apply/rollback；每事务至多一次，且不得与 v1 ``set_state`` 混用。
        """
        if self._state_projection is not None:
            raise ValueError("set_workflow_state_projection may be called at most once per transaction")
        if self._state_set:
            raise ValueError(
                "set_workflow_state_projection cannot be mixed with set_state in one transaction"
            )
        self._state_projection = content.encode("utf-8") if isinstance(content, str) else content

    def _resolve_rel(self, rel: str) -> Path:
        if not rel or rel.startswith("/") or "\\" in rel:
            raise ValueError(f"write_file path must be a relative change-dir path: {rel!r}")
        if any(part == ".." for part in Path(rel).parts):
            raise ValueError(f"write_file path must not contain '..': {rel!r}")
        normalized = Path(rel).as_posix()
        if normalized in _RESERVED_RELS:
            raise ValueError(f"write_file cannot target reserved file: {rel}")
        target = (self._change_dir / rel).resolve()
        root = self._change_dir.resolve()
        try:
            target.relative_to(root)
        except ValueError as err:
            raise ValueError(f"write_file path escapes change_dir: {rel}") from err
        reserved = {root / name for name in _RESERVED_RELS}
        if target in reserved:
            raise ValueError(f"write_file cannot target reserved file: {rel}")
        return target

    def _apply(self) -> None:
        if (
            not self._files
            and not self._events
            and self._next_state is None
            and self._state_projection is None
        ):
            return

        targets: list[Path] = [self._change_dir / rel for rel, _ in self._files]
        if self._state_projection is not None:
            targets.append(state_file(self._change_dir))
        targets.append(self._change_dir / "events.jsonl")
        if self._next_state is not None:
            targets.append(state_file(self._change_dir))

        snapshots = capture_files(targets)
        try:
            for rel, data in self._files:
                _atomic_write_bytes(self._change_dir / rel, data)
            if self._state_projection is not None:
                _atomic_write_bytes(state_file(self._change_dir), self._state_projection)
            for event in self._events:
                append_event_strict(self._change_dir, event)
            if self._next_state is not None:
                write_state(self._change_dir, self._next_state)
        except Exception as commit_err:
            try:
                restore_files(snapshots)
            except Exception as rollback_err:
                affected = [str(p) for p in targets]
                raise ProgressionRollbackError(
                    "progression apply failed and restore also failed; "
                    f"disk may be partially committed: {affected}",
                    commit_cause=commit_err,
                    rollback_cause=rollback_err,
                    affected=affected,
                ) from rollback_err
            raise ProgressionCommitError(str(commit_err)) from commit_err


@contextmanager
def transaction(
    change_dir: Path,
    *,
    lock_timeout_s: float = _DEFAULT_LOCK_TIMEOUT_S,
) -> Iterator[ProgressionTxn]:
    """Acquire composite lock, yield a staging txn, apply or restore on exit."""
    change_dir.mkdir(parents=True, exist_ok=True)
    thread_lock = _thread_lock_for(change_dir)
    deadline = time.monotonic() + lock_timeout_s

    if not thread_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise ProgressionLockTimeout(f"thread lock timeout for {change_dir}")

    lock_fd: int | None = None
    try:
        lock_path = _lock_path(change_dir)
        lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ProgressionLockTimeout(f"fcntl lock timeout for {change_dir}") from None
                time.sleep(_LOCK_POLL_S)

        txn = ProgressionTxn(change_dir)
        try:
            yield txn
        except BaseException:
            # Block-local exceptions: no business data written; just release lock.
            raise
        else:
            txn._apply()
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(lock_fd)
            except OSError:
                pass
        thread_lock.release()


def commit_tree_pointer(
    change_dir: Path,
    *,
    event: SuperstepCommittedEvent,
    checkpoint_rel: str,
    checkpoint_bytes: bytes,
) -> None:
    """coordinator 的 tree pointer 提交：checkpoint 字节与 strict commit 事件同事务落盘。

    strict event/tree pointer 先于 canonical 文件物化提交；物化中断时，下一次
    运行时调用从 ledger 读 ``target_tree_id`` 并幂等修复 canonical 文件，
    不重跑任何 task。
    """
    with transaction(change_dir) as txn:
        txn.write_runtime_file(checkpoint_rel, checkpoint_bytes)
        txn.append_strict(event)


# Re-export for type checkers / callers that probe internals in tests.
__all__ = [
    "ProgressionError",
    "ProgressionCommitError",
    "ProgressionRollbackError",
    "ProgressionLockTimeout",
    "ProgressionTxn",
    "commit_tree_pointer",
    "transaction",
    "thread_lock_for",
    "LOCK_FILENAME",
]
