from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import ValidationError
from pydantic_core import InitErrorDetails

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.runtime.secret_sources import (
    EMPTY_RUNTIME_AUTHORIZATION,
    EMPTY_RUNTIME_AUTHORIZATION_DIGEST,
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _validation_error(title: str, loc: tuple[str | int, ...], message: str, value: object) -> ValidationError:
    return ValidationError.from_exception_data(
        title,
        [
            InitErrorDetails(
                type="value_error",
                loc=loc,
                input=value,
                ctx={"error": ValueError(message)},
            )
        ],
    )


def _validate_sha256(value: str, *, loc: tuple[str | int, ...], title: str) -> str:
    if not _SHA256_PATTERN.fullmatch(value):
        raise _validation_error(title, loc, "invalid sha256 digest", value)
    return value


def _validate_relative_canonical_path(value: str) -> str:
    if not isinstance(value, str):
        raise _validation_error("SeedFile", ("path",), "relative canonical path", value)
    windows_path = PureWindowsPath(value)
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or value.startswith("/")
        or path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or any(segment in {"", ".", ".."} for segment in value.split("/"))
        or path.as_posix() != value
    ):
        raise _validation_error("SeedFile", ("path",), "relative canonical path", value)
    return value


def workspace_tree_id(files: dict[str, str]) -> str:
    pairs: JSONValue = [[path, files[path]] for path in sorted(files)]
    return canonical_digest(pairs)


@dataclass(frozen=True)
class SeedFile:
    path: str
    sha256: str
    content: bytes

    def __post_init__(self) -> None:
        path = _validate_relative_canonical_path(self.path)
        object.__setattr__(self, "path", path)
        computed = hashlib.sha256(self.content).hexdigest()
        _validate_sha256(computed, loc=("sha256",), title="SeedFile")
        if self.sha256 != computed:
            raise _validation_error(
                "SeedFile",
                ("sha256",),
                "sha256 does not match content",
                self.sha256,
            )
        _validate_sha256(self.sha256, loc=("sha256",), title="SeedFile")


@dataclass(frozen=True)
class WorkspaceSeed:
    schema_version: Literal["1"]
    tree_id: str
    files: tuple[SeedFile, ...]

    def __post_init__(self) -> None:
        if self.schema_version != "1":
            raise _validation_error(
                "WorkspaceSeed",
                ("schema_version",),
                "unsupported workspace seed schema version",
                self.schema_version,
            )
        paths = [item.path for item in self.files]
        if len(paths) != len(set(paths)):
            raise _validation_error(
                "WorkspaceSeed",
                ("files",),
                "duplicate workspace seed path",
                paths,
            )
        mapping = {item.path: item.sha256 for item in self.files}
        computed_tree_id = workspace_tree_id(mapping)
        _validate_sha256(computed_tree_id, loc=("tree_id",), title="WorkspaceSeed")
        if self.tree_id != computed_tree_id:
            raise _validation_error(
                "WorkspaceSeed",
                ("tree_id",),
                "tree_id does not match files",
                self.tree_id,
            )
        _validate_sha256(self.tree_id, loc=("tree_id",), title="WorkspaceSeed")


@dataclass(frozen=True)
class InvocationSeed:
    schema_version: Literal["1"]
    root_input: JSONValue
    root_input_digest: str
    workspace: WorkspaceSeed

    def __post_init__(self) -> None:
        if self.schema_version != "1":
            raise _validation_error(
                "InvocationSeed",
                ("schema_version",),
                "unsupported invocation seed schema version",
                self.schema_version,
            )
        computed_root_input_digest = canonical_digest(self.root_input)
        _validate_sha256(computed_root_input_digest, loc=("root_input_digest",), title="InvocationSeed")
        if self.root_input_digest != computed_root_input_digest:
            raise _validation_error(
                "InvocationSeed",
                ("root_input_digest",),
                "root_input_digest does not match root_input",
                self.root_input_digest,
            )
        _validate_sha256(self.root_input_digest, loc=("root_input_digest",), title="InvocationSeed")


def empty_invocation_seed(*, root_input: JSONValue | None = None) -> InvocationSeed:
    """Deterministic empty workspace and default root input for runtime tests."""

    input_value: JSONValue = {} if root_input is None else root_input
    empty_tree_id = workspace_tree_id({})
    return InvocationSeed(
        schema_version="1",
        root_input=input_value,
        root_input_digest=canonical_digest(input_value),
        workspace=WorkspaceSeed(schema_version="1", tree_id=empty_tree_id, files=()),
    )


__all__ = [
    "EMPTY_RUNTIME_AUTHORIZATION",
    "EMPTY_RUNTIME_AUTHORIZATION_DIGEST",
    "InvocationSeed",
    "SeedFile",
    "WorkspaceSeed",
    "empty_invocation_seed",
    "workspace_tree_id",
]
