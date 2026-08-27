from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, NoReturn, Self, cast

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, JsonValue, ValidationError, field_validator, model_validator
import yaml
from yaml.nodes import MappingNode, Node, SequenceNode

from graph_engine.composition.models import (
    SourceFile,
    SourceIdentity,
    SourceKind,
    SourceSnapshot,
    _validate_canonical_relative_path,
)
from graph_engine.composition.source_fs import (
    DeclaredTreePolicy,
    SourceSnapshotError,
    capture_declared_tree,
    capture_explicit_file,
)
from graph_engine.errors import GraphEngineError
from graph_engine.graph.schema import WorkflowDef
from graph_engine.identifiers import IdentifierError, validate_qualified_id
from graph_engine.plugin_api import (
    CapabilityBindingContribution,
    FrozenModel,
    PluginContribution,
    PluginContractError,
    PluginDependency,
    PluginDescriptor,
    ResourceContribution,
    SchemaContribution,
    validate_contribution,
)

if TYPE_CHECKING:
    from graph_engine.canonical import JSONValue
else:
    JSONValue = JsonValue


_PLUGIN_MANIFEST = "plugin.yaml"
_SAFE_MEDIA_SUFFIXES = {
    "application/json": (".json",),
    "application/schema+json": (".json",),
    "application/vnd.graph-engine.workflow+yaml": (".yaml", ".yml"),
    "application/yaml": (".yaml", ".yml"),
    "text/markdown": (".md",),
    "text/plain": (".txt",),
    "text/yaml": (".yaml", ".yml"),
}
_SAFE_MEDIA_TYPES = frozenset(_SAFE_MEDIA_SUFFIXES)
_YAML_MEDIA_TYPES = frozenset(
    {
        "application/vnd.graph-engine.workflow+yaml",
        "application/yaml",
        "text/yaml",
    }
)
_JSON_MEDIA_TYPES = frozenset({"application/json", "application/schema+json"})
_EXECUTABLE_KEYS = frozenset(
    {
        "callable",
        "command",
        "commands",
        "import",
        "imports",
        "install",
        "installer",
        "module",
        "python",
        "shell",
        "template",
        "templates",
    }
)


class _FrozenDict(dict[Any, Any]):
    def _deny_mutation(self, *_args: object, **_kwargs: object) -> NoReturn:
        raise TypeError("frozen JSON object cannot be mutated")

    __setitem__ = _deny_mutation
    __delitem__ = _deny_mutation
    __ior__ = _deny_mutation
    clear = _deny_mutation
    pop = _deny_mutation
    popitem = _deny_mutation
    setdefault = _deny_mutation  # pyright: ignore[reportAssignmentType]
    update = _deny_mutation  # pyright: ignore[reportAssignmentType]


class _FrozenList(list[Any]):
    def _deny_mutation(self, *_args: object, **_kwargs: object) -> NoReturn:
        raise TypeError("frozen JSON array cannot be mutated")

    __setitem__ = _deny_mutation
    __delitem__ = _deny_mutation
    __iadd__ = _deny_mutation
    __imul__ = _deny_mutation
    append = _deny_mutation
    clear = _deny_mutation
    extend = _deny_mutation
    insert = _deny_mutation
    pop = _deny_mutation
    remove = _deny_mutation
    reverse = _deny_mutation
    sort = _deny_mutation  # pyright: ignore[reportAssignmentType]


class DeclarativePluginRejected(GraphEngineError):
    """Raised when an untrusted config tree is not closed data."""


class DeclarativeProductRejected(GraphEngineError):
    """Raised when an untrusted product file is not a closed manifest."""


class ProductFileSource(FrozenModel):
    kind: Literal["product_file"] = "product_file"
    path: Path


class ConfigTreePluginSource(FrozenModel):
    kind: Literal["config_tree"] = "config_tree"
    path: Path


