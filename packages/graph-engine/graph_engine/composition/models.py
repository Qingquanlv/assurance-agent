from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

from packaging.version import InvalidVersion, Version

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.identifiers import IdentifierError, validate_qualified_id


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
    distribution: str | None = None
    version: str | None = None
    entrypoint_group: str | None = None
    entrypoint_name: str | None = None
    plugin_id: str | None = None
    plugin_version: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SourceKind):
            raise TypeError("source identity kind must be a SourceKind")
        if not isinstance(self.root, Path):
            raise TypeError("source identity root must be a Path")
        if not self.root.is_absolute():
            raise ValueError("source identity root must be absolute")
        wheel_coordinates = (
            self.distribution,
            self.version,
            self.entrypoint_group,
            self.entrypoint_name,
        )
        plugin_coordinates = (self.plugin_id, self.plugin_version)
        if self.kind in {SourceKind.WHEEL_PRODUCT, SourceKind.WHEEL_PLUGIN}:
            if any(not isinstance(value, str) or not value for value in wheel_coordinates):
                raise ValueError("wheel source identity requires complete wheel coordinates")
        elif self.kind == SourceKind.EDITABLE_PLUGIN:
            # Task 2's low-level editable tree capture temporarily has no wheel
            # coordinates; Task 3 rewraps its authenticated files before exposure.
            if any(value is not None for value in wheel_coordinates) and any(
                not isinstance(value, str) or not value for value in wheel_coordinates
            ):
                raise ValueError("wheel source identity requires complete wheel coordinates")
        elif any(value is not None for value in wheel_coordinates):
            raise ValueError("wheel coordinates are allowed only for wheel source identities")
        if self.kind == SourceKind.CONFIG_TREE:
            if any(value is not None for value in plugin_coordinates):
                if any(not isinstance(value, str) or not value for value in plugin_coordinates):
                    raise ValueError("config source identity requires complete plugin coordinates")
                try:
                    validate_qualified_id(self.plugin_id or "")
                except IdentifierError as error:
                    raise ValueError("config source identity requires a qualified plugin id") from error
                try:
                    Version(self.plugin_version or "")
                except InvalidVersion as error:
                    raise ValueError("config source identity requires a valid plugin version") from error
        elif any(value is not None for value in plugin_coordinates):
            raise ValueError("plugin coordinates are allowed only for config source identities")


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
        expected = _snapshot_digest(self.identity, self.files)
        if self.digest != expected:
            raise ValueError("source snapshot digest does not authenticate its files")

    @classmethod
    def from_files(
        cls,
        kind: SourceKind,
        root: Path,
        files: tuple[SourceFile, ...],
    ) -> SourceSnapshot:
        return cls.from_identity(SourceIdentity(kind=kind, root=root), files)

    @classmethod
    def from_identity(
        cls,
        identity: SourceIdentity,
        files: tuple[SourceFile, ...],
    ) -> SourceSnapshot:
        frozen_files = tuple(sorted(files, key=lambda item: item.path))
        return cls(
            identity=identity,
            files=frozen_files,
            digest=_snapshot_digest(identity, frozen_files),
        )


def _file_digest_document(files: tuple[SourceFile, ...]) -> JSONValue:
    return [{"path": item.path, "sha256": item.sha256} for item in files]


def _snapshot_digest(identity: SourceIdentity, files: tuple[SourceFile, ...]) -> str:
    file_document = _file_digest_document(files)
    if identity.plugin_id is not None:
        return canonical_digest(
            {
                "identity": {
                    "kind": identity.kind.value,
                    "root": str(identity.root),
                    "plugin_id": identity.plugin_id,
                    "plugin_version": identity.plugin_version,
                },
                "files": file_document,
            }
        )
    if identity.distribution is None:
        return canonical_digest(file_document)
    identity_document: dict[str, JSONValue] = {
        "kind": identity.kind.value,
        "distribution": identity.distribution,
        "version": identity.version,
        "entrypoint_group": identity.entrypoint_group,
        "entrypoint_name": identity.entrypoint_name,
    }
    if identity.kind == SourceKind.EDITABLE_PLUGIN:
        identity_document["root"] = str(identity.root)
    return canonical_digest({"identity": identity_document, "files": file_document})


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
