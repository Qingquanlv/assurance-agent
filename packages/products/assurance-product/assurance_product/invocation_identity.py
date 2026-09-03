from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field

from graph_engine.canonical import canonical_digest, canonical_json_bytes
from graph_engine.plugin_api import FrozenModel

from assurance_product.change_workspace import ChangeWorkspace

_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_TEST_CRASH_AT: str | None = None


class SelectionCrash(RuntimeError):
    """Raised by the test-only handshake crash injector."""


class RuntimeSelectionError(ValueError):
    """Raised when an invocation identity record is invalid."""


class InvocationIdentityRecord(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["1"]
    phase: Literal["initializing", "initialized"]
    invocation_id: str
    entrypoint: str
    root_input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    product_lock_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision_id: str = Field(pattern=r"^[0-9a-f]{64}$")


def identity_path(workspace: ChangeWorkspace, invocation_id: str) -> Path:
    return workspace.paths.langgraph_identities / f"{invocation_id}.json"


def maybe_crash(point: str) -> None:
    if _TEST_CRASH_AT == point:
        raise SelectionCrash(point)
    application = sys.modules.get("assurance_product.application")
    if application is not None and getattr(application, "_TEST_CRASH_AT", None) == point:
        raise SelectionCrash(point)


def load_identity(workspace: ChangeWorkspace, invocation_id: str) -> InvocationIdentityRecord | None:
    path = identity_path(workspace, invocation_id)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise RuntimeSelectionError("identity record must be a regular file")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeSelectionError("identity record is corrupt") from error
    if not isinstance(payload, dict):
        raise RuntimeSelectionError("identity record is corrupt")
    try:
        record = InvocationIdentityRecord.model_validate(payload)
    except Exception as error:
        raise RuntimeSelectionError("identity record is corrupt") from error
    if raw != _identity_bytes(record):
        raise RuntimeSelectionError("identity record is not canonical")
    return record


def write_initializing(
    workspace: ChangeWorkspace, record: InvocationIdentityRecord
) -> InvocationIdentityRecord:
    if record.phase != "initializing":
        raise RuntimeSelectionError("phase 1 must write an initializing record")
    workspace.paths.langgraph_root.mkdir(mode=0o700, exist_ok=True)
    workspace.paths.langgraph_identities.mkdir(mode=0o700, exist_ok=True)
    path = identity_path(workspace, record.invocation_id)
    encoded = _identity_bytes(record)
    with _namespace_lock(workspace):
        existing = load_identity(workspace, record.invocation_id)
        if existing is not None:
            _assert_same_identity(existing, record)
            if existing.phase == "initialized":
                raise RuntimeSelectionError("identity record phase cannot regress")
            if existing.phase == "initializing":
                return existing
            raise RuntimeSelectionError("identity record disagrees with the requested identity")
        _atomic_replace(path, encoded)
    maybe_crash("after_initializing")
    return record


def complete_initialized(
    workspace: ChangeWorkspace, record: InvocationIdentityRecord
) -> InvocationIdentityRecord:
    if record.phase != "initialized":
        raise RuntimeSelectionError("phase 3 must write an initialized identity record")
    path = identity_path(workspace, record.invocation_id)
    encoded = _identity_bytes(record)
    with _namespace_lock(workspace):
        existing = load_identity(workspace, record.invocation_id)
        if existing is None:
            raise RuntimeSelectionError("initialized replacement requires an initializing record")
        _assert_same_identity(existing, record)
        if existing.phase == "initialized":
            if _identity_bytes(existing) != encoded:
                raise RuntimeSelectionError("initialized identity record disagrees with evidence")
            return existing
        maybe_crash("before_initialized")
        _atomic_replace(path, encoded)
    return record


def require_initialized(record: InvocationIdentityRecord) -> InvocationIdentityRecord:
    if record.phase != "initialized":
        raise RuntimeSelectionError("only an initialized identity record is resumable")
    return record


def record_digest(record: InvocationIdentityRecord) -> str:
    return canonical_digest(record.model_dump(mode="json"))


def _assert_same_identity(existing: InvocationIdentityRecord, requested: InvocationIdentityRecord) -> None:
    if (
        existing.invocation_id != requested.invocation_id
        or existing.entrypoint != requested.entrypoint
        or existing.root_input_digest != requested.root_input_digest
        or existing.product_lock_digest != requested.product_lock_digest
        or existing.revision_id != requested.revision_id
    ):
        raise RuntimeSelectionError("identity record disagrees with the requested identity")


def _identity_bytes(record: InvocationIdentityRecord) -> bytes:
    return canonical_json_bytes(record.model_dump(mode="json")) + b"\n"


def _atomic_replace(path: Path, encoded: bytes) -> None:
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise RuntimeSelectionError("identity record must be a regular file")
    pending = path.with_name(f".{path.name}.pending")
    pending.write_bytes(encoded)
    os.replace(pending, path)


def _namespace_lock(workspace: ChangeWorkspace):
    workspace.paths.langgraph_identities.mkdir(mode=0o700, exist_ok=True)

    class _Lock:
        def __enter__(self) -> None:
            self._fd = os.open(str(workspace.paths.langgraph_identities), _DIRECTORY_FLAGS)
            fcntl.flock(self._fd, fcntl.LOCK_EX)

        def __exit__(self, *_args: object) -> None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)

    return _Lock()


__all__ = [
    "InvocationIdentityRecord",
    "RuntimeSelectionError",
    "SelectionCrash",
    "complete_initialized",
    "identity_path",
    "load_identity",
    "maybe_crash",
    "record_digest",
    "require_initialized",
    "write_initializing",
]