class DeclaredResourceFile(FrozenModel):
    kind: Literal["schema", "resource"]
    resource_id: str
    path: str
    media_type: str

    @field_validator("resource_id")
    @classmethod
    def _validate_resource_id(cls, value: str) -> str:
        return _qualified_id(value, "resource id")

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        try:
            canonical = _validate_canonical_relative_path(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"unsafe declared resource path: {value!r}") from error
        if canonical == _PLUGIN_MANIFEST:
            raise ValueError("plugin.yaml cannot declare itself as a resource")
        return canonical

    @field_validator("media_type")
    @classmethod
    def _validate_media_type(cls, value: str) -> str:
        if value not in _SAFE_MEDIA_TYPES:
            raise ValueError(f"unsafe declarative media type: {value!r}")
        return value

    @model_validator(mode="after")
    def _validate_kind_media_type(self) -> Self:
        is_schema_media = self.media_type == "application/schema+json"
        if (self.kind == "schema") != is_schema_media:
            raise ValueError("schema files must use application/schema+json exclusively")
        allowed_suffixes = _SAFE_MEDIA_SUFFIXES[self.media_type]
        if not self.path.endswith(allowed_suffixes):
            raise ValueError(
                f"resource path {self.path!r} is not registered for media type "
                f"{self.media_type!r}; executable file types and unknown suffixes "
                "are not allowed"
            )
        return self


class DeclarativeBinding(FrozenModel):
    capability_id: str
    target_capability_id: str
    data: JSONValue = None
    resource_ids: tuple[str, ...] = ()

    @field_validator("capability_id", "target_capability_id")
    @classmethod
    def _validate_capability_id(cls, value: str) -> str:
        return _qualified_id(value, "capability id")

    @field_validator("resource_ids")
    @classmethod
    def _validate_resource_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        validated = tuple(_qualified_id(value, "binding resource id") for value in values)
        if len(set(validated)) != len(validated):
            raise ValueError("binding resource ids must be unique")
        return validated


class DeclarativePluginDocument(FrozenModel):
    schema_version: Literal["1"]
    plugin_id: str
    plugin_version: str
    engine_api: str
    dependencies: tuple[PluginDependency, ...] = ()
    files: tuple[DeclaredResourceFile, ...]
    bindings: tuple[DeclarativeBinding, ...] = ()

    @field_validator("plugin_id")
    @classmethod
    def _validate_plugin_id(cls, value: str) -> str:
        return _qualified_id(value, "plugin id")

    @field_validator("plugin_version")
    @classmethod
    def _validate_plugin_version(cls, value: str) -> str:
        return _version(value, "plugin version")

    @field_validator("engine_api")
    @classmethod
    def _validate_engine_api(cls, value: str) -> str:
        return _version_or_specifier(value, "engine API")

    @field_validator("dependencies", mode="before")
    @classmethod
    def _validate_dependency_shapes(cls, values: Any) -> Any:
        if isinstance(values, (list, tuple)):
            for value in values:
                if not isinstance(value, dict) or set(value) != {
                    "plugin_id",
                    "version_specifier",
                }:
                    raise ValueError("plugin dependencies use only plugin_id and version_specifier")
        return values

    @model_validator(mode="after")
    def _validate_closed_declarations(self) -> Self:
        dependency_ids = tuple(item.plugin_id for item in self.dependencies)
        if len(set(dependency_ids)) != len(dependency_ids):
            raise ValueError("plugin dependencies must be unique")
        paths = tuple(item.path for item in self.files)
        if len(set(paths)) != len(paths):
            raise ValueError("declared resource paths must be unique")
        resource_ids = tuple(item.resource_id for item in self.files)
        if len(set(resource_ids)) != len(resource_ids):
            raise ValueError("declared resource ids must be unique")
        binding_ids = tuple(item.capability_id for item in self.bindings)
        if len(set(binding_ids)) != len(binding_ids):
            raise ValueError("declarative binding ids must be unique")
        declared_resources = set(resource_ids)
        for binding in self.bindings:
            missing = set(binding.resource_ids) - declared_resources
            if missing:
                raise ValueError(f"binding references undeclared resource id: {min(missing)}")
        return self


class DeclarativeProductRequirement(FrozenModel):
    plugin_id: str
    version_specifier: str

    @field_validator("plugin_id")
    @classmethod
    def _validate_plugin_id(cls, value: str) -> str:
        return _qualified_id(value, "plugin id")

    @field_validator("version_specifier")
    @classmethod
    def _validate_version_specifier(cls, value: str) -> str:
        return _specifier(value, "plugin version specifier")


