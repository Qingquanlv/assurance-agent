from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import NoReturn, TypeAlias, TypeVar

from packaging.version import InvalidVersion, Version

from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.frozen_json import freeze_json
from graph_engine.identifiers import IdentifierError, validate_qualified_id
from graph_engine.plugin_api import (
    CommitValidator,
    DurableEffectHandler,
    EffectPolicy,
    TaskContext,
    TaskHandler,
    TaskOutcome,
    TaskRequest,
)


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
    product_id: str | None = None
    product_version: str | None = None
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
        product_coordinates = (self.product_id, self.product_version)
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
        if self.kind == SourceKind.PRODUCT_FILE:
            if any(value is not None for value in product_coordinates):
                if any(not isinstance(value, str) or not value for value in product_coordinates):
                    raise ValueError("product source identity requires complete product coordinates")
                try:
                    validate_qualified_id(self.product_id or "")
                except IdentifierError as error:
                    raise ValueError("product source identity requires a qualified product id") from error
                try:
                    normalized_product_version = str(Version(self.product_version or ""))
                except InvalidVersion as error:
                    raise ValueError("product source identity requires a valid product version") from error
                if normalized_product_version != self.product_version:
                    raise ValueError("product source identity requires a normalized product version")
        elif any(value is not None for value in product_coordinates):
            raise ValueError("product coordinates are allowed only for product file source identities")
        if self.kind == SourceKind.CONFIG_TREE:
            if any(value is not None for value in plugin_coordinates):
                if any(not isinstance(value, str) or not value for value in plugin_coordinates):
                    raise ValueError("config source identity requires complete plugin coordinates")
                try:
                    validate_qualified_id(self.plugin_id or "")
                except IdentifierError as error:
                    raise ValueError("config source identity requires a qualified plugin id") from error
                try:
                    normalized_plugin_version = str(Version(self.plugin_version or ""))
                except InvalidVersion as error:
                    raise ValueError("config source identity requires a valid plugin version") from error
                if normalized_plugin_version != self.plugin_version:
                    raise ValueError("config source identity requires a normalized plugin version")
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


@dataclass(frozen=True, slots=True)
class SourceEntry:
    source_id: str
    snapshot: SourceSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, SourceSnapshot):
            raise TypeError("source entry snapshot must be a SourceSnapshot")
        source_id = _validate_registry_id(self.source_id, "source id")
        expected = _snapshot_registry_id(self.snapshot)
        if source_id != expected:
            raise ValueError(f"source id disagrees with snapshot identity: {source_id}; expected {expected}")


@dataclass(frozen=True, slots=True)
class TaskHandlerEntry:
    capability_id: str
    owner_id: str
    handler: TaskHandler

    def __post_init__(self) -> None:
        _validate_owned_registry_id(self.capability_id, self.owner_id, "task handler")
        if not callable(getattr(self.handler, "execute", None)):
            raise TypeError("task handler entry must provide execute")


@dataclass(frozen=True, slots=True)
class CommitValidatorEntry:
    capability_id: str
    owner_id: str
    validator: CommitValidator

    def __post_init__(self) -> None:
        _validate_owned_registry_id(self.capability_id, self.owner_id, "commit validator")
        if not callable(getattr(self.validator, "validate", None)):
            raise TypeError("commit validator entry must provide validate")


@dataclass(frozen=True, slots=True)
class _BoundTaskHandler:
    alias_id: str
    target_capability_id: str
    data: object
    resource_ids: tuple[str, ...]
    target: TaskHandler

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", freeze_json(self.data))
        object.__setattr__(self, "resource_ids", tuple(self.resource_ids))
        if not callable(getattr(self.target, "execute", None)):
            raise TypeError("bound task target must provide execute")

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        if request.capability_id != self.alias_id:
            raise ValueError(
                f"bound handler for {self.alias_id} received request for {request.capability_id}"
            )
        bound = request.model_copy(
            update={
                "target_capability_id": self.target_capability_id,
                "binding_data": self.data,
                "resource_ids": self.resource_ids,
            }
        )
        return await self.target.execute(bound, context)


