from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field as dataclass_field
from enum import Enum
from pathlib import Path, PurePosixPath
from types import FunctionType, MappingProxyType
from typing import TYPE_CHECKING, Literal, NoReturn, TypeAlias, TypeVar, cast

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import (
    BaseModel,
    Field,
    SerializerFunctionWrapHandler,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)

from graph_engine.attempts.contracts import TaskAttemptContract
from graph_engine.canonical import JSONValue, canonical_digest
from graph_engine.frozen_json import FrozenJSONValue, freeze_json, thaw_json
from graph_engine.graph.module_schema import WorkflowModuleDef
from graph_engine.graph.schema import WorkflowDef
from graph_engine.identifiers import IdentifierError, validate_qualified_id
from graph_engine.json_schema import assert_closed_json_schema
from graph_engine.composition.provenance import StandardLoader
from graph_engine.plugin_api import (
    AttemptContractRef,
    CommitValidator,
    DurableEffectHandler,
    EffectPolicy,
    FrozenModel,
    PluginDescriptor,
    PluginContribution,
    ProviderSource,
    TaskContext,
    TaskHandler,
    TaskOutcome,
    TaskRequest,
    validate_contribution,
    validate_provider_import_roots,
)

if TYPE_CHECKING:
    from graph_engine.composition.lock import InvocationLock, ProductLock
    from graph_engine.graph.compiler import CompiledWorkflow


class SourceKind(str, Enum):
    """Closed source categories understood by the composition platform."""

    WHEEL_PRODUCT = "wheel_product"
    WHEEL_PLUGIN = "wheel_plugin"
    EDITABLE_PRODUCT = "editable_product"
    EDITABLE_PLUGIN = "editable_plugin"
    PRODUCT_FILE = "product_file"
    CONFIG_TREE = "config_tree"
    ENGINE = "engine"


class SourceRole(str, Enum):
    ENGINE = "engine"
    PRODUCT = "product"
    PLUGIN = "plugin"
    CONFIG = "config"


@dataclass(frozen=True, slots=True)
class SourceKey:
    role: SourceRole
    owner_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, SourceRole):
            raise TypeError("source key role must be a SourceRole")
        _validate_registry_id(self.owner_id, "source owner id")


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    kind: SourceKind
    root: Path
    distribution: str | None = None
    version: str | None = None
    entrypoint_group: str | None = None
    entrypoint_name: str | None = None
    entrypoint_value: str | None = None
    declaration_path: str | None = None
    import_roots: tuple[str, ...] | None = None
    product_id: str | None = None
    product_version: str | None = None
    plugin_id: str | None = None
    plugin_version: str | None = None
    engine_installation: Literal["installed", "editable"] | None = None

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
            self.entrypoint_value,
            self.declaration_path,
        )
        product_coordinates = (self.product_id, self.product_version)
        plugin_coordinates = (self.plugin_id, self.plugin_version)
        if self.kind in {SourceKind.WHEEL_PRODUCT, SourceKind.WHEEL_PLUGIN}:
            if any(not isinstance(value, str) or not value for value in wheel_coordinates):
                raise ValueError("wheel source identity requires complete wheel coordinates")
        elif self.kind in {SourceKind.EDITABLE_PRODUCT, SourceKind.EDITABLE_PLUGIN}:
            # Task 2's low-level editable tree capture temporarily has no wheel
            # coordinates; Task 3 rewraps its authenticated files before exposure.
            if any(value is not None for value in wheel_coordinates) and any(
                not isinstance(value, str) or not value for value in wheel_coordinates
            ):
                raise ValueError("wheel source identity requires complete wheel coordinates")
        elif self.kind == SourceKind.ENGINE:
            has_engine_coordinates = any(
                value is not None for value in (self.distribution, self.version, self.engine_installation)
            )
            if has_engine_coordinates and (
                not isinstance(self.distribution, str)
                or not self.distribution
                or not isinstance(self.version, str)
                or not self.version
                or self.engine_installation not in {"installed", "editable"}
            ):
                raise ValueError("engine source identity requires complete installation coordinates")
            if any(
                value is not None
                for value in (
                    self.entrypoint_group,
                    self.entrypoint_name,
                    self.entrypoint_value,
                    self.declaration_path,
                )
            ):
                raise ValueError("engine source identity does not accept entrypoint coordinates")
        elif any(value is not None for value in wheel_coordinates):
            raise ValueError("wheel coordinates are allowed only for wheel source identities")
        wheel_kinds = {
            SourceKind.WHEEL_PRODUCT,
            SourceKind.WHEEL_PLUGIN,
            SourceKind.EDITABLE_PRODUCT,
            SourceKind.EDITABLE_PLUGIN,
        }
        complete_wheel_identity = self.kind in {
            SourceKind.WHEEL_PRODUCT,
            SourceKind.WHEEL_PLUGIN,
        } or (
            self.kind in {SourceKind.EDITABLE_PRODUCT, SourceKind.EDITABLE_PLUGIN}
            and any(value is not None for value in wheel_coordinates)
        )
        if complete_wheel_identity and not self.import_roots:
            raise ValueError("wheel source identity requires authenticated import roots")
        if self.kind not in wheel_kinds and self.import_roots is not None:
            raise ValueError("import roots are allowed only for wheel source identities")
        if self.import_roots is not None:
            validate_provider_import_roots(self.import_roots)
        if self.kind != SourceKind.ENGINE and self.engine_installation is not None:
            raise ValueError("engine installation is allowed only for engine source identities")
        if self.kind in {
            SourceKind.PRODUCT_FILE,
            SourceKind.WHEEL_PRODUCT,
            SourceKind.EDITABLE_PRODUCT,
        }:
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
        if self.kind in {SourceKind.CONFIG_TREE, SourceKind.WHEEL_PLUGIN, SourceKind.EDITABLE_PLUGIN}:
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
    source_key: SourceKey
    snapshot: SourceSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, SourceSnapshot):
            raise TypeError("source entry snapshot must be a SourceSnapshot")
        if not isinstance(self.source_key, SourceKey):
            raise TypeError("source entry key must be a SourceKey")
        expected = _snapshot_source_key(self.snapshot)
        if self.source_key != expected:
            raise ValueError(
                f"source key disagrees with snapshot identity: {self.source_key}; expected {expected}"
            )


class ExecutableKind(str, Enum):
    TASK_HANDLER = "task_handler"
    COMMIT_VALIDATOR = "commit_validator"
    EFFECT_APPLY = "effect_apply"
    EFFECT_RECONCILE = "effect_reconcile"

    @property
    def slot(self) -> str:
        return {
            ExecutableKind.TASK_HANDLER: "execute",
            ExecutableKind.COMMIT_VALIDATOR: "validate",
            ExecutableKind.EFFECT_APPLY: "apply",
            ExecutableKind.EFFECT_RECONCILE: "reconcile",
        }[self]


class ExecutableBindingMode(str, Enum):
    INSTANCE_METHOD = "instance_method"
    CLASS_METHOD = "class_method"
    STATIC_METHOD = "static_method"
    MODULE_FUNCTION = "module_function"