class DeclarativeProductDocument(FrozenModel):
    schema_version: Literal["1"]
    product_id: str
    product_version: str
    engine_api: str
    plugins: tuple[DeclarativeProductRequirement, ...]
    entrypoints: dict[str, str]
    configuration: dict[str, dict[str, JSONValue]]
    config_plugin_paths: tuple[str, ...] = ()
    workflow: WorkflowDef | None = None
    workflow_resource_id: str | None = None

    @field_validator("product_id")
    @classmethod
    def _validate_product_id(cls, value: str) -> str:
        return _qualified_id(value, "product id")

    @field_validator("product_version")
    @classmethod
    def _validate_product_version(cls, value: str) -> str:
        return _version(value, "product version")

    @field_validator("engine_api")
    @classmethod
    def _validate_engine_api(cls, value: str) -> str:
        return _version_or_specifier(value, "engine API")

    @field_validator("entrypoints")
    @classmethod
    def _validate_entrypoints(cls, values: dict[str, str]) -> dict[str, str]:
        if not values:
            raise ValueError("product entrypoints must not be empty")
        if any(not name.strip() or not graph_id.strip() for name, graph_id in values.items()):
            raise ValueError("product entrypoints must map non-empty names to graph ids")
        return values

    @field_validator("configuration")
    @classmethod
    def _validate_configuration(
        cls, values: dict[str, dict[str, JSONValue]]
    ) -> dict[str, dict[str, JSONValue]]:
        for plugin_id in values:
            _qualified_id(plugin_id, "configuration plugin id")
        return values

    @field_validator("config_plugin_paths")
    @classmethod
    def _validate_config_plugin_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        validated: list[str] = []
        for value in values:
            try:
                validated.append(_validate_canonical_relative_path(value))
            except (TypeError, ValueError) as error:
                raise ValueError(f"invalid config plugin path: {value!r}") from error
        if len(set(validated)) != len(validated):
            raise ValueError("config plugin paths must be unique")
        return tuple(validated)

    @field_validator("workflow_resource_id")
    @classmethod
    def _validate_workflow_resource_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _qualified_id(value, "workflow resource id")

    @model_validator(mode="after")
    def _validate_manifest_closure(self) -> Self:
        if (self.workflow is None) == (self.workflow_resource_id is None):
            raise ValueError("product manifest requires exactly one workflow form")
        plugin_ids = tuple(requirement.plugin_id for requirement in self.plugins)
        if not plugin_ids:
            raise ValueError("product manifest must require at least one plugin")
        if len(set(plugin_ids)) != len(plugin_ids):
            raise ValueError("product plugin requirements must be unique")
        unknown_configuration = set(self.configuration) - set(plugin_ids)
        if unknown_configuration:
            raise ValueError(f"configuration targets an unrequired plugin: {min(unknown_configuration)}")
        if self.workflow is not None and self.workflow.entrypoints != self.entrypoints:
            raise ValueError("inline workflow entrypoints must equal product entrypoints")
        return self

    @property
    def required_plugin_ids(self) -> tuple[str, ...]:
        return tuple(requirement.plugin_id for requirement in self.plugins)


@dataclass(frozen=True, slots=True)
class DeclarativePlugin:
    document: DeclarativePluginDocument
    descriptor: PluginDescriptor
    contribution: PluginContribution
    snapshot: SourceSnapshot


@dataclass(frozen=True, slots=True)
class DeclarativeProduct:
    manifest: DeclarativeProductDocument
    config_plugin_paths: tuple[Path, ...]
    snapshot: SourceSnapshot


def _authenticate_frozen_config_contribution(
    snapshot: SourceSnapshot,
    descriptor: PluginDescriptor,
    contribution: PluginContribution,
) -> None:
    """Re-derive one config contribution from its frozen authenticated bytes."""

    if snapshot.identity.kind is not SourceKind.CONFIG_TREE:
        raise ValueError("declarative contribution requires a config-tree snapshot")
    document = _parse_plugin_document(_source_file(snapshot, _PLUGIN_MANIFEST).content)
    expected_descriptor, expected_contribution = _plugin_values(document, snapshot)
    if descriptor != expected_descriptor or contribution != expected_contribution:
        raise ValueError("declarative contribution disagrees with its frozen source bytes")


