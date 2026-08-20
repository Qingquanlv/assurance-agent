from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

from graph_engine.canonical import JSONValue, canonical_digest


class SourceKind(str, Enum):
    """Closed source categories understood by the composition platform."""

    WHEEL_PRODUCT = "wheel_product"
    WHEEL_PLUGIN = "wheel_plugin"
    EDITABLE_PLUGIN = "editable_plugin"
    PRODUCT_FILE = "product_file"
    CONFIG_TREE = "config_tree"
    ENGINE = "engine"


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    kind: SourceKind
    root: Path

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SourceKind):
            raise TypeError("source identity kind must be a SourceKind")
        if not isinstance(self.root, Path):
            raise TypeError("source identity root must be a Path")
        if not self.root.is_absolute():
            raise ValueError("source identity root must be absolute")


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: str
    sha256: str
    content: bytes

    def __post_init__(self) -> None:
        _validate_canonical_relative_path(self.path)
        if not isinstance(self.content, bytes):
            raise TypeError("source file content must be immutable bytes")
        expected = hashlib.sha256(self.content).hexdigest()
        if self.sha256 != expected:
            raise ValueError("source file digest does not authenticate its content")

    @classmethod
    def from_bytes(cls, path: str, content: bytes) -> SourceFile:
        frozen = bytes(content)
        return cls(path=path, sha256=hashlib.sha256(frozen).hexdigest(), content=frozen)


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    identity: SourceIdentity
    files: tuple[SourceFile, ...]
    digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, SourceIdentity):
            raise TypeError("source snapshot identity must be a SourceIdentity")
        if not isinstance(self.files, tuple):
            raise TypeError("source snapshot files must be an immutable tuple")
        if any(not isinstance(item, SourceFile) for item in self.files):
            raise TypeError("source snapshot files must contain only SourceFile values")
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("source snapshot files must have unique canonical path order")
        expected = canonical_digest(_file_digest_document(self.files))
        if self.digest != expected:
            raise ValueError("source snapshot digest does not authenticate its files")

    @classmethod
    def from_files(
        cls,
        kind: SourceKind,
        root: Path,
        files: tuple[SourceFile, ...],
    ) -> SourceSnapshot:
        frozen_files = tuple(sorted(files, key=lambda item: item.path))
        return cls(
            identity=SourceIdentity(kind=kind, root=root),
            files=frozen_files,
            digest=canonical_digest(_file_digest_document(frozen_files)),
        )


def _file_digest_document(files: tuple[SourceFile, ...]) -> JSONValue:
    return [{"path": item.path, "sha256": item.sha256} for item in files]


def _validate_canonical_relative_path(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("source file path must be text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("source file path must be a canonical relative path") from error
    path = PurePosixPath(value)
    parts = value.split("/")
    if (
        not value
        or "\0" in value
        or "\\" in value
        or path.is_absolute()
        or parts != list(path.parts)
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError("source file path must be a canonical relative path")
    return value


__all__ = ["SourceFile", "SourceIdentity", "SourceKind", "SourceSnapshot"]