@dataclass(frozen=True, slots=True)
class ExecutableModuleProvenance:
    """Stable, source-relative projection of the defining module's import proof."""

    module_name: str
    standard_loader: StandardLoader
    standard_is_package: bool
    relative_origin: str
    authenticated_locations: tuple[str, ...]
    physical_sha256: str
    source_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.module_name, str) or not self.module_name:
            raise TypeError("executable module name must be non-empty text")
        if not isinstance(self.standard_loader, StandardLoader):
            raise TypeError("executable module loader must be a StandardLoader")
        _validate_canonical_relative_path(self.relative_origin)
        locations = tuple(self.authenticated_locations)
        for location in locations:
            _validate_canonical_relative_path(location)
        if locations != tuple(sorted(set(locations))):
            raise ValueError("executable module locations must have unique canonical order")
        object.__setattr__(self, "authenticated_locations", locations)
        _validate_sha256(self.physical_sha256, "executable physical digest")
        _validate_sha256(self.source_digest, "executable source digest")

    def projection(self) -> JSONValue:
        return {
            "module_name": self.module_name,
            "standard_loader": self.standard_loader.value,
            "standard_is_package": self.standard_is_package,
            "relative_origin": self.relative_origin,
            "authenticated_locations": list(self.authenticated_locations),
            "physical_sha256": self.physical_sha256,
            "source_digest": self.source_digest,
        }


@dataclass(frozen=True, slots=True)
class ExecutableProvenance:
    kind: ExecutableKind
    registry_id: str
    owner_id: str
    source_key: SourceKey
    source_digest: str
    module: ExecutableModuleProvenance
    module_digest: str
    slot: str
    callable_path: str
    binding_mode: ExecutableBindingMode
    digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ExecutableKind):
            raise TypeError("executable provenance kind must be an ExecutableKind")
        _validate_owned_registry_id(self.registry_id, self.owner_id, "executable")
        if self.source_key != SourceKey(SourceRole.PLUGIN, self.owner_id):
            raise ValueError("executable provenance must name its owning selected plugin source")
        if self.module.source_digest != self.source_digest:
            raise ValueError("executable module provenance disagrees with its source digest")
        if self.module_digest != canonical_digest(self.module.projection()):
            raise ValueError("executable module digest does not authenticate its provenance")
        if self.slot != self.kind.slot:
            raise ValueError("executable slot disagrees with its kind")
        if not isinstance(self.callable_path, str) or not self.callable_path:
            raise ValueError("executable callable path must be non-empty text")
        if not isinstance(self.binding_mode, ExecutableBindingMode):
            raise TypeError("executable binding mode must be an ExecutableBindingMode")
        _validate_sha256(self.source_digest, "executable source digest")
        expected = canonical_digest(self.projection(include_digest=False))
        if self.digest != expected:
            raise ValueError("executable provenance digest does not authenticate its projection")

    @classmethod
    def create(
        cls,
        *,
        kind: ExecutableKind,
        registry_id: str,
        owner_id: str,
        source_key: SourceKey,
        source_digest: str,
        module: ExecutableModuleProvenance,
        callable_path: str,
        binding_mode: ExecutableBindingMode,
    ) -> ExecutableProvenance:
        module_digest = canonical_digest(module.projection())
        projection: JSONValue = {
            "kind": kind.value,
            "registry_id": registry_id,
            "owner_id": owner_id,
            "source_key": {"role": source_key.role.value, "owner_id": source_key.owner_id},
            "source_digest": source_digest,
            "module": module.projection(),
            "module_digest": module_digest,
            "slot": kind.slot,
            "callable_path": callable_path,
            "binding_mode": binding_mode.value,
        }
        return cls(
            kind=kind,
            registry_id=registry_id,
            owner_id=owner_id,
            source_key=source_key,
            source_digest=source_digest,
            module=module,
            module_digest=module_digest,
            slot=kind.slot,
            callable_path=callable_path,
            binding_mode=binding_mode,
            digest=canonical_digest(projection),
        )

    def projection(self, *, include_digest: bool = True) -> JSONValue:
        projection: dict[str, JSONValue] = {
            "kind": self.kind.value,
            "registry_id": self.registry_id,
            "owner_id": self.owner_id,
            "source_key": {
                "role": self.source_key.role.value,
                "owner_id": self.source_key.owner_id,
            },
            "source_digest": self.source_digest,
            "module": self.module.projection(),
            "module_digest": self.module_digest,
            "slot": self.slot,
            "callable_path": self.callable_path,
            "binding_mode": self.binding_mode.value,
        }
        if include_digest:
            projection["digest"] = self.digest
        return projection


ExecutableKey: TypeAlias = tuple[ExecutableKind, str]


def _descriptor_executable_keys(descriptor: PluginDescriptor) -> tuple[ExecutableKey, ...]:
    keys = (
        *((ExecutableKind.TASK_HANDLER, entry_id) for entry_id in descriptor.task_handlers),
        *((ExecutableKind.COMMIT_VALIDATOR, entry_id) for entry_id in descriptor.commit_validators),
        *((ExecutableKind.EFFECT_APPLY, entry_id) for entry_id in descriptor.effects),
        *((ExecutableKind.EFFECT_RECONCILE, entry_id) for entry_id in descriptor.effects),
    )
    ordered = tuple(sorted(keys, key=lambda item: (item[1], item[0].value)))
    if len(ordered) != len(set(ordered)):
        raise ValueError("plugin descriptor executable declarations must be unique")
    return ordered


def _contribution_executable_objects(
    contribution: PluginContribution,
) -> Mapping[ExecutableKey, object]:
    values: dict[ExecutableKey, object] = {
        **{
            (ExecutableKind.TASK_HANDLER, entry_id): executable
            for entry_id, executable in contribution.task_handlers.items()
        },
        **{
            (ExecutableKind.COMMIT_VALIDATOR, entry_id): executable
            for entry_id, executable in contribution.commit_validators.items()
        },
    }
    for registration in contribution.effects:
        values[(ExecutableKind.EFFECT_APPLY, registration.kind)] = registration.handler
        values[(ExecutableKind.EFFECT_RECONCILE, registration.kind)] = registration.handler
    return MappingProxyType(values)


@dataclass(frozen=True, slots=True)
class ExecutableAuthority:
    executable: object
    function: FunctionType
    bound_self: object | None
    descriptor: object
    provenance: ExecutableProvenance

    def __post_init__(self) -> None:
        if type(self.function) is not FunctionType:
            raise TypeError("executable authority requires an exact Python function")
        if not isinstance(self.provenance, ExecutableProvenance):
            raise TypeError("executable authority requires typed provenance")