@dataclass(frozen=True, slots=True)
class CapabilityBindingEntry:
    capability_id: str
    owner_id: str
    target_capability_id: str
    data: object
    resource_ids: tuple[str, ...]
    handler: TaskHandler

    def __post_init__(self) -> None:
        _validate_owned_registry_id(self.capability_id, self.owner_id, "binding")
        _validate_registry_id(self.target_capability_id, "binding target capability id")
        try:
            frozen_data = freeze_json(self.data)
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid binding data: {error}") from error
        object.__setattr__(self, "data", frozen_data)
        resource_ids = tuple(self.resource_ids)
        for resource_id in resource_ids:
            _validate_registry_id(resource_id, "binding resource id")
        if len(resource_ids) != len(set(resource_ids)):
            raise ValueError("binding resource ids must be unique")
        object.__setattr__(self, "resource_ids", resource_ids)
        if not isinstance(self.handler, _BoundTaskHandler):
            raise TypeError("binding handler must be an engine-derived bound adapter")
        if (
            self.handler.alias_id != self.capability_id
            or self.handler.target_capability_id != self.target_capability_id
            or self.handler.data != frozen_data
            or self.handler.resource_ids != resource_ids
        ):
            raise ValueError("bound adapter disagrees with binding entry")

    @classmethod
    def _from_target(
        cls,
        *,
        capability_id: str,
        owner_id: str,
        target_capability_id: str,
        data: object,
        resource_ids: tuple[str, ...],
        target: TaskHandler,
    ) -> CapabilityBindingEntry:
        handler = _BoundTaskHandler(
            alias_id=capability_id,
            target_capability_id=target_capability_id,
            data=data,
            resource_ids=resource_ids,
            target=target,
        )
        return cls(
            capability_id=capability_id,
            owner_id=owner_id,
            target_capability_id=target_capability_id,
            data=handler.data,
            resource_ids=handler.resource_ids,
            handler=handler,
        )


CapabilityEntry: TypeAlias = TaskHandlerEntry | CommitValidatorEntry | CapabilityBindingEntry
_Entry = TypeVar("_Entry")


@dataclass(frozen=True, slots=True)
class SchemaEntry:
    schema_id: str
    owner_id: str
    media_type: str
    content: bytes
    sha256: str
    dialect: str | None

    def __post_init__(self) -> None:
        _validate_owned_registry_id(self.schema_id, self.owner_id, "schema")
        if self.media_type != "application/schema+json":
            raise ValueError("schema entry media type must be application/schema+json")
        content = bytes(self.content)
        object.__setattr__(self, "content", content)
        _validate_content_digest(content, self.sha256, "schema entry")
        expected_dialect = _schema_dialect_from_content(content)
        if self.dialect != expected_dialect:
            raise ValueError(
                "schema entry dialect disagrees with content: "
                f"{self.dialect!r}; expected {expected_dialect!r}"
            )

    @classmethod
    def from_content(
        cls,
        *,
        schema_id: str,
        owner_id: str,
        media_type: str,
        content: bytes,
    ) -> SchemaEntry:
        frozen = bytes(content)
        return cls(
            schema_id=schema_id,
            owner_id=owner_id,
            media_type=media_type,
            content=frozen,
            sha256=hashlib.sha256(frozen).hexdigest(),
            dialect=_schema_dialect_from_content(frozen),
        )