def load_config_tree(source: ConfigTreePluginSource) -> DeclarativePlugin:
    try:
        probe = capture_explicit_file(
            source.path,
            _PLUGIN_MANIFEST,
            DeclaredTreePolicy.config_tree(),
        )
        probe_document = _parse_plugin_document(_only_file(probe).content)
        declared_paths = tuple(file.path for file in probe_document.files)
        captured = capture_declared_tree(
            source.path,
            (_PLUGIN_MANIFEST, *declared_paths),
            DeclaredTreePolicy.config_tree(),
        )
        frozen_manifest = _source_file(captured, _PLUGIN_MANIFEST)
        if frozen_manifest.content != _only_file(probe).content:
            raise SourceSnapshotError("plugin.yaml changed between inventory and tree capture")
        document = _parse_plugin_document(frozen_manifest.content)
        if document != probe_document:
            raise SourceSnapshotError("plugin.yaml changed between inventory and tree capture")
        snapshot = SourceSnapshot.from_identity(
            SourceIdentity(
                kind=SourceKind.CONFIG_TREE,
                root=captured.identity.root,
                plugin_id=document.plugin_id,
                plugin_version=str(Version(document.plugin_version)),
            ),
            captured.files,
        )
        descriptor, contribution = _plugin_values(document, snapshot)
        return DeclarativePlugin(
            document=document,
            descriptor=descriptor,
            contribution=contribution,
            snapshot=snapshot,
        )
    except DeclarativePluginRejected:
        raise
    except (OSError, SourceSnapshotError, ValidationError, PluginContractError, ValueError) as error:
        raise DeclarativePluginRejected(str(error)) from error


def load_product_file(source: ProductFileSource) -> DeclarativeProduct:
    try:
        path = source.path.absolute()
        if path.suffix.lower() not in {".yaml", ".yml"}:
            raise ValueError("product file must be YAML data")
        captured = capture_explicit_file(
            path.parent,
            path.name,
            DeclaredTreePolicy.product_file(),
        )
        manifest = _parse_product_document(_only_file(captured).content)
        snapshot = SourceSnapshot.from_identity(
            SourceIdentity(
                kind=SourceKind.PRODUCT_FILE,
                root=captured.identity.root,
                product_id=manifest.product_id,
                product_version=manifest.product_version,
            ),
            captured.files,
        )
        resolved_paths = tuple(
            (snapshot.identity.root / PurePosixPath(relative_path)).absolute()
            for relative_path in manifest.config_plugin_paths
        )
        return DeclarativeProduct(
            manifest=manifest,
            config_plugin_paths=resolved_paths,
            snapshot=snapshot,
        )
    except DeclarativeProductRejected:
        raise
    except (OSError, SourceSnapshotError, ValidationError, PluginContractError, ValueError) as error:
        raise DeclarativeProductRejected(str(error)) from error


def _parse_plugin_document(content: bytes) -> DeclarativePluginDocument:
    try:
        raw = _safe_yaml_mapping(content, "plugin.yaml")
        document = DeclarativePluginDocument.model_validate(raw)
        return cast(DeclarativePluginDocument, _freeze_nested_values(document))
    except (ValidationError, PluginContractError, ValueError) as error:
        raise DeclarativePluginRejected(f"invalid plugin.yaml: {error}") from error


def _parse_product_document(content: bytes) -> DeclarativeProductDocument:
    try:
        raw = _safe_yaml_mapping(content, "product manifest")
        document = DeclarativeProductDocument.model_validate(raw)
        return cast(DeclarativeProductDocument, _freeze_nested_values(document))
    except (ValidationError, PluginContractError, ValueError) as error:
        raise DeclarativeProductRejected(f"invalid product manifest: {error}") from error