@dataclass(frozen=True, slots=True, eq=False)
class ContributionAuthority:
    """One immutable six-category contribution generation for a selected plugin."""

    provider_binding: object | None
    descriptor: PluginDescriptor
    owner_id: str
    source_key: SourceKey
    source_digest: str
    contribution: PluginContribution
    authorities: tuple[ExecutableAuthority, ...]
    attempt_contracts: tuple[AttemptContractRef, ...] = ()

    def __post_init__(self) -> None:
        if type(self.descriptor) is not PluginDescriptor:
            raise TypeError("contribution authority requires an exact static PluginDescriptor")
        if self.descriptor.plugin_id != self.owner_id:
            raise ValueError("contribution authority owner disagrees with its descriptor")
        if self.source_key.owner_id != self.owner_id or self.source_key.role not in {
            SourceRole.PLUGIN,
            SourceRole.CONFIG,
        }:
            raise ValueError("contribution authority source key disagrees with its owner")
        _validate_sha256(self.source_digest, "contribution authority source digest")
        if type(self.contribution) is not PluginContribution:
            raise TypeError("contribution authority requires an exact PluginContribution")
        validate_contribution(self.descriptor, self.contribution)
        values = tuple(self.authorities)
        if any(not isinstance(item, ExecutableAuthority) for item in values):
            raise TypeError("contribution authority executable proofs are closed")
        keys = tuple((item.provenance.kind, item.provenance.registry_id) for item in values)
        expected = _descriptor_executable_keys(self.descriptor)
        if keys != expected:
            raise ValueError("contribution authority disagrees with declared executable keys")
        contribution_objects = _contribution_executable_objects(self.contribution)
        if tuple(sorted(contribution_objects, key=lambda item: (item[1], item[0].value))) != expected:
            raise ValueError("raw contribution disagrees with declared executable keys")
        for item in values:
            key = (item.provenance.kind, item.provenance.registry_id)
            if item.executable is not contribution_objects[key]:
                raise ValueError("executable authority object is not the contributed object")
            if (
                item.provenance.owner_id != self.owner_id
                or item.provenance.source_key != self.source_key
                or item.provenance.source_digest != self.source_digest
            ):
                raise ValueError("executable authority provenance disagrees with its selected source")
        if self.source_key.role is SourceRole.CONFIG:
            if values or self.provider_binding is not None:
                raise ValueError("config contribution cannot retain executable authority")
            if self.attempt_contracts:
                raise ValueError("configuration-tree contributions cannot declare attempt contracts")
        elif self.provider_binding is None:
            raise ValueError("wheel contribution requires an owning provider binding")
        contracts = tuple(self.attempt_contracts) or tuple(self.contribution.attempt_contracts)
        if any(not isinstance(item, AttemptContractRef) for item in contracts):
            raise TypeError("attempt contracts must contain AttemptContractRef values")
        contract_ids = tuple(item.contract_id for item in contracts)
        if contract_ids != tuple(sorted(contract_ids)) or len(contract_ids) != len(set(contract_ids)):
            raise ValueError("attempt contracts require unique canonical order")
        for item in contracts:
            if not item.contract_id.startswith(f"{self.owner_id}."):
                raise ValueError(f"attempt contract id is not owned by {self.owner_id}: {item.contract_id}")
        object.__setattr__(self, "authorities", values)
        object.__setattr__(self, "attempt_contracts", contracts)

    @property
    def projection(self) -> FrozenJSONValue:
        from graph_engine.composition.contributions import contribution_authority_projection

        return freeze_json(contribution_authority_projection(self))

    @property
    def digest(self) -> str:
        return canonical_digest(cast(JSONValue, thaw_json(self.projection)))

    @property
    def keys(self) -> tuple[ExecutableKey, ...]:
        return tuple((item.provenance.kind, item.provenance.registry_id) for item in self.authorities)

    def authority(self, kind: ExecutableKind, registry_id: str) -> ExecutableAuthority:
        try:
            return next(
                item
                for item in self.authorities
                if item.provenance.kind is kind and item.provenance.registry_id == registry_id
            )
        except StopIteration as error:  # pragma: no cover - constructor proves completeness.
            raise KeyError((kind, registry_id)) from error


@dataclass(frozen=True, slots=True)
class AuthenticatedContribution:
    """Frozen contribution paired with exact executable implementation proofs."""

    owner_id: str
    source_key: SourceKey
    source_digest: str
    descriptor: PluginDescriptor
    contribution: PluginContribution
    executables: tuple[ExecutableProvenance, ...]
    authority: ContributionAuthority

    def __post_init__(self) -> None:
        _validate_registry_id(self.owner_id, "authenticated contribution owner id")
        if self.source_key.owner_id != self.owner_id or self.source_key.role not in {
            SourceRole.PLUGIN,
            SourceRole.CONFIG,
        }:
            raise ValueError("authenticated contribution source key disagrees with its owner")
        _validate_sha256(self.source_digest, "authenticated contribution source digest")
        if type(self.contribution) is not PluginContribution:
            raise TypeError("authenticated contribution requires an exact PluginContribution")
        if not isinstance(self.authority, ContributionAuthority):
            raise TypeError("authenticated contribution requires a contribution authority generation")
        if (
            self.authority.descriptor != self.descriptor
            or self.authority.owner_id != self.owner_id
            or self.authority.source_key != self.source_key
            or self.authority.source_digest != self.source_digest
            or self.authority.contribution is not self.contribution
        ):
            raise ValueError("authenticated contribution disagrees with its authority generation")
        values = tuple(self.executables)
        if any(not isinstance(item, ExecutableProvenance) for item in values):
            raise TypeError("authenticated contribution executable proofs are closed")
        keys = tuple((item.kind, item.registry_id) for item in values)
        if keys != tuple(sorted(keys, key=lambda item: (item[1], item[0].value))):
            raise ValueError("authenticated executable proofs require canonical order")
        if len(keys) != len(set(keys)):
            raise ValueError("authenticated executable proofs must be unique")
        expected = _descriptor_executable_keys(self.descriptor)
        if keys != expected or keys != self.authority.keys:
            raise ValueError("authenticated executable proofs are incomplete or contain extras")
        for item in values:
            if (
                item.owner_id != self.owner_id
                or item.source_key != self.source_key
                or item.source_digest != self.source_digest
            ):
                raise ValueError("authenticated executable proof disagrees with contribution source")
        if self.source_key.role is SourceRole.CONFIG and values:
            raise ValueError("config contribution cannot retain executable provenance")
        object.__setattr__(self, "executables", values)

    def executable(self, kind: ExecutableKind, registry_id: str) -> ExecutableProvenance:
        try:
            return next(
                item for item in self.executables if item.kind is kind and item.registry_id == registry_id
            )
        except StopIteration as error:  # pragma: no cover - constructor proves completeness.
            raise KeyError((kind, registry_id)) from error


@dataclass(frozen=True, slots=True)
class TaskHandlerEntry:
    capability_id: str
    owner_id: str
    handler: TaskHandler
    provenance: ExecutableProvenance
    authority: ContributionAuthority = dataclass_field(compare=False, repr=False)

    def __post_init__(self) -> None:
        _validate_owned_registry_id(self.capability_id, self.owner_id, "task handler")
        if not callable(getattr(self.handler, "execute", None)):
            raise TypeError("task handler entry must provide execute")
        _validate_entry_provenance(
            self.provenance,
            ExecutableKind.TASK_HANDLER,
            self.capability_id,
            self.owner_id,
        )
        _validate_entry_authority(
            self.authority,
            self.handler,
            self.provenance,
        )


@dataclass(frozen=True, slots=True)
class CommitValidatorEntry:
    capability_id: str
    owner_id: str
    validator: CommitValidator
    provenance: ExecutableProvenance
    authority: ContributionAuthority = dataclass_field(compare=False, repr=False)

    def __post_init__(self) -> None:
        _validate_owned_registry_id(self.capability_id, self.owner_id, "commit validator")
        if not callable(getattr(self.validator, "validate", None)):
            raise TypeError("commit validator entry must provide validate")
        _validate_entry_provenance(
            self.provenance,
            ExecutableKind.COMMIT_VALIDATOR,
            self.capability_id,
            self.owner_id,
        )
        _validate_entry_authority(
            self.authority,
            self.validator,
            self.provenance,
        )


