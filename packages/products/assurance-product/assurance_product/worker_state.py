"""Validated, durable worker ownership records and their short control lock."""

from __future__ import annotations
import fcntl
import json
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, Literal
from typing_extensions import NotRequired, TypedDict
from pydantic import ConfigDict, Field, TypeAdapter, ValidationError


class ProcessIdentity(TypedDict):
    pid: int
    created: str
    boot: str
    host: str
    pgid: int
    call_digest: NotRequired[str]


class WorkerRecord(TypedDict):
    """On-disk identity/evidence, distinct from the live ExecutionOwner lock handle."""

    schema: Literal[1]
    workspace: str
    directory: Annotated[list[int], Field(min_length=2, max_length=2)]
    invocation: str
    run_dir: str | None
    nonce: str
    process: ProcessIdentity
    state: Literal["running", "reserved", "stopping", "stopped"]
    children: list[ProcessIdentity]
    calls: list[str]
    servers: NotRequired[list[ProcessIdentity]]
    attempts: NotRequired[list[str]]
    launching_service: NotRequired[bool]
    stop_authority: NotRequired[dict[str, Any]]
    stop_error: NotRequired[str]


# Configure validation without adding configuration keys to the persisted shape.
setattr(ProcessIdentity, "__pydantic_config__", ConfigDict(extra="forbid"))
setattr(WorkerRecord, "__pydantic_config__", ConfigDict(extra="forbid"))
_RECORD = TypeAdapter(WorkerRecord)


class ExecutionConflict(RuntimeError):
    """The workspace still belongs to an execution that has not been stopped."""


def control_root(workspace: Path) -> Path:
    return workspace.resolve() / ".aa" / "runtime" / "worker"


def read_owner(root: Path) -> WorkerRecord | None:
    path = root / "owner.json"
    if not path.exists():
        return None
    if path.is_symlink():
        raise ExecutionConflict("worker ownership must be a regular file")
    try:
        owner = json.loads(path.read_text())
    except (ValueError, UnicodeError) as error:
        raise ExecutionConflict("invalid worker ownership record; stop identity is unconfirmed") from error
    if not isinstance(owner, dict) or type(owner.get("schema")) is not int or owner["schema"] != 1:
        raise ExecutionConflict("legacy worker ownership cannot be resumed; use a fresh isolated run")
    try:
        return _RECORD.validate_python(owner, strict=True)
    except ValidationError as error:
        raise ExecutionConflict("invalid worker ownership record; stop identity is unconfirmed") from error


def write_owner(root: Path, owner: WorkerRecord) -> None:
    temporary = root / f".owner-{secrets.token_hex(8)}"
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(owner, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / "owner.json")
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def control_lock(root: Path) -> Iterator[None]:
    for directory in (root.parent.parent, root.parent, root):
        if directory.is_symlink():
            raise ExecutionConflict("worker control directories must be real directories")
        directory.mkdir(exist_ok=True, mode=0o700)
        if not directory.is_dir():
            raise ExecutionConflict("worker control path is not a directory")
    fd = os.open(root / "control.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)