def _safe_yaml_mapping(content: bytes, label: str) -> dict[str, Any]:
    raw = _strict_yaml_value(content, label)
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise ValueError(f"{label} must be a string-keyed mapping")
    return cast(dict[str, Any], raw)


def _strict_yaml_value(content: bytes, label: str) -> object:
    text = _decode_utf8(content, label)
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
        if node is not None:
            _reject_duplicate_yaml_keys(node, set())
            _reject_yaml_aliases(node, set(), label)
        raw = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ValueError(f"{label} is not safe YAML") from error
    _validate_json_value(raw, label)
    _reject_executable_keys(raw, label)
    return raw


def _strict_json_value(content: bytes, label: str) -> object:
    text = _decode_utf8(content, label)

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} contains duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> NoReturn:
        raise ValueError(f"{label} contains invalid JSON constant: {value}")

    raw = json.loads(
        text,
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )
    _validate_json_value(raw, label)
    _reject_executable_keys(raw, label)
    return raw


def _decode_utf8(content: bytes, label: str) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} is not UTF-8 text") from error


def _reject_duplicate_yaml_keys(node: Node, seen: set[int]) -> None:
    identity = id(node)
    if identity in seen:
        return
    seen.add(identity)
    if isinstance(node, MappingNode):
        keys: set[tuple[str, str]] = set()
        for key, value in node.value:
            key_identity = (key.tag, getattr(key, "value", ""))
            if key_identity in keys:
                raise ValueError(f"duplicate YAML key: {key_identity[1]}")
            keys.add(key_identity)
            _reject_duplicate_yaml_keys(key, seen)
            _reject_duplicate_yaml_keys(value, seen)
    elif isinstance(node, SequenceNode):
        for child in node.value:
            _reject_duplicate_yaml_keys(child, seen)


def _reject_yaml_aliases(node: Node, seen: set[int], label: str) -> None:
    identity = id(node)
    if identity in seen:
        raise ValueError(f"{label} YAML aliases are not allowed")
    seen.add(identity)
    if isinstance(node, MappingNode):
        for key, value in node.value:
            _reject_yaml_aliases(key, seen, label)
            _reject_yaml_aliases(value, seen, label)
    elif isinstance(node, SequenceNode):
        for child in node.value:
            _reject_yaml_aliases(child, seen, label)


def _validate_json_value(
    value: object,
    label: str,
    seen: set[int] | None = None,
) -> None:
    seen = set() if seen is None else seen
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite number")
        return
    if isinstance(value, dict):
        if id(value) in seen:
            raise ValueError(f"{label} contains aliases or cycles")
        seen.add(id(value))
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{label} must use only string mapping keys")
            _validate_json_value(child, label, seen)
        return
    if isinstance(value, list):
        if id(value) in seen:
            raise ValueError(f"{label} contains aliases or cycles")
        seen.add(id(value))
        for child in value:
            _validate_json_value(child, label, seen)
        return
    raise ValueError(f"{label} contains a non-JSON data value")


def _reject_executable_keys(
    value: object,
    label: str,
    seen: set[int] | None = None,
) -> None:
    seen = set() if seen is None else seen
    if isinstance(value, dict):
        if id(value) in seen:
            raise ValueError(f"{label} YAML aliases are not allowed")
        seen.add(id(value))
        for key, child in value.items():
            if isinstance(key, str) and key.casefold() in _EXECUTABLE_KEYS:
                raise ValueError(f"{label} contains executable declaration: {key}")
            _reject_executable_keys(child, label, seen)
    elif isinstance(value, list):
        if id(value) in seen:
            raise ValueError(f"{label} YAML aliases are not allowed")
        seen.add(id(value))
        for child in value:
            _reject_executable_keys(child, label, seen)


