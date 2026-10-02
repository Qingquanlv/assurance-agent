"""Domain-free artifact refs and the one workspace reader for them."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Literal, TypeVar, overload

import yaml
from pydantic import BaseModel, Field, ValidationError

from graph_engine.errors import GraphEngineError
from graph_engine.plugin_api import FrozenModel

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
ModelT = TypeVar("ModelT", bound=BaseModel)
ArtifactLoader = Literal["bytes", "json", "yaml"]


class ArtifactReadError(GraphEngineError):
    """A workspace artifact ref could not be authenticated."""

    def __init__(self, message: str, *, reason: str, path: str = "") -> None:
        super().__init__(message)
        self.reason = reason
        self.path = path


class ArtifactRef(FrozenModel):
    """Committed workspace file: canonical relative path plus sha256."""

    path: str = Field(min_length=1)
    digest: str = Field(pattern=_SHA256_PATTERN)


def is_canonical_relative(path: str) -> bool:
    posix = PurePosixPath(path)
    return not (
        posix.is_absolute()
        or "\\" in path
        or (len(path) >= 2 and path[1] == ":")
        or posix.as_posix() != path
        or any(part in {"", ".", ".."} for part in posix.parts)
    )


def coerce_artifact_ref(ref: ArtifactRef | Mapping[str, object]) -> ArtifactRef:
    if isinstance(ref, ArtifactRef):
        return ref
    if isinstance(ref, BaseModel):
        return ArtifactRef.model_validate(ref.model_dump(mode="json"))
    try:
        return ArtifactRef.model_validate(ref)
    except ValidationError as error:
        raise ArtifactReadError("artifact ref is not a path and sha256", reason="path") from error


def match_artifact_pattern(pattern: str, path: str) -> bool:
    """Match ``path`` against a relative glob. ``**`` crosses directories; ``*`` does not."""
    if not is_canonical_relative(path):
        return False
    if "\\" in pattern or pattern.startswith("/") or ".." in PurePosixPath(pattern).parts:
        return False
    if any(part in {"", ".", ".."} for part in pattern.split("/")):
        return False
    return _match_parts(pattern.split("/"), path.split("/"))


def refs_from_write_set(write_set: object) -> tuple[ArtifactRef, ...]:
    """Path and after-digest of each file the kernel sealed for commit."""
    files = getattr(write_set, "files", ())
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
        return ()
    refs: list[ArtifactRef] = []
    for item in files:
        path = getattr(item, "path", None)
        digest = getattr(item, "after_sha256", None)
        if not isinstance(path, str) or not isinstance(digest, str):
            continue
        refs.append(ArtifactRef(path=path, digest=digest))
    return tuple(refs)


@overload
def open_artifact(
    workspace: Path,
    ref: ArtifactRef | Mapping[str, object],
    *,
    model: None = None,
    loader: Literal["bytes"] = "bytes",
) -> bytes: ...


@overload
def open_artifact(
    workspace: Path,
    ref: ArtifactRef | Mapping[str, object],
    *,
    model: type[ModelT],
    loader: ArtifactLoader = "bytes",
) -> ModelT: ...


def open_artifact(
    workspace: Path,
    ref: ArtifactRef | Mapping[str, object],
    *,
    model: type[ModelT] | None = None,
    loader: ArtifactLoader = "bytes",
) -> bytes | ModelT:
    """Read one regular file inside ``workspace`` and require ``ref.digest``.

    ``model`` decodes the authenticated bytes. ``loader="json"`` (the default when
    ``model`` is set and ``loader`` stays ``"bytes"``) uses ``model_validate_json``.
    ``loader="yaml"`` uses a YAML mapping or list, then ``model_validate``.
    """
    artifact = coerce_artifact_ref(ref)
    if not is_canonical_relative(artifact.path):
        raise ArtifactReadError(
            f"artifact path must be canonical and relative: {artifact.path}",
            reason="path",
            path=artifact.path,
        )
    data = _read_regular(workspace, artifact.path)
    actual = hashlib.sha256(data).hexdigest()
    if actual != artifact.digest:
        raise ArtifactReadError(
            f"artifact digest does not match: {artifact.path}",
            reason="digest",
            path=artifact.path,
        )
    if model is None:
        if loader != "bytes":
            raise ArtifactReadError(
                "artifact loader requires a model",
                reason="decode",
                path=artifact.path,
            )
        return data
    chosen: ArtifactLoader = "json" if loader == "bytes" else loader
    try:
        if chosen == "yaml":
            loaded = yaml.safe_load(data)
            return model.model_validate(loaded)
        return model.model_validate_json(data)
    except (yaml.YAMLError, UnicodeError, ValidationError, ValueError) as error:
        raise ArtifactReadError(
            f"artifact decode failed: {artifact.path}",
            reason="decode",
            path=artifact.path,
        ) from error


def _read_regular(workspace: Path, relative: str) -> bytes:
    root = workspace.resolve()
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ArtifactReadError(
                f"artifact path contains a symlink: {relative}",
                reason="symlink",
                path=relative,
            )
    try:
        current.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as error:
        missing = isinstance(error, OSError)
        raise ArtifactReadError(
            f"artifact path escapes the workspace or is missing: {relative}",
            reason="missing" if missing else "escape",
            path=relative,
        ) from error
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(current, flags)
    except OSError as error:
        raise ArtifactReadError(
            f"artifact is missing or is not a regular file: {relative}",
            reason="missing",
            path=relative,
        ) from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ArtifactReadError(
                f"artifact is not a regular single-link file: {relative}",
                reason="irregular",
                path=relative,
            )
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    before_signature = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_signature = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if before_signature != after_signature or len(data) != before.st_size:
        raise ArtifactReadError(
            f"artifact changed while being read: {relative}",
            reason="irregular",
            path=relative,
        )
    return data


def _match_segment(pattern: str, segment: str) -> bool:
    if pattern == "*":
        return segment not in {"", ".", ".."}
    if "*" not in pattern:
        return pattern == segment
    compiled = "^" + "".join("[^/]*" if char == "*" else re.escape(char) for char in pattern) + "$"
    return re.fullmatch(compiled, segment) is not None


def _match_parts(pattern: list[str], path: list[str]) -> bool:
    if not pattern:
        return not path
    head, *rest = pattern
    if head == "**":
        if _match_parts(rest, path):
            return True
        return bool(path) and _match_parts(pattern, path[1:])
    if not path or not _match_segment(head, path[0]):
        return False
    return _match_parts(rest, path[1:])


__all__ = [
    "ArtifactReadError",
    "ArtifactRef",
    "coerce_artifact_ref",
    "is_canonical_relative",
    "match_artifact_pattern",
    "open_artifact",
    "refs_from_write_set",
]
