"""Independent durable-effect retry sidecar and root terminal fence.

Backoff state must not share the progression ledger lock whose contention may
be the failure mode. Every schedule/reconcile path acquires the root fence
guard before any progression lock. The seam stays dormant for packaged
supersede until Task 13.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from assurance_agent.artifacts.canonical import canonical_json_bytes, sha256_bytes
from assurance_agent.artifacts.models.common import StrictWireModel
from assurance_agent.exceptions import AaError

_RETRY_RELPATH = Path("qa") / ".graph-runtime" / "effect-retries"
_FENCE_RELPATH = Path("qa") / ".graph-runtime" / "root-effect-fences"
_RFC3339_Z = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})T(?P<time>\d{2}:\d{2}:\d{2})(?P<frac>\.\d+)?Z$")
_DEFAULT_INITIAL_SECONDS = 1.0
_DEFAULT_MULTIPLIER = 2.0
_DEFAULT_MAX_SECONDS = 60.0

# Named fence/retry descriptors for runtime_commit_safety/v1 (Task 10).
# Digest these methods/models — not unrelated mutable module bytes.
COMMIT_SAFETY_INVENTORY: tuple[tuple[str, str, str], ...] = (
    (
        "assurance_agent.workflow.graph.effect_retry.EffectRetryStateV1",
        "model",
        "effect_retry_sidecar",
    ),
    (
        "assurance_agent.workflow.graph.effect_retry.EffectRetryStore.load",
        "protocol",
        "effect_retry_sidecar",
    ),
    (
        "assurance_agent.workflow.graph.effect_retry.EffectRetryStore.schedule_next",
        "protocol",
        "effect_retry_sidecar",
    ),
    (
        "assurance_agent.workflow.graph.effect_retry.EffectRetryStore.clear_if_acknowledged",
        "protocol",
        "effect_retry_sidecar",
    ),
    (
        "assurance_agent.workflow.graph.effect_retry.default_retry_policy_digest",
        "helper",
        "effect_retry_sidecar",
    ),
    (
        "assurance_agent.workflow.graph.effect_retry.capped_backoff_seconds",
        "helper",
        "effect_retry_sidecar",
    ),
    (
        "assurance_agent.workflow.graph.effect_retry.parse_rfc3339_z",
        "helper",
        "effect_retry_sidecar",
    ),
    (
        "assurance_agent.workflow.graph.effect_retry.RootTerminalFenceStateV1",
        "model",
        "root_terminal_fence.prepare_terminal",
    ),
    (
        "assurance_agent.workflow.graph.effect_retry.RootEffectFenceStore.guard",
        "protocol",
        "root_terminal_fence.guard",
    ),
    (
        "assurance_agent.workflow.graph.effect_retry.RootEffectFenceStore.reject_if_terminal",
        "protocol",
        "root_terminal_fence.reject_if_terminal",
    ),
    (
        "assurance_agent.workflow.graph.effect_retry.RootEffectFenceStore.prepare_terminal",
        "protocol",
        "root_terminal_fence.prepare_terminal",
    ),
    (
        "assurance_agent.workflow.graph.effect_retry.RootEffectFenceStore.commit_terminal",
        "protocol",
        "root_terminal_fence.commit_terminal",
    ),
    (
        "assurance_agent.workflow.graph.effect_retry.RootEffectFenceStore.abort_prepared",
        "protocol",
        "root_terminal_fence.abort_prepared",
    ),
)


class EffectRetryError(AaError):
    """Retry sidecar is missing, malformed, or CAS-conflicted."""


class RootTerminalFenceError(AaError):
    """Root terminal fence rejects progression or is malformed."""


class EffectRetryStateV1(StrictWireModel):
    schema_version: Literal["1"]
    root_invocation_id: str
    invocation_id: str
    task_id: str
    attempt_id: str
    effect_id: str
    kind: str
    lock_key: str
    ordinal: Annotated[int, Field(strict=True, gt=0)]
    retry_policy_digest: str
    next_retry_at: str
    last_retryable_error_code: str

    @field_validator(
        "root_invocation_id",
        "invocation_id",
        "task_id",
        "attempt_id",
        "effect_id",
        "kind",
        "lock_key",
        "retry_policy_digest",
        "next_retry_at",
        "last_retryable_error_code",
    )
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    @model_validator(mode="after")
    def validate_timestamp(self) -> EffectRetryStateV1:
        parse_rfc3339_z(self.next_retry_at)
        return self


class RootTerminalFenceStateV1(StrictWireModel):
    schema_version: Literal["1"]
    root_invocation_id: str
    status: Literal["prepared", "committed"]
    supersede_id: str | None = None
    prepared_at: str
    committed_at: str | None = None

    @field_validator("root_invocation_id", "prepared_at")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    @model_validator(mode="after")
    def validate_timestamps_and_status(self) -> RootTerminalFenceStateV1:
        parse_rfc3339_z(self.prepared_at)
        if self.committed_at is not None:
            parse_rfc3339_z(self.committed_at)
        if self.status == "committed" and self.committed_at is None:
            raise ValueError("committed fence requires committed_at")
        if self.status == "prepared" and self.committed_at is not None:
            raise ValueError("prepared fence must not set committed_at")
        return self


def parse_rfc3339_z(value: str) -> datetime:
    """Parse canonical RFC 3339 UTC with a literal ``Z`` suffix (no coercion)."""
    match = _RFC3339_Z.fullmatch(value)
    if match is None:
        raise ValueError(f"timestamp must be canonical RFC 3339 UTC with Z: {value!r}")
    fraction = match.group("frac") or ""
    if fraction:
        # Normalize fractional seconds to microseconds.
        digits = fraction[1:]
        micros = int((digits + "000000")[:6])
        base = datetime.strptime(
            f"{match.group('date')}T{match.group('time')}",
            "%Y-%m-%dT%H:%M:%S",
        ).replace(tzinfo=timezone.utc, microsecond=micros)
        return base
    return datetime.strptime(
        f"{match.group('date')}T{match.group('time')}",
        "%Y-%m-%dT%H:%M:%S",
    ).replace(tzinfo=timezone.utc)


def format_rfc3339_z(value: datetime) -> str:
    utc = value.astimezone(timezone.utc)
    if utc.microsecond:
        return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond:06d}Z"
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def default_retry_policy_digest() -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "initial_seconds": _DEFAULT_INITIAL_SECONDS,
                "max_seconds": _DEFAULT_MAX_SECONDS,
                "multiplier": _DEFAULT_MULTIPLIER,
                "schema": "effect_retry_policy/v1",
            }
        )
    )


def capped_backoff_seconds(ordinal: int) -> float:
    """Deterministic capped exponential backoff for sidecar ordinal N."""
    if ordinal < 1:
        raise ValueError("ordinal must be >= 1")
    delay = _DEFAULT_INITIAL_SECONDS * (_DEFAULT_MULTIPLIER ** (ordinal - 1))
    return min(delay, _DEFAULT_MAX_SECONDS)


class EffectRetryStore:
    """Coordinator-owned CAS sidecar for unacknowledged effect backoff."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root.resolve()
        self._root = self._project_root / _RETRY_RELPATH
        self._thread_locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def load(self, effect_id: str) -> EffectRetryStateV1 | None:
        path = self._path(effect_id)
        if not path.exists():
            return None
        return self._read(path)

    def schedule_next(
        self,
        *,
        fence_store: RootEffectFenceStore,
        root_invocation_id: str,
        invocation_id: str,
        task_id: str,
        attempt_id: str,
        effect_id: str,
        kind: str,
        lock_key: str,
        error_code: str,
        now: datetime,
        expected: EffectRetryStateV1 | None = None,
    ) -> EffectRetryStateV1 | None:
        """Advance one ordinal when due, or create ordinal 1. CAS loser returns None."""
        with fence_store.guard(root_invocation_id):
            fence_store.reject_if_terminal(root_invocation_id)
            return self._schedule_next_unlocked(
                root_invocation_id=root_invocation_id,
                invocation_id=invocation_id,
                task_id=task_id,
                attempt_id=attempt_id,
                effect_id=effect_id,
                kind=kind,
                lock_key=lock_key,
                error_code=error_code,
                now=now,
                expected=expected,
            )

    def clear_if_acknowledged(self, effect_id: str) -> None:
        path = self._path(effect_id)
        if path.exists() or path.is_symlink():
            try:
                path.unlink()
            except FileNotFoundError:
                return
            self._fsync_dir(path.parent)

    def is_due(self, state: EffectRetryStateV1, *, now: datetime) -> bool:
        return now >= parse_rfc3339_z(state.next_retry_at)

    def _schedule_next_unlocked(
        self,
        *,
        root_invocation_id: str,
        invocation_id: str,
        task_id: str,
        attempt_id: str,
        effect_id: str,
        kind: str,
        lock_key: str,
        error_code: str,
        now: datetime,
        expected: EffectRetryStateV1 | None,
    ) -> EffectRetryStateV1 | None:
        path = self._path(effect_id)
        with self._file_lock(effect_id):
            current = self.load(effect_id)
            if expected is not None:
                if current is None or current.model_dump(mode="json") != expected.model_dump(mode="json"):
                    return None
                if not self.is_due(current, now=now):
                    return current
                next_ordinal = current.ordinal + 1
            elif current is None:
                next_ordinal = 1
            else:
                if not self.is_due(current, now=now):
                    return current
                next_ordinal = current.ordinal + 1
            delay = capped_backoff_seconds(next_ordinal)
            state = EffectRetryStateV1(
                schema_version="1",
                root_invocation_id=root_invocation_id,
                invocation_id=invocation_id,
                task_id=task_id,
                attempt_id=attempt_id,
                effect_id=effect_id,
                kind=kind,
                lock_key=lock_key,
                ordinal=next_ordinal,
                retry_policy_digest=default_retry_policy_digest(),
                next_retry_at=format_rfc3339_z(now + timedelta(seconds=delay)),
                last_retryable_error_code=error_code,
            )
            self._atomic_write(path, state)
            return state

    def _path(self, effect_id: str) -> Path:
        if not effect_id or any(ch in effect_id for ch in "/\\"):
            raise EffectRetryError("unsafe effect_id for retry sidecar")
        digest = hashlib.sha256(effect_id.encode("utf-8")).hexdigest()
        return self._prepare_root() / f"{digest}.json"

    def _prepare_root(self) -> Path:
        return _prepare_confined_directory(self._project_root, _RETRY_RELPATH)

    def _thread_lock(self, effect_id: str) -> threading.Lock:
        with self._guard:
            lock = self._thread_locks.get(effect_id)
            if lock is None:
                lock = threading.Lock()
                self._thread_locks[effect_id] = lock
            return lock

    @contextmanager
    def _file_lock(self, effect_id: str) -> Iterator[None]:
        path = self._path(effect_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with self._thread_lock(effect_id):
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _read(self, path: Path) -> EffectRetryStateV1:
        try:
            if path.is_symlink():
                raise EffectRetryError(f"retry sidecar cannot be a symlink: {path}")
            payload = json.loads(path.read_text(encoding="utf-8"))
            return EffectRetryStateV1.model_validate(payload)
        except EffectRetryError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise EffectRetryError(f"malformed effect retry sidecar: {path}") from exc

    def _atomic_write(self, path: Path, state: EffectRetryStateV1) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            self._fsync_dir(path.parent)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class RootEffectFenceStore:
    """Root-scoped guard + prepared/committed terminal fence for effect/retry paths."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root.resolve()
        self._root = self._project_root / _FENCE_RELPATH
        self._thread_locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    @contextmanager
    def guard(self, root_invocation_id: str) -> Iterator[None]:
        path = self._lock_path(root_invocation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock(root_invocation_id):
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX)
                        break
                    except OSError as exc:
                        if exc.errno != errno.EINTR:
                            raise
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def load(self, root_invocation_id: str) -> RootTerminalFenceStateV1 | None:
        path = self._state_path(root_invocation_id)
        if not path.exists():
            return None
        return self._read(path)

    def reject_if_terminal(self, root_invocation_id: str) -> None:
        state = self.load(root_invocation_id)
        if state is None:
            return
        if state.status == "prepared":
            raise RootTerminalFenceError(
                f"root terminal fence prepared; suppressing effect/retry for {root_invocation_id}"
            )
        if state.status == "committed":
            raise RootTerminalFenceError(
                f"root terminal fence committed; permanently rejecting effect/retry for {root_invocation_id}"
            )

    def prepare_terminal(
        self,
        root_invocation_id: str,
        *,
        supersede_id: str | None = None,
        now: datetime | None = None,
    ) -> RootTerminalFenceStateV1:
        stamp = format_rfc3339_z(now or datetime.now(timezone.utc))
        with self.guard(root_invocation_id):
            existing = self.load(root_invocation_id)
            if existing is not None:
                if existing.status == "committed":
                    raise RootTerminalFenceError(
                        f"root terminal fence already committed for {root_invocation_id}"
                    )
                return existing
            state = RootTerminalFenceStateV1(
                schema_version="1",
                root_invocation_id=root_invocation_id,
                status="prepared",
                supersede_id=supersede_id,
                prepared_at=stamp,
                committed_at=None,
            )
            self._atomic_write(self._state_path(root_invocation_id), state)
            return state

    def commit_terminal(
        self,
        root_invocation_id: str,
        *,
        now: datetime | None = None,
    ) -> RootTerminalFenceStateV1:
        stamp = format_rfc3339_z(now or datetime.now(timezone.utc))
        with self.guard(root_invocation_id):
            existing = self.load(root_invocation_id)
            if existing is None:
                raise RootTerminalFenceError(
                    f"cannot commit terminal fence without preparation for {root_invocation_id}"
                )
            if existing.status == "committed":
                return existing
            state = RootTerminalFenceStateV1(
                schema_version="1",
                root_invocation_id=root_invocation_id,
                status="committed",
                supersede_id=existing.supersede_id,
                prepared_at=existing.prepared_at,
                committed_at=stamp,
            )
            self._atomic_write(self._state_path(root_invocation_id), state)
            return state

    def abort_prepared(self, root_invocation_id: str) -> None:
        with self.guard(root_invocation_id):
            existing = self.load(root_invocation_id)
            if existing is None:
                return
            if existing.status == "committed":
                raise RootTerminalFenceError(
                    f"cannot abort committed terminal fence for {root_invocation_id}"
                )
            path = self._state_path(root_invocation_id)
            try:
                path.unlink()
            except FileNotFoundError:
                return
            EffectRetryStore._fsync_dir(path.parent)

    def _thread_lock(self, root_invocation_id: str) -> threading.Lock:
        with self._guard:
            lock = self._thread_locks.get(root_invocation_id)
            if lock is None:
                lock = threading.Lock()
                self._thread_locks[root_invocation_id] = lock
            return lock

    def _prepare_root(self) -> Path:
        return _prepare_confined_directory(self._project_root, _FENCE_RELPATH)

    def _state_path(self, root_invocation_id: str) -> Path:
        digest = hashlib.sha256(root_invocation_id.encode("utf-8")).hexdigest()
        return self._prepare_root() / f"{digest}.json"

    def _lock_path(self, root_invocation_id: str) -> Path:
        digest = hashlib.sha256(root_invocation_id.encode("utf-8")).hexdigest()
        return self._prepare_root() / f"{digest}.lock"

    def _read(self, path: Path) -> RootTerminalFenceStateV1:
        try:
            if path.is_symlink():
                raise RootTerminalFenceError(f"terminal fence cannot be a symlink: {path}")
            payload = json.loads(path.read_text(encoding="utf-8"))
            return RootTerminalFenceStateV1.model_validate(payload)
        except RootTerminalFenceError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RootTerminalFenceError(f"malformed root terminal fence: {path}") from exc

    def _atomic_write(self, path: Path, state: RootTerminalFenceStateV1) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            EffectRetryStore._fsync_dir(path.parent)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass


def _prepare_confined_directory(project_root: Path, relpath: Path) -> Path:
    import stat

    current = project_root
    for part in relpath.parts:
        candidate = current / part
        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError:
            try:
                candidate.mkdir()
            except FileExistsError:
                pass
            mode = candidate.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise EffectRetryError(f"runtime directory cannot be a symlink: {candidate}")
        if not stat.S_ISDIR(mode):
            raise EffectRetryError(f"runtime path is not a directory: {candidate}")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(project_root)
        current = candidate
    return current


__all__ = [
    "COMMIT_SAFETY_INVENTORY",
    "EffectRetryError",
    "RootTerminalFenceError",
    "EffectRetryStateV1",
    "RootTerminalFenceStateV1",
    "EffectRetryStore",
    "RootEffectFenceStore",
    "parse_rfc3339_z",
    "format_rfc3339_z",
    "default_retry_policy_digest",
    "capped_backoff_seconds",
]