def _freeze_nested_values(value: object) -> object:
    if isinstance(value, BaseModel):
        for field_name, field_value in value.__dict__.items():
            object.__setattr__(value, field_name, _freeze_nested_values(field_value))
        return value
    if isinstance(value, dict):
        return _FrozenDict((key, _freeze_nested_values(item)) for key, item in value.items())
    if isinstance(value, list):
        return _FrozenList(_freeze_nested_values(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_nested_values(item) for item in value)
    return value


def _plugin_values(
    document: DeclarativePluginDocument,
    snapshot: SourceSnapshot,
) -> tuple[PluginDescriptor, PluginContribution]:
    source_files = {file.path: file for file in snapshot.files}
    schemas: list[SchemaContribution] = []
    resources: list[ResourceContribution] = []
    for declared in document.files:
        source_file = source_files[declared.path]
        _validate_resource_content(declared, source_file.content)
        if declared.kind == "schema":
            schemas.append(
                SchemaContribution(
                    schema_id=declared.resource_id,
                    media_type=declared.media_type,
                    content=source_file.content,
                )
            )
        else:
            resources.append(
                ResourceContribution(
                    resource_id=declared.resource_id,
                    media_type=declared.media_type,
                    content=source_file.content,
                )
            )
    bindings = tuple(
        cast(
            CapabilityBindingContribution,
            _freeze_nested_values(
                CapabilityBindingContribution(
                    capability_id=binding.capability_id,
                    target_capability_id=binding.target_capability_id,
                    data=binding.data,
                    resource_ids=binding.resource_ids,
                )
            ),
        )
        for binding in document.bindings
    )
    descriptor = PluginDescriptor(
        schema_version="1",
        source=None,
        plugin_id=document.plugin_id,
        plugin_version=document.plugin_version,
        engine_api=document.engine_api,
        dependencies=document.dependencies,
        task_handlers=(),
        commit_validators=(),
        schemas=tuple(schema.schema_id for schema in schemas),
        resources=tuple(resource.resource_id for resource in resources),
        effects=(),
        bindings=tuple(binding.capability_id for binding in bindings),
    )
    contribution = PluginContribution(
        schemas=tuple(schemas),
        resources=tuple(resources),
        bindings=bindings,
    )
    validate_contribution(descriptor, contribution)
    return descriptor, contribution


def _validate_resource_content(declared: DeclaredResourceFile, content: bytes) -> None:
    try:
        label = f"declarative resource {declared.path!r}"
        if declared.media_type in _JSON_MEDIA_TYPES:
            _strict_json_value(content, label)
        elif declared.media_type in _YAML_MEDIA_TYPES:
            parsed = _strict_yaml_value(content, label)
            if declared.media_type == "application/vnd.graph-engine.workflow+yaml":
                WorkflowDef.model_validate(parsed)
        else:
            _decode_utf8(content, label)
    except (json.JSONDecodeError, ValidationError, ValueError, yaml.YAMLError) as error:
        raise DeclarativePluginRejected(f"invalid declarative resource: {declared.path}") from error


def _source_file(snapshot: SourceSnapshot, path: str) -> SourceFile:
    try:
        return next(file for file in snapshot.files if file.path == path)
    except StopIteration as error:
        raise SourceSnapshotError(f"snapshot is missing required file: {path}") from error


def _only_file(snapshot: SourceSnapshot) -> SourceFile:
    if len(snapshot.files) != 1:
        raise SourceSnapshotError("explicit file snapshot must contain exactly one file")
    return snapshot.files[0]


def _qualified_id(value: str, kind: str) -> str:
    try:
        return validate_qualified_id(value)
    except IdentifierError as error:
        raise ValueError(f"invalid {kind}: {value!r}") from error


def _version(value: str, kind: str) -> str:
    try:
        return str(Version(value))
    except InvalidVersion as error:
        raise ValueError(f"invalid {kind}: {value!r}") from error


def _specifier(value: str, kind: str) -> str:
    try:
        SpecifierSet(value)
    except InvalidSpecifier as error:
        raise ValueError(f"invalid {kind}: {value!r}") from error
    return value


def _version_or_specifier(value: str, kind: str) -> str:
    try:
        return str(Version(value))
    except InvalidVersion:
        return _specifier(value, kind)


__all__ = [
    "ConfigTreePluginSource",
    "DeclarativePlugin",
    "DeclarativePluginRejected",
    "DeclarativeProduct",
    "DeclarativeProductRejected",
    "ProductFileSource",
    "load_config_tree",
    "load_product_file",
]