@dataclass(frozen=True, slots=True)
class _BoundTaskHandler:
    alias_id: str
    target_capability_id: str
    data: object
    resource_ids: tuple[str, ...]
    target: TaskHandler
    secret_handles: tuple[str, ...] = ()
    contract_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", freeze_json(self.data))
        object.__setattr__(self, "resource_ids", tuple(self.resource_ids))
        object.__setattr__(self, "secret_handles", tuple(sorted(self.secret_handles)))
        if not callable(getattr(self.target, "execute", None)):
            raise TypeError("bound task target must provide execute")

    async def execute(self, request: TaskRequest, context: TaskContext) -> TaskOutcome:
        if request.capability_id != self.alias_id:
            raise ValueError(
                f"bound handler for {self.alias_id} received request for {request.capability_id}"
            )
        expected_ids = tuple(sorted(self.resource_ids))
        if request.resource_ids != expected_ids:
            raise ValueError(
                f"bound handler resource ids disagree with scheduler projection: {self.alias_id}"
            )
        digests = thaw_json(request.resource_digests)
        if not isinstance(digests, dict) or set(digests) != set(expected_ids):
            raise ValueError(
                f"bound handler resource digests disagree with scheduler projection: {self.alias_id}"
            )
        bound = request.model_copy(
            update={
                "target_capability_id": self.target_capability_id,
                "binding_data": self.data,
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
    target_provenance: ExecutableProvenance
    secret_handles: tuple[str, ...] = ()
    contract_id: str | None = None

    def __post_init__(self) -> None:
        _validate_owned_registry_id(self.capability_id, self.owner_id, "binding")
        _validate_registry_id(self.target_capability_id, "binding target capability id")
        if self.contract_id is not None:
            _validate_registry_id(self.contract_id, "binding contract id")
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
        secret_handles = tuple(self.secret_handles)
        for handle in secret_handles:
            _validate_registry_id(handle, "binding secret handle")
        if len(secret_handles) != len(set(secret_handles)):
            raise ValueError("binding secret handles must be unique")
        object.__setattr__(self, "secret_handles", tuple(sorted(secret_handles)))
        if not isinstance(self.handler, _BoundTaskHandler):
            raise TypeError("binding handler must be an engine-derived bound adapter")
        if (
            self.handler.alias_id != self.capability_id
            or self.handler.target_capability_id != self.target_capability_id
            or self.handler.data != frozen_data
            or self.handler.resource_ids != resource_ids
            or self.handler.secret_handles != secret_handles
            or self.handler.contract_id != self.contract_id
        ):
            raise ValueError("bound adapter disagrees with binding entry")
        _validate_entry_provenance(
            self.target_provenance,
            ExecutableKind.TASK_HANDLER,
            self.target_capability_id,
            self.target_provenance.owner_id,
        )

    @classmethod
    def _from_target(
        cls,
        *,
        capability_id: str,
        owner_id: str,
        target_capability_id: str,
        data: object,
        resource_ids: tuple[str, ...],
        secret_handles: tuple[str, ...] = (),
        contract_id: str | None = None,
        target: TaskHandler,
        target_provenance: ExecutableProvenance,
    ) -> CapabilityBindingEntry:
        handler = _BoundTaskHandler(
            alias_id=capability_id,
            target_capability_id=target_capability_id,
            data=data,
            resource_ids=resource_ids,
            secret_handles=secret_handles,
            contract_id=contract_id,
            target=target,
        )
        return cls(
            capability_id=capability_id,
            owner_id=owner_id,
            target_capability_id=target_capability_id,
            data=handler.data,
            resource_ids=handler.resource_ids,
            secret_handles=handler.secret_handles,
            contract_id=handler.contract_id,
            handler=handler,
            target_provenance=target_provenance,
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
    document = _schema_document_from_content(content)
    if not isinstance(document, dict):
        return None
    if "$schema" not in document:
        return None
    dialect = document["$schema"]
    if not isinstance(dialect, str):
        raise ValueError("schema entry dialect must be text")
    return dialect


def _schema_document_from_content(content: bytes) -> dict[str, object] | bool:
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
    return document


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
    apply_provenance: ExecutableProvenance
    reconcile_provenance: ExecutableProvenance
    authority: ContributionAuthority = dataclass_field(compare=False, repr=False)

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
        _validate_entry_provenance(
            self.apply_provenance,
            ExecutableKind.EFFECT_APPLY,
            self.kind,
            self.owner_id,
        )
        _validate_entry_authority(
            self.authority,
            self.handler,
            self.apply_provenance,
        )
        _validate_entry_provenance(
            self.reconcile_provenance,
            ExecutableKind.EFFECT_RECONCILE,
            self.kind,
            self.owner_id,
        )
        _validate_entry_authority(
            self.authority,
            self.handler,
            self.reconcile_provenance,
        )


def _validate_entry_authority(
    authority: ContributionAuthority,
    executable: object,
    provenance: ExecutableProvenance,
) -> None:
    if not isinstance(authority, ContributionAuthority):
        raise TypeError("executable registry entry requires a contribution authority generation")
    try:
        executable_authority = authority.authority(provenance.kind, provenance.registry_id)
    except KeyError as error:
        raise ValueError("executable registry entry is missing its declared authority") from error
    if executable_authority.executable is not executable or executable_authority.provenance != provenance:
        raise ValueError("executable registry entry disagrees with its authority generation")


def _validate_entry_provenance(
    provenance: ExecutableProvenance,
    kind: ExecutableKind,
    registry_id: str,
    owner_id: str,
) -> None:
    if not isinstance(provenance, ExecutableProvenance):
        raise TypeError("executable registry entry requires typed implementation provenance")
    if (
        provenance.kind is not kind
        or provenance.registry_id != registry_id
        or provenance.owner_id != owner_id
    ):
        raise ValueError("executable provenance disagrees with its registry entry")


def _validate_registry_id(value: object, kind: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{kind} must be text")
    try:
        return validate_qualified_id(value)
    except IdentifierError as error:
        raise ValueError(f"invalid {kind}: {value!r}") from error


def _snapshot_source_key(snapshot: SourceSnapshot) -> SourceKey:
    roles = {
        SourceKind.ENGINE: SourceRole.ENGINE,
        SourceKind.WHEEL_PRODUCT: SourceRole.PRODUCT,
        SourceKind.EDITABLE_PRODUCT: SourceRole.PRODUCT,
        SourceKind.PRODUCT_FILE: SourceRole.PRODUCT,
        SourceKind.WHEEL_PLUGIN: SourceRole.PLUGIN,
        SourceKind.EDITABLE_PLUGIN: SourceRole.PLUGIN,
        SourceKind.CONFIG_TREE: SourceRole.CONFIG,
    }
    return SourceKey(role=roles[snapshot.identity.kind], owner_id=_snapshot_owner_id(snapshot))


def _snapshot_owner_id(snapshot: SourceSnapshot) -> str:
    identity = snapshot.identity
    if identity.kind == SourceKind.CONFIG_TREE:
        value = identity.plugin_id
    elif identity.kind in {
        SourceKind.WHEEL_PLUGIN,
        SourceKind.EDITABLE_PLUGIN,
        SourceKind.WHEEL_PRODUCT,
        SourceKind.EDITABLE_PRODUCT,
    }:
        value = (
            identity.product_id
            if identity.kind in {SourceKind.WHEEL_PRODUCT, SourceKind.EDITABLE_PRODUCT}
            else identity.plugin_id
        )
    elif identity.kind == SourceKind.PRODUCT_FILE:
        value = identity.product_id
    elif identity.kind == SourceKind.ENGINE:
        value = "graph.engine"
    else:  # pragma: no cover - SourceKind is closed, defensive against unsafe construction.
        raise ValueError(f"source kind has no registry identity: {identity.kind!r}")
    if value is None:
        raise ValueError(f"source snapshot lacks registry identity: {identity.kind.value}")
    return _validate_registry_id(value, "snapshot owner id")


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


def _validate_sha256(value: object, kind: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{kind} must be a lowercase SHA-256 hex digest")
    try:
        parsed = bytes.fromhex(value)
    except ValueError as error:
        raise ValueError(f"{kind} must be a lowercase SHA-256 hex digest") from error
    if len(parsed) != 32 or value != value.lower():
        raise ValueError(f"{kind} must be a lowercase SHA-256 hex digest")
    return value


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
    entries: Mapping[SourceKey, SourceEntry]

    def __post_init__(self) -> None:
        if not isinstance(self.entries, Mapping):
            raise TypeError("registry entries must be a mapping")
        entries = MappingProxyType(
            dict(sorted(dict(self.entries).items(), key=lambda item: (item[0].role.value, item[0].owner_id)))
        )
        for source_key, entry in entries.items():
            if not isinstance(entry, SourceEntry):
                raise TypeError("source registry accepts only SourceEntry values")
            if source_key != entry.source_key:
                raise ValueError(f"source registry key disagrees with entry: {source_key}")
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
        _validate_object_view(bindings, expected_bindings, "binding")
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
class AttemptContractClaim:
    """One Feature-declared Attempt contract plus the owner/dependency closure used to resolve it."""

    contract: TaskAttemptContract[BaseModel, BaseModel]
    dependencies: tuple[str, ...] = ()
    available_handlers: Mapping[str, str] = dataclass_field(default_factory=dict)
    available_validators: Mapping[str, str] = dataclass_field(default_factory=dict)
    source_role: SourceRole = SourceRole.PLUGIN
    handler: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.contract, TaskAttemptContract):
            raise TypeError("attempt contract claim requires a TaskAttemptContract")
        if not isinstance(self.source_role, SourceRole):
            raise TypeError("attempt contract claim source role must be a SourceRole")
        object.__setattr__(self, "dependencies", tuple(self.dependencies))
        object.__setattr__(self, "available_handlers", MappingProxyType(dict(self.available_handlers)))
        object.__setattr__(self, "available_validators", MappingProxyType(dict(self.available_validators)))


@dataclass(frozen=True, slots=True)
class AttemptContractEntry:
    contract_id: str
    owner_id: str
    handler_id: str
    digest: str
    validators: tuple[str, ...]
    projection: FrozenJSONValue
    authority_handler: object | None = dataclass_field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        _validate_owned_registry_id(self.contract_id, self.owner_id, "attempt contract")
        _validate_registry_id(self.handler_id, "attempt contract handler id")
        _validate_sha256(self.digest, "attempt contract")
        object.__setattr__(self, "validators", tuple(self.validators))
        object.__setattr__(self, "projection", freeze_json(self.projection))


@dataclass(frozen=True, slots=True)
class AttemptContractRegistry:
    entries: Mapping[str, AttemptContractEntry]

    def __post_init__(self) -> None:
        entries = _immutable_mapping(self.entries)
        for contract_id, entry in entries.items():
            if not isinstance(entry, AttemptContractEntry):
                raise TypeError("attempt contract registry accepts only AttemptContractEntry values")
            if contract_id != entry.contract_id:
                raise ValueError(f"attempt contract registry key disagrees with entry: {contract_id}")
        object.__setattr__(self, "entries", entries)

    def projection(self) -> list[JSONValue]:
        return [cast(JSONValue, thaw_json(entry.projection)) for entry in self.entries.values()]

    @property
    def digest(self) -> str:
        return canonical_digest(self.projection())


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
        sources_by_owner: dict[str, list[SourceEntry]] = {}
        for source in self.sources.entries.values():
            owner_id = _snapshot_owner_id(source.snapshot)
            sources_by_owner.setdefault(owner_id, []).append(source)
        plugin_kinds = {
            SourceKind.WHEEL_PLUGIN,
            SourceKind.EDITABLE_PLUGIN,
            SourceKind.CONFIG_TREE,
        }
        for entry in owned_entries:
            candidates = sources_by_owner.get(entry.owner_id, [])
            if not candidates:
                raise ValueError(f"registry entry owner has no selected source: {entry.owner_id}")
            plugin_sources = tuple(
                source for source in candidates if source.snapshot.identity.kind in plugin_kinds
            )
            if not plugin_sources:
                raise ValueError(f"registry entry owner is not a plugin source: {entry.owner_id}")
            source_kind = plugin_sources[0].snapshot.identity.kind
            if source_kind == SourceKind.CONFIG_TREE and isinstance(
                entry,
                TaskHandlerEntry | CommitValidatorEntry | EffectEntry,
            ):
                raise ValueError(f"config source cannot own executable registry entry: {entry.owner_id}")
            provenances: tuple[ExecutableProvenance, ...]
            if isinstance(entry, TaskHandlerEntry | CommitValidatorEntry):
                provenances = (entry.provenance,)
            elif isinstance(entry, EffectEntry):
                provenances = (entry.apply_provenance, entry.reconcile_provenance)
            else:
                provenances = ()
            for provenance in provenances:
                selected = self.sources.entries.get(provenance.source_key)
                if selected is None or selected.snapshot.digest != provenance.source_digest:
                    raise ValueError(
                        "executable provenance disagrees with the selected plugin source: "
                        f"{provenance.registry_id}"
                    )
        for binding in self.capabilities.bindings.values():
            target = self.capabilities.entries.get(binding.target_capability_id)
            if not isinstance(target, TaskHandlerEntry) or binding.target_provenance != target.provenance:
                raise ValueError("binding target provenance disagrees with target capability")
            for resource_id in binding.resource_ids:
                if resource_id not in self.resources.entries:
                    raise ValueError(f"binding resource is not registered: {resource_id}")
        for effect in self.effects.entries.values():
            for role, schema_id in (
                ("intent", effect.intent_schema_id),
                ("receipt", effect.receipt_schema_id),
            ):
                schema = self.schemas.entries.get(schema_id)
                if schema is None:
                    raise ValueError(f"effect {role} schema is not registered: {schema_id}")
                try:
                    assert_closed_json_schema(_schema_document_from_content(schema.content))
                except ValueError as error:
                    raise ValueError(
                        f"effect {role} schema is outside the closed runtime subset: {schema_id}: {error}"
                    ) from error


class PluginRequirement(FrozenModel):
    """One exact source requirement expressed as a PEP 440 constraint."""

    plugin_id: str
    version_specifier: str

    @field_validator("plugin_id")
    @classmethod
    def _validate_plugin_id(cls, value: str) -> str:
        return _validate_manifest_id(value, "plugin requirement id")

    @field_validator("version_specifier")
    @classmethod
    def _validate_version_specifier(cls, value: str) -> str:
        try:
            return str(SpecifierSet(value))
        except InvalidSpecifier as error:
            raise ValueError(f"invalid plugin version specifier: {value!r}") from error


class WorkflowModuleRequirement(FrozenModel):
    """One Feature or Product module resource required by a modular product."""

    module_id: str
    owner_id: str
    resource_id: str

    @field_validator("module_id")
    @classmethod
    def _validate_module_id(cls, value: str) -> str:
        return _validate_manifest_id(value, "workflow module module id")

    @field_validator("owner_id")
    @classmethod
    def _validate_owner_id(cls, value: str) -> str:
        return _validate_manifest_id(value, "workflow module owner id")

    @field_validator("resource_id")
    @classmethod
    def _validate_resource_id(cls, value: str) -> str:
        return _validate_manifest_id(value, "workflow module resource id")


class WorkflowSlotBinding(FrozenModel):
    """One Product-owned binding of a module slot to a concrete capability."""

    module_id: str
    slot: str
    capability_id: str
    contract_id: str

    @field_validator("module_id")
    @classmethod
    def _validate_module_id(cls, value: str) -> str:
        return _validate_manifest_id(value, "workflow slot module id")

    @field_validator("capability_id")
    @classmethod
    def _validate_capability_id(cls, value: str) -> str:
        return _validate_manifest_id(value, "workflow slot capability id")

    @field_validator("contract_id")
    @classmethod
    def _validate_contract_id(cls, value: str) -> str:
        return _validate_manifest_id(value, "workflow slot contract id")

    @field_validator("slot")
    @classmethod
    def _validate_slot(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("workflow slot name must be non-empty text")
        return value


class ProductManifest(FrozenModel):
    """Normalized product input consumed by the registry platform."""

    schema_version: Literal["1"]
    source: ProviderSource | None
    product_id: str
    product_version: str
    engine_api: str
    plugins: tuple[PluginRequirement, ...]
    entrypoints: Mapping[str, str]
    configuration: FrozenJSONValue = Field(default_factory=dict)
    config_plugin_paths: tuple[str, ...] = ()
    workflow: WorkflowDef | None = None
    workflow_resource_id: str | None = None
    workflow_module: WorkflowModuleDef | None = None
    workflow_module_resources: tuple[WorkflowModuleRequirement, ...] = ()
    workflow_slot_bindings: tuple[WorkflowSlotBinding, ...] = ()
    graph_factory_symbol: str | None = None

    @field_validator("product_id")
    @classmethod
    def _validate_product_id(cls, value: str) -> str:
        return _validate_manifest_id(value, "product id")

    @field_validator("product_version")
    @classmethod
    def _validate_product_version(cls, value: str) -> str:
        try:
            return str(Version(value))
        except InvalidVersion as error:
            raise ValueError(f"invalid product version: {value!r}") from error

    @field_validator("engine_api")
    @classmethod
    def _validate_engine_api(cls, value: str) -> str:
        try:
            return str(Version(value))
        except InvalidVersion:
            try:
                return str(SpecifierSet(value))
            except InvalidSpecifier as error:
                raise ValueError(f"invalid engine API requirement: {value!r}") from error

    @field_validator("entrypoints", mode="after")
    @classmethod
    def _freeze_entrypoints(cls, values: Mapping[str, str]) -> Mapping[str, str]:
        copied = dict(values)
        if not copied:
            raise ValueError("product entrypoints must not be empty")
        if any(
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(graph_id, str)
            or not graph_id.strip()
            for name, graph_id in copied.items()
        ):
            raise ValueError("product entrypoints must map non-empty names to graph ids")
        return MappingProxyType(dict(sorted(copied.items())))

    @field_validator("configuration", mode="after")
    @classmethod
    def _validate_configuration(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            raise ValueError("product configuration must be a namespaced mapping")
        for plugin_id, plugin_configuration in cast(Mapping[object, object], value).items():
            if not isinstance(plugin_id, str):
                raise ValueError("product configuration namespace must be text")
            _validate_manifest_id(plugin_id, "configuration plugin id")
            if not isinstance(plugin_configuration, Mapping):
                raise ValueError(f"configuration for {plugin_id} must be a mapping")
        return value

    @field_validator("config_plugin_paths")
    @classmethod
    def _validate_config_plugin_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        frozen = tuple(values)
        if any(not isinstance(value, str) or not value for value in frozen):
            raise ValueError("config plugin paths must be non-empty text")
        if len(set(frozen)) != len(frozen):
            raise ValueError("config plugin paths must be unique")
        return frozen

    @field_validator("workflow_resource_id")
    @classmethod
    def _validate_workflow_resource_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_manifest_id(value, "workflow resource id")

    @model_validator(mode="after")
    def _validate_manifest_closure(self) -> ProductManifest:
        if (
            sum(
                value is not None
                for value in (
                    self.workflow,
                    self.workflow_resource_id,
                    self.workflow_module,
                    self.graph_factory_symbol,
                )
            )
            != 1
        ):
            raise ValueError("product manifest requires exactly one workflow form")
        if self.graph_factory_symbol is not None:
            _validate_product_graph_factory_symbol(self.graph_factory_symbol, self.source)
        if self.workflow_module is None:
            if self.workflow_module_resources:
                raise ValueError("workflow module resources are allowed only on the modular product form")
            if self.workflow_slot_bindings:
                raise ValueError("workflow slot bindings are allowed only on the modular product form")
        else:
            if self.workflow_module.owner_id != self.product_id:
                raise ValueError("product module owner must equal product id")
            if self.workflow_module.role != "product":
                raise ValueError("product module role must be product")
            if self.workflow_module.module_version != self.product_version:
                raise ValueError("product module version must equal product version")
            if dict(self.workflow_module.entrypoints) != dict(self.entrypoints):
                raise ValueError("product module entrypoints must equal product entrypoints")
        module_ids = tuple(item.module_id for item in self.workflow_module_resources)
        if len(set(module_ids)) != len(module_ids):
            raise ValueError("workflow module resource module ids must be unique")
        resource_ids = tuple(item.resource_id for item in self.workflow_module_resources)
        if len(set(resource_ids)) != len(resource_ids):
            raise ValueError("workflow module resource ids must be unique")
        slot_keys = tuple((item.module_id, item.slot) for item in self.workflow_slot_bindings)
        if len(set(slot_keys)) != len(slot_keys):
            raise ValueError("workflow slot bindings must be unique")
        if not self.plugins:
            raise ValueError("product manifest must require at least one plugin")
        plugin_ids = tuple(requirement.plugin_id for requirement in self.plugins)
        if len(set(plugin_ids)) != len(plugin_ids):
            raise ValueError("product plugin requirements must be unique")
        if self.workflow is not None and dict(self.workflow.entrypoints) != dict(self.entrypoints):
            raise ValueError("inline workflow entrypoints must equal product entrypoints")
        if self.source is not None:
            if self.source.entrypoint_group != "graph_engine.products":
                raise ValueError("product source must use graph_engine.products")
            if Version(self.source.version) != Version(self.product_version):
                raise ValueError("source version must equal product version")
        return self

    @model_serializer(mode="wrap")
    def _omit_absent_graph_factory_symbol(self, serializer: SerializerFunctionWrapHandler) -> object:
        data = serializer(self)
        if isinstance(data, dict) and data.get("graph_factory_symbol") is None:
            data.pop("graph_factory_symbol", None)
        return data

    @field_serializer("entrypoints")
    def _serialize_entrypoints(self, value: Mapping[str, str]) -> dict[str, str]:
        return dict(value)

    @field_serializer("configuration")
    def _serialize_configuration(self, value: object) -> object:
        return thaw_json(value)

    @property
    def required_plugin_ids(self) -> tuple[str, ...]:
        return tuple(requirement.plugin_id for requirement in self.plugins)


@dataclass(frozen=True, slots=True)
class FrozenComposition:
    """The sole immutable composition value accepted by the Phase 2 runtime."""

    manifest: ProductManifest
    descriptors: tuple[PluginDescriptor, ...]
    registries: RegistrySet
    workflow: CompiledWorkflow | None
    configuration: object
    contribution_authorities: Mapping[str, ContributionAuthority] = dataclass_field(
        compare=False,
        repr=False,
    )
    providers: Mapping[str, object]
    product_provider: object | None
    declarative_sources: Mapping[str, SourceSnapshot]
    lock: InvocationLock | ProductLock
    digest: str

    def __post_init__(self) -> None:
        from graph_engine.composition.lock import (
            InvocationLock,
            ProductLock,
            authenticate_composition_lock,
            pinned_execution_host_lock,
        )
        from graph_engine.composition.contributions import validate_registry_contribution_authorities
        from graph_engine.composition.declarative import _authenticate_frozen_config_contribution
        from graph_engine.composition.sources import AuthenticatedProviderBinding
        from graph_engine.graph.compiler import CompiledWorkflow

        if not isinstance(self.manifest, ProductManifest):
            raise TypeError("frozen composition manifest must be a ProductManifest")
        descriptors = tuple(self.descriptors)
        if any(not isinstance(descriptor, PluginDescriptor) for descriptor in descriptors):
            raise TypeError("frozen composition descriptors must contain PluginDescriptor values")
        if not isinstance(self.registries, RegistrySet):
            raise TypeError("frozen composition registries must be a RegistrySet")
        if self.manifest.graph_factory_symbol is not None:
            if self.workflow is not None:
                raise TypeError("factory composition must not carry a compiled workflow")
            if not isinstance(self.lock, ProductLock):
                raise TypeError("factory composition lock must be a ProductLock")
        else:
            if not isinstance(self.workflow, CompiledWorkflow):
                raise TypeError("frozen composition workflow must be a CompiledWorkflow")
            if not isinstance(self.lock, InvocationLock):
                raise TypeError("frozen composition lock must be an InvocationLock")
        providers = _immutable_mapping(self.providers)
        contribution_authorities = _immutable_mapping(self.contribution_authorities)
        declarative_sources = _immutable_mapping(self.declarative_sources)
        if any(not isinstance(snapshot, SourceSnapshot) for snapshot in declarative_sources.values()):
            raise TypeError("declarative source view must contain SourceSnapshot values")
        configuration = freeze_json(self.configuration)
        if not isinstance(configuration, Mapping):
            raise TypeError("frozen composition configuration must be a mapping")
        object.__setattr__(self, "descriptors", descriptors)
        object.__setattr__(self, "providers", providers)
        object.__setattr__(self, "contribution_authorities", contribution_authorities)
        object.__setattr__(self, "declarative_sources", declarative_sources)
        object.__setattr__(self, "configuration", configuration)

        for owner_id, authority in contribution_authorities.items():
            if authority.source_key.role is SourceRole.PLUGIN:
                provider = providers.get(owner_id)
                if not isinstance(provider, AuthenticatedProviderBinding):
                    raise ValueError(
                        f"wheel contribution lacks its authenticated provider binding: {owner_id}"
                    )
                try:
                    provider.authenticate_contribution(authority)
                except Exception as error:
                    raise ValueError(
                        f"wheel contribution provenance is not currently authenticated: {owner_id}"
                    ) from error
            else:
                snapshot = declarative_sources.get(owner_id)
                if not isinstance(snapshot, SourceSnapshot):
                    raise ValueError(f"declarative contribution lacks its frozen source bytes: {owner_id}")
                try:
                    _authenticate_frozen_config_contribution(
                        snapshot,
                        authority.descriptor,
                        authority.contribution,
                    )
                except Exception as error:
                    raise ValueError(
                        f"declarative contribution authority is not authenticated: {owner_id}"
                    ) from error
        validate_registry_contribution_authorities(
            self.registries,
            contribution_authorities,
            descriptors,
        )
        authenticate_composition_lock(
            self.manifest,
            descriptors,
            self.registries,
            self.workflow,
            configuration,
            self.lock,
            contribution_authorities,
        )
        if isinstance(self.lock, InvocationLock) and self.lock.execution_host != pinned_execution_host_lock():
            raise ValueError("frozen composition execution host disagrees with the pinned engine host")
        expected_digest = canonical_digest({"lock_digest": self.lock.digest})
        if self.digest != expected_digest:
            raise ValueError("frozen composition digest does not authenticate its lock")

        descriptor_ids = tuple(descriptor.plugin_id for descriptor in descriptors)
        if descriptor_ids != self.lock.dependency_order:
            raise ValueError("composition descriptors disagree with locked dependency order")
        implementation_ids = set(providers) | set(declarative_sources)
        if implementation_ids != set(descriptor_ids):
            raise ValueError("composition provider set disagrees with selected descriptors")
        for plugin_id, snapshot in declarative_sources.items():
            source = self.registries.sources.entries.get(SourceKey(SourceRole.CONFIG, plugin_id))
            if source is None or source.snapshot is not snapshot:
                raise ValueError("composition declarative source is not the selected snapshot")

        engine_source = self.registries.sources.entries.get(SourceKey(SourceRole.ENGINE, "graph.engine"))
        if engine_source is None or engine_source.snapshot.digest != self.lock.engine_digest:
            raise ValueError("composition engine source disagrees with invocation lock")
        product_source = self.registries.sources.entries.get(
            SourceKey(SourceRole.PRODUCT, self.manifest.product_id)
        )
        if product_source is None or (product_source.snapshot.digest != self.lock.product.source.digest):
            raise ValueError("composition product source disagrees with invocation lock")
        locked_plugins = {plugin.plugin_id: plugin for plugin in self.lock.plugins}
        for plugin_id in descriptor_ids:
            role = (
                SourceRole.PLUGIN
                if locked_plugins[plugin_id].descriptor.source is not None
                else SourceRole.CONFIG
            )
            source = self.registries.sources.entries.get(SourceKey(role, plugin_id))
            if source is None or source.snapshot.digest != locked_plugins[plugin_id].source.digest:
                raise ValueError("composition plugin source disagrees with invocation lock")

        executable_entries = (
            *(
                (entry.owner_id, entry.handler, entry.provenance, entry.authority)
                for entry in self.registries.capabilities.entries.values()
                if isinstance(entry, TaskHandlerEntry)
            ),
            *(
                (entry.owner_id, entry.validator, entry.provenance, entry.authority)
                for entry in self.registries.capabilities.entries.values()
                if isinstance(entry, CommitValidatorEntry)
            ),
            *(
                (entry.owner_id, entry.handler, entry.apply_provenance, entry.authority)
                for entry in self.registries.effects.entries.values()
            ),
            *(
                (entry.owner_id, entry.handler, entry.reconcile_provenance, entry.authority)
                for entry in self.registries.effects.entries.values()
            ),
        )
        expected_executables = {
            (descriptor.plugin_id, kind, registry_id)
            for descriptor in descriptors
            for kind, registry_id in _descriptor_executable_keys(descriptor)
        }
        actual_executables = {
            (owner_id, provenance.kind, provenance.registry_id)
            for owner_id, _, provenance, _ in executable_entries
        }
        if actual_executables != expected_executables:
            raise ValueError("composition registries disagree with declared executable set")
        for owner_id, _, _, authority in executable_entries:
            if contribution_authorities.get(owner_id) is not authority:
                raise ValueError(
                    f"composition registry entries mix contribution authority generations: {owner_id}"
                )
        for owner_id, executable, provenance, authority in executable_entries:
            provider = providers.get(owner_id)
            if not isinstance(provider, AuthenticatedProviderBinding):
                raise ValueError(
                    f"executable registry owner lacks an authenticated provider binding: {owner_id}"
                )
            try:
                provider.authenticate_executable(authority, executable, provenance)
            except Exception as error:
                raise ValueError(
                    f"executable registry provenance is not currently authenticated: {provenance.registry_id}"
                ) from error

    @classmethod
    def freeze(
        cls,
        manifest: ProductManifest,
        registries: RegistrySet,
        workflow: CompiledWorkflow | None,
        lock: InvocationLock | ProductLock,
        *,
        descriptors: tuple[PluginDescriptor, ...] = (),
        configuration: object | None = None,
        contribution_authorities: Mapping[str, ContributionAuthority] | None = None,
        providers: Mapping[str, object] | None = None,
        product_provider: object | None = None,
        declarative_sources: Mapping[str, SourceSnapshot] | None = None,
    ) -> FrozenComposition:
        from graph_engine.composition.lock import authenticate_composition_lock

        frozen_configuration = freeze_json(manifest.configuration if configuration is None else configuration)
        authenticate_composition_lock(
            manifest,
            tuple(descriptors),
            registries,
            workflow,
            frozen_configuration,
            lock,
            {} if contribution_authorities is None else contribution_authorities,
        )
        digest = canonical_digest({"lock_digest": lock.digest})
        return cls(
            manifest=manifest,
            descriptors=tuple(descriptors),
            registries=registries,
            workflow=workflow,
            configuration=frozen_configuration,
            contribution_authorities=({} if contribution_authorities is None else contribution_authorities),
            providers={} if providers is None else providers,
            product_provider=product_provider,
            declarative_sources={} if declarative_sources is None else declarative_sources,
            lock=lock,
            digest=digest,
        )

    @property
    def lock_digest(self) -> str:
        return self.lock.digest


def _validate_manifest_id(value: object, kind: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid {kind}: {value!r}")
    try:
        return validate_qualified_id(value)
    except IdentifierError as error:
        raise ValueError(f"invalid {kind}: {value!r}") from error


def _file_digest_document(files: tuple[SourceFile, ...]) -> JSONValue:
    return [{"path": item.path, "sha256": item.sha256} for item in files]


def _snapshot_digest(identity: SourceIdentity, files: tuple[SourceFile, ...]) -> str:
    file_document = _file_digest_document(files)
    if identity.kind == SourceKind.ENGINE:
        if identity.engine_installation is None:
            return canonical_digest(file_document)
        engine_identity: dict[str, JSONValue] = {
            "kind": identity.kind.value,
            "distribution": identity.distribution,
            "version": identity.version,
            "installation": identity.engine_installation,
        }
        if identity.engine_installation == "editable":
            engine_identity["root"] = str(identity.root)
        return canonical_digest({"identity": engine_identity, "files": file_document})
    if identity.kind == SourceKind.PRODUCT_FILE:
        if identity.product_id is None:
            return canonical_digest(file_document)
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
    if identity.kind == SourceKind.CONFIG_TREE:
        if identity.plugin_id is None:
            return canonical_digest(file_document)
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
        "entrypoint_value": identity.entrypoint_value,
        "declaration_path": identity.declaration_path,
    }
    if identity.import_roots is not None:
        identity_document["import_roots"] = list(identity.import_roots)
    if identity.product_id is not None:
        identity_document["product_id"] = identity.product_id
        identity_document["product_version"] = identity.product_version
    if identity.plugin_id is not None:
        identity_document["plugin_id"] = identity.plugin_id
        identity_document["plugin_version"] = identity.plugin_version
    if identity.kind in {SourceKind.EDITABLE_PRODUCT, SourceKind.EDITABLE_PLUGIN}:
        identity_document["root"] = str(identity.root)
    return canonical_digest({"identity": identity_document, "files": file_document})


def _validate_product_graph_factory_symbol(symbol: str, source: ProviderSource | None) -> str:
    if not isinstance(symbol, str) or symbol.count(":") != 1:
        raise ValueError("graph factory symbol must be module:attribute")
    module_name, attribute = symbol.split(":")
    if (
        not module_name
        or module_name.startswith(".")
        or any(not part.isidentifier() for part in module_name.split("."))
    ):
        raise ValueError("graph factory symbol module is not a public absolute import")
    if not attribute.isidentifier() or attribute.startswith("_"):
        raise ValueError("graph factory symbol attribute must be a public name")
    if module_name == "config_tree" or module_name.startswith("config_tree."):
        raise ValueError("graph factory symbol cannot come from configuration")
    if source is None:
        return symbol
    if not _module_belongs_to_import_roots(module_name, source.import_roots):
        raise ValueError("graph factory symbol is outside the authenticated Product import roots")
    return symbol


def _module_belongs_to_import_roots(module_name: str, import_roots: tuple[str, ...]) -> bool:
    for import_root in import_roots:
        if not import_root:
            continue
        if module_name == import_root or module_name.startswith(f"{import_root}."):
            return True
    return False


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
    "AttemptContractClaim",
    "AttemptContractEntry",
    "AttemptContractRef",
    "AttemptContractRegistry",
    "CapabilityBindingEntry",
    "CapabilityEntry",
    "CapabilityRegistry",
    "CommitValidatorEntry",
    "EffectEntry",
    "EffectRegistry",
    "FrozenComposition",
    "PluginRequirement",
    "ProductManifest",
    "WorkflowModuleRequirement",
    "WorkflowSlotBinding",
    "RegistrySet",
    "ResourceEntry",
    "ResourceRegistry",
    "SchemaEntry",
    "SchemaRegistry",
    "SourceEntry",
    "SourceFile",
    "SourceIdentity",
    "SourceKey",
    "SourceKind",
    "SourceRole",
    "SourceRegistry",
    "SourceSnapshot",
    "TaskHandlerEntry",
]