def _schema_dialect_from_content(content: bytes) -> str | None:
    try:
        document = json.loads(
            content,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("schema entry content must be valid JSON") from error
    if not isinstance(document, dict | bool):
        raise ValueError("schema entry content must be a JSON object or boolean")
    if not isinstance(document, dict):
        return None
    dialect = document.get("$schema")
    if dialect is not None and not isinstance(dialect, str):
        raise ValueError("schema entry dialect must be text")
    return dialect


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number: {value}")


@dataclass(frozen=True, slots=True)
class ResourceEntry:
    resource_id: str
    owner_id: str
    media_type: str
    content: bytes
    sha256: str

    def __post_init__(self) -> None:
        _validate_owned_registry_id(self.resource_id, self.owner_id, "resource")
        if not isinstance(self.media_type, str) or not self.media_type.strip():
            raise ValueError("resource entry media type must be non-empty text")
        content = bytes(self.content)
        object.__setattr__(self, "content", content)
        _validate_content_digest(content, self.sha256, "resource entry")


@dataclass(frozen=True, slots=True)
class EffectEntry:
    kind: str
    owner_id: str
    intent_schema_id: str
    receipt_schema_id: str
    handler: DurableEffectHandler
    policy: EffectPolicy

    def __post_init__(self) -> None:
        _validate_owned_registry_id(self.kind, self.owner_id, "effect")
        _validate_registry_id(self.intent_schema_id, "effect intent schema id")
        _validate_registry_id(self.receipt_schema_id, "effect receipt schema id")
        if not callable(getattr(self.handler, "apply", None)) or not callable(
            getattr(self.handler, "reconcile", None)
        ):
            raise TypeError("effect entry handler must provide apply and reconcile")
        if not isinstance(self.policy, EffectPolicy):
            raise TypeError("effect entry policy must be an EffectPolicy")


def _validate_registry_id(value: object, kind: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{kind} must be text")
    try:
        return validate_qualified_id(value)
    except IdentifierError as error:
        raise ValueError(f"invalid {kind}: {value!r}") from error


def _snapshot_registry_id(snapshot: SourceSnapshot) -> str:
    identity = snapshot.identity
    if identity.kind == SourceKind.CONFIG_TREE:
        value = identity.plugin_id
    elif identity.kind in {SourceKind.WHEEL_PLUGIN, SourceKind.EDITABLE_PLUGIN}:
        value = identity.entrypoint_name
    elif identity.kind == SourceKind.PRODUCT_FILE:
        value = identity.product_id
    elif identity.kind == SourceKind.WHEEL_PRODUCT:
        value = identity.entrypoint_name
    elif identity.kind == SourceKind.ENGINE:
        value = "graph.engine"
    else:  # pragma: no cover - SourceKind is closed, defensive against unsafe construction.
        raise ValueError(f"source kind has no registry identity: {identity.kind!r}")
    if value is None:
        raise ValueError(f"source snapshot lacks registry identity: {identity.kind.value}")
    return _validate_registry_id(value, "snapshot registry id")


def _validate_owned_registry_id(value: object, owner_id: object, kind: str) -> None:
    entry_id = _validate_registry_id(value, f"{kind} id")
    owner = _validate_registry_id(owner_id, f"{kind} owner id")
    if not entry_id.startswith(f"{owner}."):
        raise ValueError(f"{kind} id is not owned by {owner}: {entry_id}")


def _validate_content_digest(content: bytes, sha256: object, kind: str) -> None:
    if not isinstance(sha256, str):
        raise TypeError(f"{kind} sha256 must be text")
    if hashlib.sha256(content).hexdigest() != sha256:
        raise ValueError(f"{kind} digest does not authenticate its content")


def _immutable_mapping(values: Mapping[str, _Entry]) -> Mapping[str, _Entry]:
    if not isinstance(values, Mapping):
        raise TypeError("registry entries must be a mapping")
    return MappingProxyType(dict(sorted(dict(values).items())))


def _validate_object_view(
    actual: Mapping[str, object],
    expected: Mapping[str, object],
    kind: str,
) -> None:
    if tuple(actual) != tuple(expected) or any(actual[key] is not expected[key] for key in actual):
        raise ValueError(f"{kind} view disagrees with capability entries")


@dataclass(frozen=True, slots=True)
class SourceRegistry:
    entries: Mapping[str, SourceEntry]

    def __post_init__(self) -> None:
        entries = _immutable_mapping(self.entries)
        for source_id, entry in entries.items():
            if not isinstance(entry, SourceEntry):
                raise TypeError("source registry accepts only SourceEntry values")
            if source_id != entry.source_id:
                raise ValueError(f"source registry key disagrees with entry: {source_id}")
        object.__setattr__(self, "entries", entries)


@dataclass(frozen=True, slots=True)
class CapabilityRegistry:
    entries: Mapping[str, CapabilityEntry]
    task_handlers: Mapping[str, TaskHandler]
    commit_validators: Mapping[str, CommitValidator]
    bindings: Mapping[str, CapabilityBindingEntry]

    def __post_init__(self) -> None:
        entries = _immutable_mapping(self.entries)
        task_handlers = _immutable_mapping(self.task_handlers)
        commit_validators = _immutable_mapping(self.commit_validators)
        bindings = _immutable_mapping(self.bindings)

        expected_handlers: dict[str, TaskHandler] = {}
        expected_validators: dict[str, CommitValidator] = {}
        expected_bindings: dict[str, CapabilityBindingEntry] = {}
        for capability_id, entry in entries.items():
            if not isinstance(
                entry,
                TaskHandlerEntry | CommitValidatorEntry | CapabilityBindingEntry,
            ):
                raise TypeError("capability registry accepts only capability entry values")
            if capability_id != entry.capability_id:
                raise ValueError(f"capability registry key disagrees with entry: {capability_id}")
            if isinstance(entry, TaskHandlerEntry):
                expected_handlers[capability_id] = entry.handler
            elif isinstance(entry, CommitValidatorEntry):
                expected_validators[capability_id] = entry.validator
            else:
                expected_handlers[capability_id] = entry.handler
                expected_bindings[capability_id] = entry

        _validate_object_view(task_handlers, expected_handlers, "task handler")
        _validate_object_view(commit_validators, expected_validators, "commit validator")
        if bindings != expected_bindings:
            raise ValueError("binding view disagrees with capability entries")
        for binding in expected_bindings.values():
            target = entries.get(binding.target_capability_id)
            if not isinstance(target, TaskHandlerEntry):
                raise ValueError(
                    f"binding target must select a direct task handler: {binding.target_capability_id}"
                )
            if not isinstance(binding.handler, _BoundTaskHandler) or (
                binding.handler.target is not target.handler
            ):
                raise ValueError(
                    f"bound adapter target disagrees with direct task handler: {binding.target_capability_id}"
                )

        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "task_handlers", task_handlers)
        object.__setattr__(self, "commit_validators", commit_validators)
        object.__setattr__(self, "bindings", bindings)

    @classmethod
    def empty(cls) -> CapabilityRegistry:
        return cls(entries={}, task_handlers={}, commit_validators={}, bindings={})


@dataclass(frozen=True, slots=True)
class SchemaRegistry:
    entries: Mapping[str, SchemaEntry]

    def __post_init__(self) -> None:
        entries = _immutable_mapping(self.entries)
        for schema_id, entry in entries.items():
            if not isinstance(entry, SchemaEntry):
                raise TypeError("schema registry accepts only SchemaEntry values")
            if schema_id != entry.schema_id:
                raise ValueError(f"schema registry key disagrees with entry: {schema_id}")
        object.__setattr__(self, "entries", entries)


@dataclass(frozen=True, slots=True)
class ResourceRegistry:
    entries: Mapping[str, ResourceEntry]

    def __post_init__(self) -> None:
        entries = _immutable_mapping(self.entries)
        for resource_id, entry in entries.items():
            if not isinstance(entry, ResourceEntry):
                raise TypeError("resource registry accepts only ResourceEntry values")
            if resource_id != entry.resource_id:
                raise ValueError(f"resource registry key disagrees with entry: {resource_id}")
        object.__setattr__(self, "entries", entries)


@dataclass(frozen=True, slots=True)
class EffectRegistry:
    entries: Mapping[str, EffectEntry]

    def __post_init__(self) -> None:
        entries = _immutable_mapping(self.entries)
        for kind, entry in entries.items():
            if not isinstance(entry, EffectEntry):
                raise TypeError("effect registry accepts only EffectEntry values")
            if kind != entry.kind:
                raise ValueError(f"effect registry key disagrees with entry: {kind}")
        object.__setattr__(self, "entries", entries)

    def require(self, kind: str) -> EffectEntry:
        return self.entries[kind]


@dataclass(frozen=True, slots=True)
class RegistrySet:
    sources: SourceRegistry
    capabilities: CapabilityRegistry
    schemas: SchemaRegistry
    resources: ResourceRegistry
    effects: EffectRegistry

    def __post_init__(self) -> None:
        expected_types = (
            ("sources", self.sources, SourceRegistry),
            ("capabilities", self.capabilities, CapabilityRegistry),
            ("schemas", self.schemas, SchemaRegistry),
            ("resources", self.resources, ResourceRegistry),
            ("effects", self.effects, EffectRegistry),
        )
        for name, registry, expected in expected_types:
            if not isinstance(registry, expected):
                raise TypeError(f"registry set {name} must be a {expected.__name__}")

        kinds: tuple[tuple[str, Mapping[str, object]], ...] = (
            ("source", self.sources.entries),
            ("capability", self.capabilities.entries),
            ("schema", self.schemas.entries),
            ("resource", self.resources.entries),
            ("effect", self.effects.entries),
        )
        ownership: dict[str, str] = {}
        for kind, entries in kinds:
            for entry_id in entries:
                previous = ownership.get(entry_id)
                if previous is not None:
                    raise ValueError(f"cross-kind registry id: {entry_id} is both {previous} and {kind}")
                ownership[entry_id] = kind

        owned_entries = (
            *self.capabilities.entries.values(),
            *self.schemas.entries.values(),
            *self.resources.entries.values(),
            *self.effects.entries.values(),
        )
        for entry in owned_entries:
            source = self.sources.entries.get(entry.owner_id)
            if source is None:
                raise ValueError(f"registry entry owner has no selected source: {entry.owner_id}")
            source_kind = source.snapshot.identity.kind
            if source_kind not in {
                SourceKind.WHEEL_PLUGIN,
                SourceKind.EDITABLE_PLUGIN,
                SourceKind.CONFIG_TREE,
            }:
                raise ValueError(f"registry entry owner is not a plugin source: {entry.owner_id}")
            if source_kind == SourceKind.CONFIG_TREE and isinstance(
                entry,
                TaskHandlerEntry | CommitValidatorEntry | EffectEntry,
            ):
                raise ValueError(f"config source cannot own executable registry entry: {entry.owner_id}")
        for binding in self.capabilities.bindings.values():
            for resource_id in binding.resource_ids:
                if resource_id not in self.resources.entries:
                    raise ValueError(f"binding resource is not registered: {resource_id}")
        for effect in self.effects.entries.values():
            if effect.intent_schema_id not in self.schemas.entries:
                raise ValueError(f"effect intent schema is not registered: {effect.intent_schema_id}")
            if effect.receipt_schema_id not in self.schemas.entries:
                raise ValueError(f"effect receipt schema is not registered: {effect.receipt_schema_id}")


def _file_digest_document(files: tuple[SourceFile, ...]) -> JSONValue:
    return [{"path": item.path, "sha256": item.sha256} for item in files]


def _snapshot_digest(identity: SourceIdentity, files: tuple[SourceFile, ...]) -> str:
    file_document = _file_digest_document(files)
    if identity.product_id is not None:
        return canonical_digest(
            {
                "identity": {
                    "kind": identity.kind.value,
                    "root": str(identity.root),
                    "product_id": identity.product_id,
                    "product_version": identity.product_version,
                },
                "files": file_document,
            }
        )
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


__all__ = [
    "CapabilityBindingEntry",
    "CapabilityEntry",
    "CapabilityRegistry",
    "CommitValidatorEntry",
    "EffectEntry",
    "EffectRegistry",
    "RegistrySet",
    "ResourceEntry",
    "ResourceRegistry",
    "SchemaEntry",
    "SchemaRegistry",
    "SourceEntry",
    "SourceFile",
    "SourceIdentity",
    "SourceKind",
    "SourceRegistry",
    "SourceSnapshot",
    "TaskHandlerEntry",
]
